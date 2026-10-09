"""The Tether window: a GTK 4 + libadwaita front end that drives the daemon over its control socket."""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import __version__  # noqa: E402
from .config import Paths  # noqa: E402

APP_ID = "dev.tether.Tether"


def friendly(error: str | None) -> str:
    """'not_found: no device is connected' → 'No device is connected'."""
    text = (error or "Something went wrong").split(": ", 1)[-1]
    return text[:1].upper() + text[1:]


def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def device_subtitle(d: dict) -> str:
    if not d["connected"]:
        return "Not connected"
    b = d.get("battery")
    if not b:
        return "Connected"
    return f"Connected · Battery {b['level']}%{' · charging' if b['charging'] else ''}"


def short_code(sas: str) -> str:
    return f"{sas[:3]} {sas[3:]}"


# -- daemon connection --------------------------------------------------------------


class Daemon(GObject.Object):
    """Talks to the daemon. Events arrive on the main loop as the "event" signal."""

    __gsignals__ = {
        "event": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "running": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    def __init__(self, paths: Paths, listen: bool = True):
        super().__init__()
        self.path = str(paths.socket)
        self.up: bool | None = None
        if listen:
            threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(self.path)
                    f = s.makefile("rwb")
                    f.write(b'{"cmd": "subscribe"}\n')
                    f.flush()
                    GLib.idle_add(self._set_up, True)
                    for line in f:
                        ev = json.loads(line)
                        GLib.idle_add(self._emit_event, ev)
            except (OSError, ValueError):
                pass
            GLib.idle_add(self._set_up, False)
            time.sleep(2)

    def _emit_event(self, ev):
        self.emit("event", ev)
        return False

    def _set_up(self, up: bool):
        if up != self.up:
            self.up = up
            self.emit("running", up)
        return False

    def call(self, cmd: str, callback=None, **kw):
        """Runs a command on its own connection (long ones don't block others); callback(result) on the main loop."""

        def work():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(self.path)
                    f = s.makefile("rwb")
                    f.write(json.dumps({"cmd": cmd, **kw}).encode() + b"\n")
                    f.flush()
                    res = json.loads(f.readline() or b'{"ok": false, "error": "The service closed the connection"}')
            except OSError:
                res = {"ok": False, "error": "The Tether service isn't running"}
            except ValueError:
                res = {"ok": False, "error": "Garbled answer from the service"}
            if callback:
                GLib.idle_add(lambda: callback(res) and False)

        threading.Thread(target=work, daemon=True).start()


# -- small widget helpers ---------------------------------------------------------------


def icon_button(icon: str, tooltip: str, on_click=None, action: str | None = None, target: str | None = None):
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    b.add_css_class("flat")
    if on_click:
        b.connect("clicked", lambda *_: on_click())
    if action:
        b.set_action_name(action)
        if target is not None:
            b.set_action_target_value(GLib.Variant("s", target))
    return b


def text_button(label: str, on_click, style: str | None = None):
    b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    if style:
        b.add_css_class(style)
    b.connect("clicked", lambda *_: on_click())
    return b


def placeholder_row(text: str) -> Adw.ActionRow:
    row = Adw.ActionRow(title=text)
    row.add_css_class("dim-label")
    return row


class ListGroup:
    """A preferences group whose rows are rebuilt only when their data changes."""

    def __init__(self, title: str, description: str | None = None):
        self.group = Adw.PreferencesGroup(title=title)
        if description:
            self.group.set_description(description)
        self.rows: list[Gtk.Widget] = []
        self.signature = None

    def update(self, data, build):
        sig = json.dumps(data, sort_keys=True, default=str)
        if sig == self.signature:
            return
        self.signature = sig
        for r in self.rows:
            self.group.remove(r)
        self.rows = build(data)
        for r in self.rows:
            self.group.add(r)


# -- the window ---------------------------------------------------------------------


class TetherWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, daemon: Daemon):
        super().__init__(application=app, title="Tether")
        self.set_default_size(760, 680)
        self.set_size_request(360, 420)
        self.daemon = daemon
        self.status: dict | None = None
        self.pair_dialogs: dict[str, Adw.AlertDialog] = {}
        self.transfer_rows: dict[int, tuple] = {}
        self._updating = False

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(self._devices_page(), "devices", "Devices", "phone-symbolic")
        self.stack.add_titled_with_icon(self._folders_page(), "folders", "Folders", "folder-symbolic")
        self.stack.add_titled_with_icon(self._commands_page(), "commands", "Commands", "utilities-terminal-symbolic")
        self.stack.add_titled_with_icon(self._settings_page(), "settings", "Settings", "emblem-system-symbolic")
        self.toasts.set_child(self.stack)

        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar(title_widget=switcher)
        bottom = Adw.ViewSwitcherBar(stack=self.stack)
        self.banner = Adw.Banner(title="The Tether service isn't running", button_label="Start")
        self.banner.connect("button-clicked", lambda *_: self._systemctl("start"))

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        view.add_bottom_bar(bottom)
        view.set_content(self.toasts)
        self.set_content(view)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        no_title = GObject.Value(Gtk.Widget)
        no_title.set_object(None)
        narrow.add_setter(header, "title-widget", no_title)
        narrow.add_setter(bottom, "reveal", GObject.Value(bool, True))
        self.add_breakpoint(narrow)

        for name, fn in (
            ("send-files", self.pick_and_send),
            ("send-clip", self.send_clipboard),
            ("notify", self.ask_notification),
            ("unpair", self.confirm_unpair),
            ("browse", lambda _id: self.browse()),
        ):
            act = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            act.connect("activate", lambda _a, v, fn=fn: fn(v.get_string()))
            self.add_action(act)

        daemon.connect("event", self.on_event)
        daemon.connect("running", self.on_running)
        self.on_running(daemon, bool(daemon.up))
        GLib.timeout_add_seconds(3, self._poll)

    # -- plumbing -------------------------------------------------------------------

    def toast(self, text: str, button: str | None = None, on_button=None):
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=4)
        if button and on_button:
            t.set_button_label(button)
            t.connect("button-clicked", lambda *_: on_button())
        self.toasts.add_toast(t)

    def call(self, cmd: str, done=None, ok_toast: str | None = None, refresh: bool = True, **kw):
        def cb(res):
            if not res.get("ok"):
                self.toast(friendly(res.get("error")))
            elif ok_toast:
                self.toast(ok_toast)
            if done and res.get("ok"):
                done(res)
            if refresh:
                self.refresh()

        self.daemon.call(cmd, cb, **kw)

    def refresh(self):
        self.daemon.call("status", lambda res: res.get("ok") and self.apply_status(res))

    def _poll(self):
        if self.get_visible() and self.daemon.up:
            self.refresh()
        return True

    def _systemctl(self, verb: str):
        try:
            subprocess.Popen(["systemctl", "--user", verb, "tether.service"])
        except OSError as e:
            self.toast(f"Couldn't run systemctl: {e}")

    def on_running(self, _daemon, up: bool):
        self.banner.set_revealed(not up)
        self.stack.set_sensitive(up)
        self.service_row.set_subtitle("Running" if up else "Stopped")
        if up:
            self.refresh()
            self.refresh_wol()

    def on_event(self, _daemon, ev: dict):
        kind = ev.get("ev")
        if kind == "status":
            self.apply_status(ev)
        elif kind == "pair_request":
            if not ev.get("initiator"):
                self.show_pair_dialog(ev["id"], ev["name"], ev["sas"])
            self.refresh()
        elif kind == "paired":
            self._close_pair_dialog(ev.get("id"))
            self.toast(f"Paired with {ev.get('name', 'the device')}")
            self.refresh()
        elif kind == "pair_failed":
            self._close_pair_dialog(ev.get("id"))
            self.toast("Pairing timed out" if ev.get("reason") == "timeout" else "Pairing was cancelled")
            self.refresh()
        elif kind == "transfer":
            self.update_transfer(ev)
        elif kind == "file_received":
            path = Path(ev.get("path", ""))
            self.toast(f"Received {path.name}", "Show", lambda: self.open_uri(path.parent.as_uri()))

    def open_uri(self, uri: str):
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as e:
            self.toast(f"Couldn't open {uri}: {e.message}")

    def device_name(self, device_id: str) -> str:
        for d in (self.status or {}).get("devices", []):
            if d["id"] == device_id:
                return d["name"]
        return "the device"

    # -- status -> widgets ------------------------------------------------------------

    def apply_status(self, st: dict):
        self.status = st
        self._updating = True
        try:
            self.paired.update(st["devices"], self._device_rows)
            self.pending.update(st["pending"], self._pending_rows)
            self.pending.group.set_visible(bool(st["pending"]))
            self.nearby.update(st["discovered"], self._nearby_rows)
            self.shares.update(st["shares"], self._share_rows)
            self.synced.update(st["sync"], self._sync_rows)
            self.saved_cmds.update(st["commands"], self._command_rows)
            self.download_row.set_subtitle(st["download_dir"])
            self.shell_row.set_active(bool(st["remote_shell"]))
            self.timeout_row.set_value(float(st.get("command_timeout", 120)))
            self.clip_row.set_active(bool(st["clipboard"]))
            self.notif_row.set_active(bool(st.get("notifications", True)))
            self.apps.update({"apps": st.get("notif_apps", []), "muted": st.get("notif_muted", [])}, self._app_rows)
            for t in st.get("transfers", []):
                self.update_transfer(t)
            self.clip_row.set_subtitle(
                "Uses the Tether shell extension" if st["clipboard_backend"] == "gnome-extension"
                else f"Uses {st['clipboard_backend']}"
            )
            if not self.name_row.has_focus() and self.name_row.get_text() != st["name"]:
                self.name_row.set_text(st["name"])
            self.id_row.set_subtitle(" ".join(re.findall("....", st["id"][:32])))
            for p in st["pending"]:
                if not p["confirmed"] and not p["initiator"] and p["id"] not in self.pair_dialogs:
                    self.show_pair_dialog(p["id"], p["name"], p["sas"])
        finally:
            self._updating = False

    # -- devices page -------------------------------------------------------------------

    def _devices_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        self.transfers_group = Adw.PreferencesGroup(title="Transfers", visible=False)
        page.add(self.transfers_group)
        self.pending = ListGroup("Pairing")
        self.paired = ListGroup("Paired devices", "Drop files on a connected device to send them.")
        self.nearby = ListGroup("Nearby", "Devices running Tether on this network")
        self.nearby.group.set_header_suffix(text_button("Pair by Address…", self.ask_address, "flat"))
        for g in (self.pending, self.paired, self.nearby):
            page.add(g.group)
        return page

    def _device_rows(self, devices: list) -> list:
        if not devices:
            return [placeholder_row("No paired devices yet — pair one below")]
        rows = []
        for d in devices:
            online = d["connected"]
            row = Adw.ActionRow(title=GLib.markup_escape_text(d["name"]), subtitle=device_subtitle(d))
            icon = Gtk.Image.new_from_icon_name("phone-symbolic" if d.get("type") == "phone" else "computer-symbolic")
            if not online:
                icon.add_css_class("dim-label")
            row.add_prefix(icon)
            menu = Gio.Menu()
            if online:
                row.add_suffix(icon_button("document-send-symbolic", "Send Files…", action="win.send-files", target=d["id"]))
                row.add_suffix(icon_button("folder-remote-symbolic", "Browse Files", action="win.browse", target=d["id"]))
                menu.append("Send Clipboard", f"win.send-clip::{d['id']}")
                menu.append("Send Notification…", f"win.notify::{d['id']}")
                self._accept_drops(row, d["id"])
            menu.append("Unpair…", f"win.unpair::{d['id']}")
            more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER,
                                  tooltip_text="More")
            more.add_css_class("flat")
            row.add_suffix(more)
            rows.append(row)
        return rows

    def _accept_drops(self, row: Gtk.Widget, device_id: str):
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)

        def on_drop(_t, value, _x, _y):
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
            files = [p for p in paths if os.path.isfile(p)]
            if not files:
                self.toast("Only files can be sent, not folders")
                return False
            self.send_paths(device_id, files)
            return True

        target.connect("drop", on_drop)
        target.connect("enter", lambda *_: (row.add_css_class("accent"), Gdk.DragAction.COPY)[1])
        target.connect("leave", lambda *_: row.remove_css_class("accent"))
        row.add_controller(target)

    def _pending_rows(self, pending: list) -> list:
        rows = []
        for p in pending:
            row = Adw.ActionRow(title=GLib.markup_escape_text(f"Pairing with {p['name']}"),
                                subtitle=f"Code {short_code(p['sas'])}")
            if p["confirmed"]:
                row.add_suffix(Gtk.Label(label="Waiting for the other device…", css_classes=["dim-label"]))
            else:
                row.add_suffix(text_button("Reject", lambda i=p["id"]: self.answer_pairing(i, False)))
                row.add_suffix(text_button("Codes Match", lambda i=p["id"]: self.answer_pairing(i, True),
                                           "suggested-action"))
            rows.append(row)
        return rows

    def _nearby_rows(self, found: list) -> list:
        if not found:
            row = Adw.ActionRow(title="Looking for devices…",
                                subtitle="Open Tether on your phone; it appears here within a few seconds")
            spinner = Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER)
            row.add_suffix(spinner)
            return [row]
        rows = []
        for d in found:
            row = Adw.ActionRow(title=GLib.markup_escape_text(d["name"]), subtitle=d.get("addr", ""))
            row.add_prefix(Gtk.Image.new_from_icon_name(
                "phone-symbolic" if d.get("type") == "phone" else "computer-symbolic"))
            row.add_suffix(text_button("Pair", lambda i=d["id"]: self.start_pairing(device=i), "suggested-action"))
            rows.append(row)
        return rows

    # -- transfers -------------------------------------------------------------------------

    def update_transfer(self, t: dict):
        tid = t["id"]
        entry = self.transfer_rows.get(tid)
        if entry is None:
            if t["state"] != "active":
                return
            arrow = "To" if t["direction"] == "out" else "From"
            row = Adw.ActionRow(title=GLib.markup_escape_text(t["name"]))
            row.add_prefix(Gtk.Image.new_from_icon_name(
                "document-send-symbolic" if t["direction"] == "out" else "folder-download-symbolic"))
            bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=140)
            row.add_suffix(bar)
            row.add_suffix(icon_button("process-stop-symbolic", "Cancel",
                                       lambda: self.call("transfer_cancel", refresh=False, id=tid)))
            entry = (row, bar, f"{arrow} {t['peer_name']}")
            self.transfer_rows[tid] = entry
            self.transfers_group.add(row)
            self.transfers_group.set_visible(True)
        row, bar, who = entry
        total = t.get("total") or 0
        if total:
            bar.set_fraction(min(1.0, t["done"] / total))
            row.set_subtitle(f"{who} · {human(t['done'])} of {human(total)}")
        else:
            bar.pulse()
            row.set_subtitle(f"{who} · {human(t['done'])}")
        if t["state"] != "active":
            if t["state"] == "failed":
                self.toast(f"Couldn't transfer {t['name']}")
            elif t["state"] == "cancelled":
                self.toast(f"Cancelled {t['name']}")
            bar.set_fraction(1.0 if t["state"] == "done" else bar.get_fraction())
            GLib.timeout_add(1200 if t["state"] == "done" else 0, self._remove_transfer, tid)

    def _remove_transfer(self, tid: int):
        entry = self.transfer_rows.pop(tid, None)
        if entry:
            self.transfers_group.remove(entry[0])
        self.transfers_group.set_visible(bool(self.transfer_rows))
        return False

    # -- pairing --------------------------------------------------------------------------

    def start_pairing(self, device: str | None = None, addr: str | None = None):
        kw = {"device": device} if device else {"addr": addr}
        self.call("pair", lambda res: self.show_pair_dialog(res["device"], res["name"], res["sas"]), **kw)

    def ask_address(self):
        entry = Gtk.Entry(placeholder_text="192.168.1.42", activates_default=True)
        dialog = Adw.AlertDialog(heading="Pair by Address",
                                 body="Enter the phone's IP address, shown in the Tether app.")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("pair", "Pair")
        dialog.set_response_appearance("pair", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("pair")

        def on_response(_d, resp):
            addr = entry.get_text().strip()
            if resp == "pair" and addr:
                self.start_pairing(addr=addr)

        dialog.connect("response", on_response)
        dialog.present(self)

    def show_pair_dialog(self, device_id: str, name: str, sas: str):
        if device_id in self.pair_dialogs:
            return
        code = Gtk.Label(label=short_code(sas))
        code.add_css_class("title-1")
        code.add_css_class("monospace")
        dialog = Adw.AlertDialog(heading=f"Pair with {name}?",
                                 body="Make sure the other device shows the same code.")
        dialog.set_extra_child(code)
        dialog.add_response("reject", "Cancel")
        dialog.add_response("accept", "Codes Match")
        dialog.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("reject")

        def on_response(_d, resp):
            if self.pair_dialogs.get(device_id) is not dialog:
                return  # closed because pairing finished elsewhere
            self.answer_pairing(device_id, resp == "accept")

        dialog.connect("response", on_response)
        self.pair_dialogs[device_id] = dialog
        dialog.present(self)

    def _close_pair_dialog(self, device_id):
        d = self.pair_dialogs.pop(device_id, None)
        if d is not None:
            d.force_close()

    def answer_pairing(self, device_id: str, accept: bool):
        self._close_pair_dialog(device_id)
        self.call("pair_confirm", device=device_id, accept=accept)

    # -- device actions -------------------------------------------------------------------

    def pick_and_send(self, device_id: str):
        dialog = Gtk.FileDialog(title=f"Send to {self.device_name(device_id)}")

        def done(d, result):
            try:
                files = d.open_multiple_finish(result)
            except GLib.Error:
                return
            paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            self.send_paths(device_id, [p for p in paths if p])

        dialog.open_multiple(self, None, done)

    def send_paths(self, device_id: str, paths: list):
        if not paths:
            return
        n = len(paths)
        noun = os.path.basename(paths[0]) if n == 1 else f"{n} files"
        self.toast(f"Sending {noun}…")
        self.call("send", ok_toast=f"Sent {noun}", refresh=False, device=device_id, paths=paths)

    def send_clipboard(self, device_id: str):
        clipboard = self.get_display().get_clipboard()

        def done(cb, result):
            try:
                text = cb.read_text_finish(result)
            except GLib.Error:
                text = None
            if not text:
                self.toast("The clipboard has no text")
                return
            self.call("clip_send", ok_toast="Clipboard sent", refresh=False, text=text)

        clipboard.read_text_async(None, done)

    def ask_notification(self, device_id: str):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        title = Gtk.Entry(placeholder_text="Title", activates_default=True)
        body = Gtk.Entry(placeholder_text="Message (optional)", activates_default=True)
        box.append(title)
        box.append(body)
        dialog = Adw.AlertDialog(heading=f"Notify {self.device_name(device_id)}")
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("send", "Send")
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("send")

        def on_response(_d, resp):
            if resp == "send" and title.get_text().strip():
                self.call("notify", ok_toast="Notification sent", refresh=False,
                          device=device_id, title=title.get_text().strip(), text=body.get_text().strip())

        dialog.connect("response", on_response)
        dialog.present(self)

    def confirm_unpair(self, device_id: str):
        name = self.device_name(device_id)
        dialog = Adw.AlertDialog(heading=f"Unpair {name}?",
                                 body="It will no longer connect until you pair again.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("unpair", "Unpair")
        dialog.set_response_appearance("unpair", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r: r == "unpair" and self.call(
            "unpair", ok_toast=f"Unpaired {name}", device=device_id))
        dialog.present(self)

    def browse(self):
        if self.status:
            self.open_uri(self.status["dav_url"])

    # -- folders page -------------------------------------------------------------------

    def _folders_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()

        received = Adw.PreferencesGroup(title="Received Files")
        self.download_row = Adw.ActionRow(title="Saved to")
        self.download_row.add_suffix(icon_button("folder-open-symbolic", "Open Folder",
                                                 lambda: self.open_uri(Path(self.status["download_dir"]).as_uri())))
        self.download_row.add_suffix(text_button("Change…", self.change_download_dir, "flat"))
        received.add(self.download_row)
        page.add(received)

        browse = Adw.PreferencesGroup(title="Phone Storage",
                                      description="Browse a connected phone like any other folder.")
        open_row = Adw.ActionRow(title="Open in Files", activatable=True)
        open_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        open_row.connect("activated", lambda *_: self.browse())
        browse.add(open_row)
        side_row = Adw.ActionRow(title="Show in the Files sidebar", subtitle="Adds a “Phone (Tether)” bookmark")
        side_row.add_suffix(text_button("Add", self.add_bookmark, "flat"))
        browse.add(side_row)
        page.add(browse)

        self.shares = ListGroup("Shared With Your Phone", "Your phone can browse and edit these folders.")
        self.shares.group.set_header_suffix(icon_button("list-add-symbolic", "Share a Folder…", self.add_share))
        page.add(self.shares.group)

        self.synced = ListGroup("Synced Folders",
                                "Kept identical on both devices. Give the folder the same name on the phone.")
        suffix = Gtk.Box(spacing=4)
        suffix.append(icon_button("view-refresh-symbolic", "Sync Now",
                                  lambda: self.call("rescan", ok_toast="Folders rescanned")))
        suffix.append(icon_button("list-add-symbolic", "Sync a Folder…", self.add_sync))
        self.synced.group.set_header_suffix(suffix)
        page.add(self.synced.group)
        return page

    def _share_rows(self, shares: dict) -> list:
        if not shares:
            return [placeholder_row("Nothing shared")]
        rows = []
        for name, path in shares.items():
            row = Adw.ActionRow(title=GLib.markup_escape_text(name), subtitle=GLib.markup_escape_text(path))
            row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
            row.add_suffix(icon_button("user-trash-symbolic", "Stop Sharing",
                                       lambda n=name: self.call("share_remove", name=n)))
            rows.append(row)
        return rows

    def _sync_rows(self, folders: list) -> list:
        if not folders:
            return [placeholder_row("No synced folders")]
        rows = []
        for f in folders:
            row = Adw.ActionRow(title=GLib.markup_escape_text(f["id"]), subtitle=GLib.markup_escape_text(f["path"]))
            row.add_prefix(Gtk.Image.new_from_icon_name("emblem-synchronizing-symbolic"))
            row.add_suffix(icon_button("user-trash-symbolic", "Stop Syncing",
                                       lambda i=f["id"]: self.call("sync_remove", folder=i)))
            rows.append(row)
        return rows

    def pick_folder(self, title: str, then):
        dialog = Gtk.FileDialog(title=title)

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            if folder and folder.get_path():
                then(folder.get_path())

        dialog.select_folder(self, None, done)

    def change_download_dir(self):
        self.pick_folder("Save Received Files In", lambda p: self.call("set", key="download_dir", value=p))

    def add_bookmark(self):
        from .cli import add_bookmark

        if self.status:
            add_bookmark(self.status["dav_url"])
            self.toast("Added to the Files sidebar")

    def add_share(self):
        def then(path):
            base = os.path.basename(path.rstrip("/")) or "Files"
            name, i = base, 2
            while self.status and name in self.status["shares"]:
                name, i = f"{base} {i}", i + 1
            self.call("share_add", ok_toast=f"Sharing {name}", name=name, path=path)

        self.pick_folder("Share a Folder", then)

    def add_sync(self):
        def then(path):
            suggested = re.sub(r"[^a-z0-9_-]+", "-", os.path.basename(path.rstrip("/")).lower()).strip("-") or "folder"
            entry = Gtk.Entry(text=suggested, activates_default=True)
            dialog = Adw.AlertDialog(heading="Name This Folder",
                                     body=f"{path}\n\nUse the same name when adding the folder on the phone.")
            dialog.set_extra_child(entry)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("add", "Sync")
            dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("add")

            def on_response(_d, resp):
                fid = re.sub(r"[^a-z0-9_-]+", "-", entry.get_text().strip().lower()).strip("-")
                if resp == "add" and fid:
                    self.call("sync_add", ok_toast=f"Syncing “{fid}”", folder=fid, path=path)

            dialog.connect("response", on_response)
            dialog.present(self)

        self.pick_folder("Sync a Folder", then)

    # -- commands page ----------------------------------------------------------------------

    def _commands_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        self.saved_cmds = ListGroup("Saved Commands",
                                    "Buttons your phone can press. They run as you, through /bin/sh, "
                                    "in your home folder.")
        page.add(self.saved_cmds.group)

        add = Adw.PreferencesGroup(title="Add a Command")
        self.cmd_name = Adw.EntryRow(title="Name, e.g. Lock screen")
        self.cmd_line = Adw.EntryRow(title="Command line, e.g. loginctl lock-session")
        self.cmd_line.add_css_class("monospace")
        self.cmd_add = text_button("Add", self.add_command, "suggested-action")
        self.cmd_add.set_sensitive(False)
        for e in (self.cmd_name, self.cmd_line):
            e.connect("changed", lambda *_: self.cmd_add.set_sensitive(
                bool(self.cmd_name.get_text().strip() and self.cmd_line.get_text().strip())))
            add.add(e)
        add.set_header_suffix(self.cmd_add)
        page.add(add)

        free = Adw.PreferencesGroup(title="Free-Form Commands")
        self.shell_row = Adw.SwitchRow(title="Allow any command from the phone",
                                       subtitle="Anyone holding your unlocked phone could run anything as you. "
                                                "Each command still shows a notification here.")
        self.shell_row.connect("notify::active", self.on_shell_toggled)
        free.add(self.shell_row)
        self.timeout_row = Adw.SpinRow.new_with_range(5, 3600, 5)
        self.timeout_row.set_title("Time limit")
        self.timeout_row.set_subtitle("Seconds before a running command is stopped")
        self.timeout_row.connect("notify::value", self.on_timeout_changed)
        free.add(self.timeout_row)
        page.add(free)
        return page

    def _command_rows(self, commands: list) -> list:
        if not commands:
            return [placeholder_row("No saved commands yet")]
        rows = []
        for c in commands:
            row = Adw.ActionRow(title=GLib.markup_escape_text(c["name"]),
                                subtitle=GLib.markup_escape_text(c["command"]), subtitle_selectable=True)
            row.add_suffix(icon_button("user-trash-symbolic", "Remove",
                                       lambda n=c["name"]: self.call("command_remove", name=n)))
            rows.append(row)
        return rows

    def add_command(self):
        name, line = self.cmd_name.get_text().strip(), self.cmd_line.get_text().strip()

        def done(_res):
            self.cmd_name.set_text("")
            self.cmd_line.set_text("")

        self.call("command_add", done, ok_toast=f"Added “{name}”", name=name, command=line)

    def on_shell_toggled(self, row, _pspec):
        if self._updating:
            return
        if not row.get_active():
            self.call("set", key="remote_shell", value=False)
            return
        dialog = Adw.AlertDialog(heading="Allow Any Command?",
                                 body="Your phone will be able to run any command on this computer as you. "
                                      "Keep a screen lock on the phone.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("allow", "Allow")
        dialog.set_response_appearance("allow", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_d, resp):
            if resp == "allow":
                self.call("set", key="remote_shell", value=True)
            else:
                self._updating = True
                row.set_active(False)
                self._updating = False

        dialog.connect("response", on_response)
        dialog.present(self)

    def on_timeout_changed(self, row, _pspec):
        if not self._updating:
            self.call("set", refresh=False, key="command_timeout", value=int(row.get_value()))

    def refresh_wol(self):
        self.daemon.call("wol_status", lambda res: res.get("ok") and self.wol.update(res["interfaces"], self._wol_rows))

    def _wol_rows(self, ifs: list) -> list:
        if not ifs:
            return [placeholder_row("No wired or Wi-Fi interface with an address")]
        rows = []
        for i in ifs:
            state = {True: "On", False: "Off", None: "Unknown"}[i.get("enabled")]
            kind = "Ethernet" if i["wired"] else "Wi-Fi (rarely supported)"
            row = Adw.ActionRow(title=GLib.markup_escape_text(f"{i['ifname']} · {kind}"),
                                subtitle=f"{i['mac']} · Wake-on-LAN {state}", subtitle_selectable=True)
            if i.get("enabled") is not True and i.get("connection"):
                row.add_suffix(text_button("Enable", lambda n=i["ifname"]: self.call(
                    "wol_enable", lambda _r: self.refresh_wol(), ok_toast="Wake-on-LAN enabled", ifname=n), "flat"))
            elif i.get("enabled") is True:
                row.add_suffix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic"))
            rows.append(row)
        return rows

    def _app_rows(self, data: dict) -> list:
        if not data["apps"]:
            return [placeholder_row("Apps appear here once the phone forwards a notification")]
        rows = []
        for a in data["apps"]:
            row = Adw.SwitchRow(title=GLib.markup_escape_text(a["name"]), subtitle=GLib.markup_escape_text(a["app"]),
                                active=a["app"] not in data["muted"])
            row.connect("notify::active", lambda r, _p, app=a["app"]: self._updating or self.call(
                "notif_mute", app=app, muted=not r.get_active()))
            rows.append(row)
        return rows

    # -- settings page ------------------------------------------------------------------

    def _settings_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()

        device = Adw.PreferencesGroup(title="This Computer")
        self.name_row = Adw.EntryRow(title="Name shown on your phone", show_apply_button=True)
        self.name_row.connect("apply", lambda r: r.get_text().strip() and self.call(
            "set", ok_toast="Renamed", key="name", value=r.get_text().strip()))
        device.add(self.name_row)
        self.id_row = Adw.ActionRow(title="Device ID", subtitle_selectable=True)
        self.id_row.add_css_class("property")
        device.add(self.id_row)
        page.add(device)

        clip = Adw.PreferencesGroup(title="Clipboard")
        self.clip_row = Adw.SwitchRow(title="Sync clipboard")
        self.clip_row.connect("notify::active", lambda r, _p: self._updating or self.call(
            "set", key="clipboard", value=r.get_active()))
        clip.add(self.clip_row)
        page.add(clip)

        notifs = Adw.PreferencesGroup(title="Phone Notifications",
                                      description="Needs notification access, granted in the phone app.")
        self.notif_row = Adw.SwitchRow(title="Show phone notifications here",
                                       subtitle="Reply from the desktop; dismissing one here dismisses it on the phone")
        self.notif_row.connect("notify::active", lambda r, _p: self._updating or self.call(
            "set", key="notifications", value=r.get_active()))
        notifs.add(self.notif_row)
        page.add(notifs)
        self.apps = ListGroup("Apps", "Turn off apps you don't want to see here.")
        page.add(self.apps.group)

        self.wol = ListGroup("Wake on LAN",
                             "Lets the phone switch this computer on. Also enable “Wake on LAN” in the BIOS/UEFI.")
        page.add(self.wol.group)

        service = Adw.PreferencesGroup(title="Background Service")
        self.service_row = Adw.ActionRow(title="Tether service")
        self.service_row.add_suffix(text_button("Restart", lambda: self._systemctl("restart"), "flat"))
        service.add(self.service_row)
        version = Adw.ActionRow(title="Version", subtitle=__version__)
        version.add_css_class("property")
        service.add(version)
        page.add(service)
        return page


class TetherApp(Adw.Application):
    def __init__(self, paths: Paths):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.paths = paths
        self.daemon: Daemon | None = None
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def do_activate(self):
        if self.daemon is None:
            self.daemon = Daemon(self.paths)
        win = self.get_active_window() or TetherWindow(self, self.daemon)
        win.present()


class ReplyApp(Adw.Application):
    """A small window for answering a phone notification from the desktop."""

    def __init__(self, paths: Paths, device: str, key: str, title: str):
        super().__init__(application_id="dev.tether.Reply", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.daemon = Daemon(paths, listen=False)
        self.device, self.key, self.title = device, key, title

    def do_activate(self):
        win = Adw.ApplicationWindow(application=self, title="Reply", default_width=420)
        toasts = Adw.ToastOverlay()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)
        heading = Gtk.Label(label=self.title, xalign=0, wrap=True, max_width_chars=48)
        heading.add_css_class("heading")
        entry = Gtk.Entry(placeholder_text="Message", activates_default=True, hexpand=True)
        send = Gtk.Button(label="Send", css_classes=["suggested-action"])
        row = Gtk.Box(spacing=8)
        row.append(entry)
        row.append(send)
        box.append(heading)
        box.append(row)
        toasts.set_child(box)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(toasts)
        win.set_content(view)
        win.set_default_widget(send)

        def do_send(*_):
            text = entry.get_text().strip()
            if not text:
                return
            send.set_sensitive(False)

            def done(res):
                if res.get("ok"):
                    win.close()
                else:
                    send.set_sensitive(True)
                    toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(friendly(res.get("error")))))

            self.daemon.call("notif_reply", done, device=self.device, key=self.key, text=text)

        send.connect("clicked", do_send)
        entry.connect("activate", do_send)
        win.present()
        entry.grab_focus()


def reply_main(paths: Paths, device: str, key: str, title: str) -> int:
    GLib.set_application_name("Tether")
    return ReplyApp(paths, device, key, title).run([])


def main(paths: Paths | None = None) -> int:
    GLib.set_application_name("Tether")
    return TetherApp(paths or Paths(os.environ.get("TETHER_HOME"))).run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
