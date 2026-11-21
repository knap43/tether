"""The Tether TUI: a Textual front end for terminals, doing what the window does.

Talks to the daemon over its control socket exactly like gui.py. Honours
TETHER_FEATURES (see gui.shown_features) to show only part of Tether.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DirectoryTree, Footer, Header, Input, Label, OptionList, ProgressBar,
                             Static, Switch, TabbedContent, TabPane)
from textual.widgets.option_list import Option

from . import __version__
from .config import Paths

FEATURES = ("clipboard", "files", "notifications", "media", "commands", "wol")


def shown_features() -> set[str]:
    raw = os.environ.get("TETHER_FEATURES", "").strip()
    if not raw:
        return set(FEATURES)
    return {f.strip().lower() for f in raw.split(",")} & set(FEATURES)


def friendly(error: str | None) -> str:
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
        return "Asleep" if d.get("asleep") else "Not connected"
    parts = ["Connected"]
    b = d.get("battery")
    if b:
        parts.append(f"Battery {b['level']}%{' · charging' if b['charging'] else ''}")
    if d.get("audio") == "on":
        parts.append("Playing its audio here")
    elif d.get("audio") == "starting":
        parts.append("Waiting for the phone to share audio…")
    if d.get("screen") == "on":
        parts.append("Showing its screen here")
    elif d.get("screen") == "starting":
        parts.append("Waiting for the phone to share its screen…")
    return " · ".join(parts)


def short_code(sas: str) -> str:
    return f"{sas[:3]} {sas[3:]}"


def esc(text) -> str:
    """Escapes Textual markup in text that comes from devices or paths."""
    return str(text).replace("[", r"\[")


def open_in_files(target: str) -> None:
    """Opens a folder or a dav:// URL in the file manager, detached from the terminal."""
    if target.startswith(("dav://", "davs://")):
        subprocess.run(["gio", "mount", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def read_clipboard() -> str | None:
    for cmd in (["wl-paste", "--no-newline", "--type", "text"], ["xclip", "-o", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if out.returncode == 0:
                return out.stdout
    return None


# -- daemon connection --------------------------------------------------------------


class Daemon:
    """The control socket. Each call uses its own connection, so long ones don't block others."""

    def __init__(self, paths: Paths):
        self.path = str(paths.socket)

    async def call(self, cmd: str, **kw) -> dict:
        try:
            reader, writer = await asyncio.open_unix_connection(self.path, limit=1 << 22)
        except OSError:
            return {"ok": False, "error": "The Tether service isn't running"}
        try:
            writer.write(json.dumps({"cmd": cmd, **kw}).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
            return json.loads(line or b'{"ok": false, "error": "The service closed the connection"}')
        except OSError:
            return {"ok": False, "error": "The Tether service isn't running"}
        except ValueError:
            return {"ok": False, "error": "Garbled answer from the service"}
        finally:
            writer.close()

    async def events(self):
        """Yields daemon events; returns when the connection drops."""
        reader, writer = await asyncio.open_unix_connection(self.path, limit=1 << 22)
        try:
            writer.write(b'{"cmd": "subscribe"}\n')
            await writer.drain()
            yield {"ev": "_connected"}
            while line := await reader.readline():
                yield json.loads(line)
        finally:
            writer.close()


# -- small widgets ----------------------------------------------------------------------


class ActButton(Button):
    """A button that runs a callback."""

    def __init__(self, label: str, fn, variant: str = "default", **kw):
        super().__init__(label, variant=variant, **kw)
        self.fn = fn

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.fn()


class ActSwitch(Switch):
    """A switch that runs a callback with its new value, unless set from status."""

    def __init__(self, value: bool, fn, **kw):
        super().__init__(value=value, **kw)
        self.fn = fn

    def set_quietly(self, value: bool) -> None:
        if self.value != value:
            with self.prevent(Switch.Changed):
                self.value = value

    def on_switch_changed(self, event: Switch.Changed) -> None:
        event.stop()
        self.fn(event.value)


class Row(Horizontal):
    """A line in a section: a title, a dim subtitle, and controls on the right."""

    def __init__(self, title: str, subtitle: str = "", *controls, dim: bool = False):
        super().__init__(classes="row")
        self.title, self.subtitle, self.controls, self.dim = title, subtitle, controls, dim

    def compose(self) -> ComposeResult:
        with Vertical(classes="row-text"):
            yield Label(self.title, classes="row-title dim" if self.dim else "row-title")
            if self.subtitle:
                yield Label(self.subtitle, classes="row-sub")
        yield from self.controls


class Section(Vertical):
    """A titled group whose rows are rebuilt only when their data changes."""

    def __init__(self, title: str, description: str = "", header: tuple = (), rows: tuple = (), **kw):
        super().__init__(classes="section", **kw)
        self.title, self.description, self.header = title, description, header
        self.signature = None
        self.body = Vertical(*rows, classes="section-body")

    def compose(self) -> ComposeResult:
        with Horizontal(classes="section-head"):
            with Vertical(classes="section-text"):
                yield Label(self.title, classes="section-title")
                if self.description:
                    yield Label(self.description, classes="section-desc")
            yield from self.header
        yield self.body

    def update(self, data, build) -> None:
        sig = json.dumps(data, sort_keys=True, default=str)
        if sig == self.signature:
            return
        self.signature = sig
        self.body.remove_children()
        self.body.mount(*build(data))


# -- dialogs ---------------------------------------------------------------------------


class Confirm(ModalScreen[bool]):
    def __init__(self, heading: str, body: str, yes: str = "OK", danger: bool = False):
        super().__init__()
        self.heading, self.body, self.yes, self.danger = heading, body, yes, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.heading, classes="dialog-title")
            yield Static(self.body, classes="dialog-body")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="no")
                yield Button(self.yes, id="yes", variant="error" if self.danger else "primary")

    def on_mount(self) -> None:
        self.query_one("#no" if self.danger else "#yes").focus()

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)


class Ask(ModalScreen[list | None]):
    """Asks for one or more lines of text."""

    def __init__(self, heading: str, body: str, fields: list[tuple[str, str]], ok: str = "OK"):
        super().__init__()
        self.heading, self.body, self.fields, self.ok = heading, body, fields, ok

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.heading, classes="dialog-title")
            if self.body:
                yield Static(self.body, classes="dialog-body")
            for placeholder, value in self.fields:
                yield Input(value=value, placeholder=placeholder)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="no")
                yield Button(self.ok, id="yes", variant="primary")

    @on(Input.Submitted)
    @on(Button.Pressed, "#yes")
    def done(self) -> None:
        self.dismiss([i.value.strip() for i in self.query(Input)])

    @on(Button.Pressed, "#no")
    def cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class PairDialog(ModalScreen[bool]):
    def __init__(self, name: str, sas: str):
        super().__init__()
        self.name_, self.sas = name, sas

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Pair with {esc(self.name_)}?", classes="dialog-title")
            yield Static("Make sure the other device shows the same code.", classes="dialog-body")
            yield Static(short_code(self.sas), classes="pair-code")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="no")
                yield Button("Codes Match", id="yes", variant="success")

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)


class Menu(ModalScreen[str | None]):
    """A list of actions; returns the chosen action's id."""

    def __init__(self, heading: str, options: list[tuple[str, str]]):
        super().__init__()
        self.heading, self.options = heading, options

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.heading, classes="dialog-title")
            yield OptionList(*[Option(label, id=key) for key, label in self.options])
            yield Label("Enter to choose · Esc to close", classes="dim")

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def key_escape(self) -> None:
        self.dismiss(None)


class PathPicker(ModalScreen[list | None]):
    """Picks files (Space marks several) or one folder, from a tree or by typing a path."""

    def __init__(self, heading: str, folders: bool = False, start: str | None = None):
        super().__init__()
        self.heading, self.folders = heading, folders
        self.start = Path(start or Path.home()).expanduser()
        self.marked: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog picker"):
            yield Label(self.heading, classes="dialog-title")
            hint = ("Pick a folder, or type its path." if self.folders else
                    "Enter adds a file · Space marks several · or type a path.")
            yield Label(hint, classes="dim")
            yield Input(value=str(self.start), placeholder="Path", id="path")
            yield DirectoryTree(str(self.start), id="tree")
            yield Label("", id="marked", classes="dim")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="no")
                yield Button("Choose" if self.folders else "Send", id="yes", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#tree").focus()

    def _show_marked(self) -> None:
        names = ", ".join(os.path.basename(p) for p in self.marked[:4])
        more = f" and {len(self.marked) - 4} more" if len(self.marked) > 4 else ""
        self.query_one("#marked", Label).update(f"Marked: {esc(names)}{more}" if self.marked else "")

    @on(DirectoryTree.FileSelected)
    def file_selected(self, event: DirectoryTree.FileSelected) -> None:
        if self.folders:
            self.query_one("#path", Input).value = str(event.path.parent)
            return
        path = str(event.path)
        if path not in self.marked:
            self.marked.append(path)
        self._show_marked()
        if len(self.marked) == 1 and not getattr(self, "_multi", False):
            self.dismiss(self.marked)

    @on(DirectoryTree.DirectorySelected)
    def dir_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.query_one("#path", Input).value = str(event.path)

    def key_space(self) -> None:
        if self.folders:
            return
        tree = self.query_one("#tree", DirectoryTree)
        node = tree.cursor_node
        if node is None or node.data is None or Path(node.data.path).is_dir():
            return
        self._multi = True
        path = str(node.data.path)
        self.marked.remove(path) if path in self.marked else self.marked.append(path)
        self._show_marked()

    @on(Input.Submitted, "#path")
    def typed(self) -> None:
        p = Path(self.query_one("#path", Input).value).expanduser()
        if p.is_dir():
            self.query_one("#tree", DirectoryTree).path = p
            if self.folders:
                self.dismiss([str(p)])
        elif p.is_file() and not self.folders:
            self.dismiss([str(p)])
        else:
            self.app.notify(f"{esc(p)} doesn't exist", severity="warning")

    @on(Button.Pressed, "#yes")
    def choose(self) -> None:
        if self.folders:
            p = Path(self.query_one("#path", Input).value).expanduser()
            self.dismiss([str(p)] if p.is_dir() else None)
        else:
            p = Path(self.query_one("#path", Input).value).expanduser()
            self.dismiss(self.marked or ([str(p)] if p.is_file() else None))

    @on(Button.Pressed, "#no")
    def cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


# -- the app -------------------------------------------------------------------------------


CSS = """
Screen { background: $surface; }
#down { display: none; background: $warning 30%; padding: 0 2; height: auto; }
#down.shown { display: block; }
TabPane { padding: 0 1; }
.section { height: auto; margin: 1 0 0 0; }
.section-head { height: auto; }
.section-text { width: 1fr; height: auto; }
.section-title { text-style: bold; }
.section-desc, .row-sub, .dim { color: $text-muted; }
.section-head Button { min-width: 8; }
.section-body { height: auto; border: round $panel-lighten-2; padding: 0 1; }
.row { height: auto; min-height: 3; align-vertical: middle; padding: 0 0; }
.row-text { width: 1fr; height: auto; padding: 0 1 0 0; }
.row-title.dim { color: $text-muted; }
.row Button { min-width: 6; margin: 0 0 0 1; }
.row Switch { margin: 0 0 0 1; }
.row > Label { margin: 1 0 0 1; }
.row ProgressBar { width: 24; margin: 0 1; }
.row Input { width: 32; }
ModalScreen { align: center middle; background: $background 60%; }
.dialog { width: 64; max-width: 95%; height: auto; max-height: 90%; border: thick $primary; background: $panel; padding: 1 2; }
.picker { width: 90; height: 36; }
.picker DirectoryTree { height: 1fr; border: round $panel-lighten-2; }
.dialog-title { text-style: bold; margin: 0 0 1 0; }
.dialog-body { margin: 0 0 1 0; }
.dialog OptionList { height: auto; max-height: 16; }
.dialog Input { margin: 0 0 1 0; }
.dialog-buttons { height: auto; align-horizontal: right; margin: 1 0 0 0; }
.dialog-buttons Button { margin: 0 0 0 1; }
.pair-code { text-style: bold; color: $success; width: 100%; content-align: center middle; height: 3; border: round $success; }
"""


class TetherTUI(App):
    TITLE = "Tether"
    CSS = CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "start_service", "Start service", show=False),
        Binding("1", "tab(0)", show=False), Binding("2", "tab(1)", show=False),
        Binding("3", "tab(2)", show=False), Binding("4", "tab(3)", show=False),
    ]

    def __init__(self, paths: Paths):
        super().__init__()
        self.paths = paths
        self.daemon = Daemon(paths)
        self.features = shown_features()
        self.status: dict | None = None
        self.up: bool | None = None
        self.pair_dialogs: set[str] = set()
        self.transfer_rows: dict = {}

    # -- layout ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("The Tether service isn't running — press [b]s[/b] to start it.", id="down")
        with TabbedContent(id="tabs"):
            with TabPane("Devices", id="devices"):
                yield from self._devices_page()
            if "files" in self.features:
                with TabPane("Folders", id="folders"):
                    yield from self._folders_page()
            if "commands" in self.features:
                with TabPane("Commands", id="commands"):
                    yield from self._commands_page()
            with TabPane("Settings", id="settings"):
                yield from self._settings_page()
        yield Footer()

    def _devices_page(self):
        with VerticalScroll():
            self.transfers = Section("Transfers")
            self.transfers.display = False
            yield self.transfers
            self.pending = Section("Pairing")
            self.pending.display = False
            yield self.pending
            self.paired = Section("Paired devices", "Enter on a device for what you can do with it.")
            yield self.paired
            self.nearby = Section("Nearby", "Devices running Tether on this network",
                                  header=(ActButton("Pair by Address…", self.ask_address),))
            yield self.nearby

    def _folders_page(self):
        with VerticalScroll():
            self.download_row = Row("Saved to", "", ActButton("Open", lambda: open_in_files(
                self.status["download_dir"])), ActButton("Change…", self.change_download_dir))
            yield Section("Received Files", rows=(self.download_row,))
            self.browse_list = Section("Your Devices' Files",
                                       "Open a device's shared folders in Files, or keep it in the Files sidebar.")
            yield self.browse_list
            self.shares = Section("Shared From This Computer", "Every paired device can browse and edit these folders.",
                                  header=(ActButton("Share a Folder…", self.add_share),))
            yield self.shares
            self.synced = Section("Synced Folders", "Kept identical on every device that syncs a folder with the same name.",
                                  header=(ActButton("Sync Now", lambda: self.call("rescan", ok="Folders rescanned")),
                                          ActButton("Sync a Folder…", self.add_sync)))
            yield self.synced

    def _commands_page(self):
        with VerticalScroll():
            self.saved_cmds = Section("Saved Commands", "Buttons your phone can press. They run as you, in your "
                                                        "login shell, in your home folder.")
            yield self.saved_cmds
            self.cmd_name = Input(placeholder="Name, e.g. Lock screen")
            self.cmd_line = Input(placeholder="Command line, e.g. loginctl lock-session")
            yield Section("Add a Command", rows=(self.cmd_name, self.cmd_line,
                                                 Horizontal(ActButton("Add", self.add_command, "primary"),
                                                            classes="row")))
            self.shell_switch = ActSwitch(False, self.on_shell_toggled)
            self.timeout_input = Input(type="integer", placeholder="120", id="timeout")
            yield Section("Free-Form Commands", rows=(
                Row("Allow any command from the phone", "Anyone holding your unlocked phone could run anything as "
                    "you. Each command still shows a notification here.", self.shell_switch),
                Row("Time limit", "Seconds before a running command is stopped (Enter to save)", self.timeout_input)))

    def _settings_page(self):
        with VerticalScroll():
            self.name_input = Input(placeholder="Name", id="name")
            self.id_row = Row("Device ID", "")
            yield Section("This Computer", rows=(Row("Name shown on your other devices", "Enter to save",
                                                     self.name_input), self.id_row))

            if "clipboard" in self.features:
                self.clip_switch = ActSwitch(False, lambda v: self.call("set", key="clipboard", value=v))
                self.clip_row = Row("Sync clipboard", "", self.clip_switch)
                yield Section("Clipboard", rows=(self.clip_row,))

            if "notifications" in self.features:
                self.notif_switch = ActSwitch(False, lambda v: self.call("set", key="notifications", value=v))
                yield Section("Phone Notifications", "Needs notification access, granted in the phone app.", rows=(
                    Row("Show phone notifications here", "Reply from the desktop; dismissing one here dismisses it "
                        "on the phone", self.notif_switch),))
                self.apps = Section("Apps", "Turn off apps you don't want to see here.")
                yield self.apps

            if "media" in self.features:
                self.audio_switch = ActSwitch(False, lambda v: self.call("set", key="phone_audio", value=v))
                self.screen_switch = ActSwitch(False, lambda v: self.call("set", key="phone_screen", value=v))
                self.play_switch = ActSwitch(False, lambda v: self.call("set", key="phone_play", value=v))
                self.remote_switch = ActSwitch(False, lambda v: self.call("set", key="phone_remote", value=v))
                self.movies_row = Row("Movies folder", "", ActButton("Open", lambda: open_in_files(
                    self.status["movies_dir"])), ActButton("Change…", self.change_movies_dir),
                    ActButton("Reset", lambda: self.call("set", ok="Movies folder reset", key="movies_dir", value="")))
                yield Section("Media", rows=(
                    Row("Play phone audio here", "", self.audio_switch),
                    Row("Show phone screen here", "", self.screen_switch),
                    Row("Play what the phone shares", "", self.play_switch),
                    Row("Let the phone use mouse and keyboard", "", self.remote_switch),
                    self.movies_row))

            if "wol" in self.features:
                self.wol = Section("Wake on LAN", "Lets the phone switch this computer on. Also enable “Wake on LAN” "
                                                  "in the BIOS/UEFI.")
                yield self.wol

            self.service_row = Row("Tether service", "", ActButton("Restart", lambda: self.systemctl("restart")))
            yield Section("Background Service", rows=(self.service_row, Row("Version", __version__)))

    # -- plumbing ----------------------------------------------------------------------

    def on_mount(self) -> None:
        self.listen()
        self.set_interval(3, self._poll)

    @work(exclusive=True, group="listen")
    async def listen(self) -> None:
        while True:
            try:
                async for ev in self.daemon.events():
                    if ev.get("ev") == "_connected":
                        self.set_up(True)
                    else:
                        self.on_daemon_event(ev)
            except (OSError, ValueError):
                pass
            self.set_up(False)
            await asyncio.sleep(2)

    def set_up(self, up: bool) -> None:
        if up == self.up:
            return
        self.up = up
        self.query_one("#down").set_class(not up, "shown")
        self.set_sub_row(self.service_row, "Running" if up else "Stopped")
        if up:
            self.action_refresh()

    def _poll(self) -> None:
        if self.up:
            self.action_refresh()

    def call(self, cmd: str, done=None, ok: str | None = None, refresh: bool = True, **kw) -> None:
        async def run():
            res = await self.daemon.call(cmd, **kw)
            if not res.get("ok"):
                self.notify(esc(friendly(res.get("error"))), severity="error")
            else:
                if ok:
                    self.notify(esc(ok))
                if done:
                    done(res)
            if refresh:
                self.action_refresh()

        self.run_worker(run(), group="calls")

    def action_refresh(self) -> None:
        async def run():
            res = await self.daemon.call("status")
            if res.get("ok"):
                self.apply_status(res)
            if "wol" in self.features:
                w = await self.daemon.call("wol_status")
                if w.get("ok"):
                    self.wol.update(w["interfaces"], self._wol_rows)

        self.run_worker(run(), group="refresh", exclusive=True)

    def action_tab(self, index: int) -> None:
        panes = [p.id for p in self.query(TabPane)]
        if index < len(panes):
            self.query_one("#tabs", TabbedContent).active = panes[index]

    def action_start_service(self) -> None:
        if not self.up:
            self.systemctl("start")

    def systemctl(self, verb: str) -> None:
        try:
            subprocess.Popen(["systemctl", "--user", verb, "tether.service"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.notify(f"{'Starting' if verb == 'start' else 'Restarting'} the service…")
        except OSError as e:
            self.notify(f"Couldn't run systemctl: {esc(e)}", severity="error")

    def set_sub_row(self, row: Row, text: str) -> None:
        row.subtitle = text
        subs = row.query(".row-sub")
        if subs:
            subs.first(Label).update(esc(text))
        elif row.is_mounted and text:
            row.query_one(".row-text").mount(Label(esc(text), classes="row-sub"))

    def device_name(self, device_id: str) -> str:
        for d in (self.status or {}).get("devices", []):
            if d["id"] == device_id:
                return d["name"]
        return "the device"

    # -- events and status -------------------------------------------------------------

    def on_daemon_event(self, ev: dict) -> None:
        kind = ev.get("ev")
        if kind == "status":
            self.apply_status(ev)
        elif kind == "pair_request":
            if not ev.get("initiator"):
                self.show_pair_dialog(ev["id"], ev["name"], ev["sas"])
            self.action_refresh()
        elif kind == "paired":
            self._close_pair_dialog(ev.get("id"))
            self.notify(f"Paired with {esc(ev.get('name', 'the device'))}")
            self.action_refresh()
        elif kind == "pair_failed":
            self._close_pair_dialog(ev.get("id"))
            self.notify("Pairing timed out" if ev.get("reason") == "timeout" else "Pairing was cancelled",
                        severity="warning")
            self.action_refresh()
        elif kind == "transfer":
            self.update_transfer(ev)
        elif kind == "file_received":
            self.notify(f"Received {esc(Path(ev.get('path', '')).name)}")

    def apply_status(self, st: dict) -> None:
        self.status = st
        audio_ok = bool(st.get("phone_audio", True) and st.get("phone_audio_available", True))
        screen_ok = bool(st.get("phone_screen", True) and st.get("phone_screen_available", True))
        self.paired.update([{**d, "audio_ok": audio_ok, "screen_ok": screen_ok} for d in st["devices"]],
                           self._device_rows)
        self.pending.update(st["pending"], self._pending_rows)
        self.pending.display = bool(st["pending"])
        self.nearby.update(st["discovered"], self._nearby_rows)
        for t in st.get("transfers", []):
            self.update_transfer(t)
        if "files" in self.features:
            self.browse_list.update(
                [{k: d.get(k) for k in ("id", "name", "type", "connected", "asleep", "sidebar")} for d in st["devices"]],
                self._browse_rows)
            self.shares.update(st["shares"], self._share_rows)
            self.synced.update(st["sync"], self._sync_rows)
            self.set_sub_row(self.download_row, st["download_dir"])
        if "commands" in self.features:
            self.saved_cmds.update(st["commands"], self._command_rows)
            self.shell_switch.set_quietly(bool(st["remote_shell"]))
            if not self.timeout_input.has_focus:
                self.timeout_input.value = str(int(st.get("command_timeout", 120)))
        if "clipboard" in self.features:
            self.clip_switch.set_quietly(bool(st["clipboard"]))
            self.set_sub_row(self.clip_row, "Uses the Tether shell extension" if st["clipboard_backend"] == "gnome-extension"
                             else f"Uses {st['clipboard_backend']}")
        if "notifications" in self.features:
            self.notif_switch.set_quietly(bool(st.get("notifications", True)))
            self.apps.update({"apps": st.get("notif_apps", []), "muted": st.get("notif_muted", [])}, self._app_rows)
        if "media" in self.features:
            self.audio_switch.set_quietly(bool(st.get("phone_audio", True)))
            self.screen_switch.set_quietly(bool(st.get("phone_screen", True)))
            self.play_switch.set_quietly(bool(st.get("phone_play", True)))
            self.remote_switch.set_quietly(bool(st.get("phone_remote", False)))
            movies = st.get("movies_dir", "")
            self.set_sub_row(self.movies_row, movies if Path(movies).is_dir() else f"{movies} (not found)")
        if not self.name_input.has_focus and self.name_input.value != st["name"]:
            self.name_input.value = st["name"]
        self.set_sub_row(self.id_row, " ".join(re.findall("....", st["id"][:32])))
        for p in st["pending"]:
            if not p["confirmed"] and not p["initiator"] and p["id"] not in self.pair_dialogs:
                self.show_pair_dialog(p["id"], p["name"], p["sas"])

    # -- devices --------------------------------------------------------------------------

    def _device_rows(self, devices: list) -> list:
        if not devices:
            return [Row("No paired devices yet — pair one below", dim=True)]
        rows = []
        for d in devices:
            kind = "Phone" if d.get("type") == "phone" else "Computer"
            rows.append(Row(f"{esc(d['name'])}  [dim]{kind}[/dim]", esc(device_subtitle(d)),
                            ActButton("Actions…", lambda i=d["id"]: self.device_menu(i),
                                      "primary" if d["connected"] else "default")))
        return rows

    def _pending_rows(self, pending: list) -> list:
        rows = []
        for p in pending:
            title, sub = f"Pairing with {esc(p['name'])}", f"Code {short_code(p['sas'])}"
            if p["confirmed"]:
                rows.append(Row(title, sub + " · waiting for the other device…"))
            else:
                rows.append(Row(title, sub, ActButton("Reject", lambda i=p["id"]: self.answer_pairing(i, False)),
                                ActButton("Codes Match", lambda i=p["id"]: self.answer_pairing(i, True), "success")))
        return rows

    def _nearby_rows(self, found: list) -> list:
        if not found:
            return [Row("Looking for devices…", "Open Tether on your phone; it appears here within a few seconds",
                        dim=True)]
        return [Row(esc(d["name"]), esc(d.get("addr", "")),
                    ActButton("Pair", lambda i=d["id"]: self.start_pairing(device=i), "success")) for d in found]

    def device_menu(self, device_id: str) -> None:
        d = next((d for d in (self.status or {}).get("devices", []) if d["id"] == device_id), None)
        if d is None:
            return
        files, media = "files" in self.features, "media" in self.features
        opts = []
        if d["connected"]:
            if files:
                opts += [("send", "Send files…"), ("browse", "Browse its files")]
            if "clipboard" in self.features:
                opts.append(("clip", "Send clipboard"))
            if "notifications" in self.features:
                opts.append(("notify", "Send notification…"))
            if media and d.get("type") == "phone":
                audio_on = d.get("audio") in ("on", "starting")
                screen_on = d.get("screen") in ("on", "starting")
                opts.append(("audio", "Stop phone audio" if audio_on else "Play phone audio here"))
                opts.append(("screen", "Stop showing its screen" if screen_on else "Show its screen here"))
        if files:
            opts.append(("sidebar", "Remove from Files sidebar" if d.get("sidebar") else "Show in Files sidebar"))
        opts.append(("unpair", "Unpair…"))

        def chosen(key: str | None) -> None:
            if key == "send":
                self.pick_and_send(device_id)
            elif key == "browse":
                self.browse(device_id)
            elif key == "clip":
                self.send_clipboard()
            elif key == "notify":
                self.ask_notification(device_id)
            elif key == "audio":
                self.call("phone_audio", device=device_id, on=d.get("audio") not in ("on", "starting"))
            elif key == "screen":
                self.call("phone_screen", device=device_id, on=d.get("screen") not in ("on", "starting"))
            elif key == "sidebar":
                on_ = not d.get("sidebar")
                self.call("sidebar", device=device_id, on=on_,
                          ok="Added to the Files sidebar" if on_ else "Removed from the Files sidebar")
            elif key == "unpair":
                self.confirm_unpair(device_id)

        self.push_screen(Menu(esc(d["name"]), opts), chosen)

    def start_pairing(self, device: str | None = None, addr: str | None = None) -> None:
        kw = {"device": device} if device else {"addr": addr}
        self.call("pair", lambda res: self.show_pair_dialog(res["device"], res["name"], res["sas"]), **kw)

    def ask_address(self) -> None:
        def then(values):
            if values and values[0]:
                self.start_pairing(addr=values[0])

        self.push_screen(Ask("Pair by Address", "Enter the phone's IP address, shown in the Tether app.",
                             [("192.168.1.42", "")], "Pair"), then)

    def show_pair_dialog(self, device_id: str, name: str, sas: str) -> None:
        if device_id in self.pair_dialogs:
            return
        self.pair_dialogs.add(device_id)

        def then(accept: bool) -> None:
            if device_id in self.pair_dialogs:  # not already closed because pairing finished elsewhere
                self.pair_dialogs.discard(device_id)
                self.call("pair_confirm", device=device_id, accept=bool(accept))

        dialog = PairDialog(name, sas)
        dialog.device_id = device_id
        self.push_screen(dialog, then)

    def _close_pair_dialog(self, device_id) -> None:
        if device_id in self.pair_dialogs:
            self.pair_dialogs.discard(device_id)
            for screen in self.screen_stack:
                if isinstance(screen, PairDialog) and getattr(screen, "device_id", None) == device_id:
                    screen.dismiss(False)

    def answer_pairing(self, device_id: str, accept: bool) -> None:
        self._close_pair_dialog(device_id)
        self.call("pair_confirm", device=device_id, accept=accept)

    def pick_and_send(self, device_id: str) -> None:
        def then(paths):
            files = [p for p in (paths or []) if os.path.isfile(p)]
            if paths and not files:
                self.notify("Only files can be sent, not folders", severity="warning")
            if files:
                n = len(files)
                noun = os.path.basename(files[0]) if n == 1 else f"{n} files"
                self.notify(f"Sending {esc(noun)}…")
                self.call("send", ok=f"Sent {noun}", refresh=False, device=device_id, paths=files)

        self.push_screen(PathPicker(f"Send to {esc(self.device_name(device_id))}"), then)

    def send_clipboard(self) -> None:
        text = read_clipboard()
        if not text:
            self.notify("The clipboard has no text", severity="warning")
            return
        self.call("clip_send", ok="Clipboard sent", refresh=False, text=text)

    def ask_notification(self, device_id: str) -> None:
        def then(values):
            if values and values[0]:
                self.call("notify", ok="Notification sent", refresh=False, device=device_id,
                          title=values[0], text=values[1])

        self.push_screen(Ask(f"Notify {esc(self.device_name(device_id))}", "",
                             [("Title", ""), ("Message (optional)", "")], "Send"), then)

    def confirm_unpair(self, device_id: str) -> None:
        name = self.device_name(device_id)
        self.push_screen(Confirm(f"Unpair {esc(name)}?", "It will no longer connect until you pair again.",
                                 "Unpair", danger=True),
                         lambda yes: yes and self.call("unpair", ok=f"Unpaired {name}", device=device_id))

    def browse(self, device_id: str | None = None) -> None:
        if not self.status:
            return
        d = next((d for d in self.status["devices"] if d["id"] == device_id), None)
        url = d["dav_url"] if d and d.get("dav_url") else self.status["dav_url"]
        self.notify("Opening in Files…")
        self.run_worker(asyncio.to_thread(open_in_files, url), group="open")

    # -- transfers -------------------------------------------------------------------------

    def update_transfer(self, t: dict) -> None:
        tid = t["id"]
        entry = self.transfer_rows.get(tid)
        if entry is None:
            if t["state"] != "active":
                return
            arrow = "To" if t["direction"] == "out" else "From"
            bar = ProgressBar(total=None, show_eta=False)
            row = Row(esc(t["name"]), "", bar, ActButton("Cancel", lambda: self.call(
                "transfer_cancel", refresh=False, id=tid)))
            entry = self.transfer_rows[tid] = (row, bar, f"{arrow} {t['peer_name']}")
            self.transfers.body.mount(row)
            self.transfers.display = True
        row, bar, who = entry
        total = t.get("total") or 0
        if total:
            bar.update(total=total, progress=min(t["done"], total))
            self.set_sub_row(row, f"{who} · {human(t['done'])} of {human(total)}")
        else:
            self.set_sub_row(row, f"{who} · {human(t['done'])}")
        if t["state"] != "active":
            if t["state"] == "failed":
                self.notify(f"Couldn't transfer {esc(t['name'])}", severity="error")
            elif t["state"] == "cancelled":
                self.notify(f"Cancelled {esc(t['name'])}", severity="warning")
            self.set_timer(1.2 if t["state"] == "done" else 0.01, lambda: self._remove_transfer(tid))

    def _remove_transfer(self, tid) -> None:
        entry = self.transfer_rows.pop(tid, None)
        if entry:
            entry[0].remove()
        self.transfers.display = bool(self.transfer_rows)

    # -- folders ---------------------------------------------------------------------------

    def _browse_rows(self, devices: list) -> list:
        if not devices:
            return [Row("Pair a device to browse its files", dim=True)]
        rows = []
        for d in devices:
            online = d["connected"]
            sub = "Asleep" if d.get("asleep") else "Connected" if online else "Not connected"
            side = ActSwitch(bool(d.get("sidebar")), lambda v, i=d["id"]: self.call(
                "sidebar", device=i, on=v, ok="Added to the Files sidebar" if v else "Removed from the Files sidebar"))
            controls = [Label("Sidebar", classes="dim"), side]
            if online:
                controls.append(ActButton("Open", lambda i=d["id"]: self.browse(i)))
            rows.append(Row(esc(d["name"]), sub, *controls))
        return rows

    def _share_rows(self, shares: dict) -> list:
        if not shares:
            return [Row("Nothing shared", dim=True)]
        return [Row(esc(name), esc(path), ActButton("Stop Sharing", lambda n=name: self.call("share_remove", name=n),
                                                    "error")) for name, path in shares.items()]

    def _sync_rows(self, folders: list) -> list:
        if not folders:
            return [Row("No synced folders", dim=True)]
        return [Row(esc(f["id"]), esc(f["path"]), ActButton("Stop Syncing", lambda i=f["id"]: self.call(
            "sync_remove", folder=i), "error")) for f in folders]

    def change_download_dir(self) -> None:
        self.push_screen(PathPicker("Save Received Files In", folders=True, start=(self.status or {}).get("download_dir")),
                         lambda p: p and self.call("set", key="download_dir", value=p[0]))

    def change_movies_dir(self) -> None:
        self.push_screen(PathPicker("Play Movies From", folders=True, start=(self.status or {}).get("movies_dir")),
                         lambda p: p and self.call("set", key="movies_dir", value=p[0]))

    def add_share(self) -> None:
        def then(p):
            if not p:
                return
            path = p[0]
            base = os.path.basename(path.rstrip("/")) or "Files"
            name, i = base, 2
            while self.status and name in self.status["shares"]:
                name, i = f"{base} {i}", i + 1
            self.call("share_add", ok=f"Sharing {name}", name=name, path=path)

        self.push_screen(PathPicker("Share a Folder", folders=True), then)

    def add_sync(self) -> None:
        def picked(p):
            if not p:
                return
            path = p[0]
            suggested = re.sub(r"[^a-z0-9_-]+", "-", os.path.basename(path.rstrip("/")).lower()).strip("-") or "folder"

            def named(values):
                fid = re.sub(r"[^a-z0-9_-]+", "-", (values or [""])[0].lower()).strip("-")
                if values and fid:
                    self.call("sync_add", ok=f"Syncing “{fid}”", folder=fid, path=path)

            self.push_screen(Ask("Name This Folder", f"{esc(path)}\n\nOn each device that should share it, add a "
                                                     "folder with this same name.", [("Name", suggested)], "Sync"), named)

        self.push_screen(PathPicker("Sync a Folder", folders=True), picked)

    # -- commands --------------------------------------------------------------------------

    def _command_rows(self, commands: list) -> list:
        if not commands:
            return [Row("No saved commands yet", dim=True)]
        return [Row(esc(c["name"]), esc(c["command"]), ActButton("Remove", lambda n=c["name"]: self.call(
            "command_remove", name=n), "error")) for c in commands]

    def add_command(self) -> None:
        name, line = self.cmd_name.value.strip(), self.cmd_line.value.strip()
        if not (name and line):
            self.notify("Give the command a name and a command line", severity="warning")
            return

        def done(_res):
            self.cmd_name.value = ""
            self.cmd_line.value = ""

        self.call("command_add", done, ok=f"Added “{name}”", name=name, command=line)

    def on_shell_toggled(self, value: bool) -> None:
        if not value:
            self.call("set", key="remote_shell", value=False)
            return

        def then(yes: bool) -> None:
            if yes:
                self.call("set", key="remote_shell", value=True)
            else:
                self.shell_switch.set_quietly(False)

        self.push_screen(Confirm("Allow Any Command?", "Your phone will be able to run any command on this computer "
                                 "as you. Keep a screen lock on the phone.", "Allow", danger=True), then)

    @on(Input.Submitted, "#timeout")
    def timeout_submitted(self, event: Input.Submitted) -> None:
        try:
            value = max(5, min(3600, int(event.value)))
        except ValueError:
            return
        self.call("set", refresh=False, ok="Time limit saved", key="command_timeout", value=value)

    @on(Input.Submitted, "#name")
    def name_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.call("set", ok="Renamed", key="name", value=event.value.strip())

    # -- settings lists -------------------------------------------------------------------

    def _wol_rows(self, ifs: list) -> list:
        if not ifs:
            return [Row("No wired or Wi-Fi interface with an address", dim=True)]
        rows = []
        for i in ifs:
            state = {True: "On", False: "Off", None: "Unknown"}[i.get("enabled")]
            kind = "Ethernet" if i["wired"] else "Wi-Fi (rarely supported)"
            controls = []
            if i.get("enabled") is not True and i.get("connection"):
                controls.append(ActButton("Enable", lambda n=i["ifname"]: self.call(
                    "wol_enable", ok="Wake-on-LAN enabled", ifname=n)))
            rows.append(Row(f"{esc(i['ifname'])} · {kind}", f"{i['mac']} · Wake-on-LAN {state}", *controls))
        return rows

    def _app_rows(self, data: dict) -> list:
        if not data["apps"]:
            return [Row("Apps appear here once the phone forwards a notification", dim=True)]
        return [Row(esc(a["name"]), esc(a["app"]), ActSwitch(a["app"] not in data["muted"], lambda v, app=a["app"]:
                                                             self.call("notif_mute", app=app, muted=not v)))
                for a in data["apps"]]


def main(paths: Paths | None = None) -> int:
    TetherTUI(paths or Paths(os.environ.get("TETHER_HOME"))).run()
    return 0
