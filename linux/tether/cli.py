"""Command-line interface: `tether daemon` plus commands that talk to it."""

import argparse
import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

from .config import Paths


def die(msg: str, code: int = 1):
    print(f"tether: {msg}", file=sys.stderr)
    sys.exit(code)


class Ctl:
    def __init__(self, paths: Paths):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(str(paths.socket))
        except OSError:
            die("the daemon is not running (systemctl --user start tether)")
        self.f = self.sock.makefile("rwb")

    def call(self, cmd: str, **kw) -> dict:
        self.f.write(json.dumps({"cmd": cmd, **kw}).encode() + b"\n")
        self.f.flush()
        res = json.loads(self.f.readline() or b'{"ok": false, "error": "daemon closed the connection"}')
        if not res.get("ok"):
            die(res.get("error", "failed"))
        return res

    def events(self):
        self.f.write(b'{"cmd": "subscribe"}\n')
        self.f.flush()
        for line in self.f:
            yield json.loads(line)


def short(d: dict) -> str:
    return f"{d['name']} [{d['id'][:8]}]"


def cmd_status(paths, args):
    st = Ctl(paths).call("status")
    if args.json:
        print(json.dumps(st, indent=2))
        return
    print(f"This device: {st['name']} [{st['id'][:8]}]")
    print(f"Clipboard:   {'on' if st['clipboard'] else 'off'} ({st['clipboard_backend']})")
    print(f"Media:       {'on' if st.get('media', True) else 'off'} (the phone controls players here)")
    print(f"Phone audio: {'on' if st.get('phone_audio', True) else 'off'} (the phone may play its sound here)")
    print(f"Downloads:   {st['download_dir']}")
    print("Paired devices:")
    for d in st["devices"] or []:
        audio = "  · playing audio here" if d.get("audio") == "on" else ""
        print(f"  {'●' if d['connected'] else '○'} {short(d)}{'  connected' if d['connected'] else ''}{audio}")
    if not st["devices"]:
        print("  (none — run `tether pair`)")
    if st["pending"]:
        print("Pairing in progress:")
        for p in st["pending"]:
            print(f"  {short(p)}  code {p['sas']}")
    if st["discovered"]:
        print("Unpaired devices nearby:")
        for d in st["discovered"]:
            print(f"  {short(d)}  {d['addr']}")
    print("Shared for browsing:", ", ".join(f"{k} → {v}" for k, v in st["shares"].items()) or "nothing")
    print("Synced folders:", ", ".join(f"{f['id']} → {f['path']}" for f in st["sync"]) or "none")
    print("Phone commands:", ", ".join(c["name"] for c in st["commands"]) or "none",
          "| free-form shell", "ON" if st["remote_shell"] else "off")


def _wait_paired(paths, device_id) -> bool:
    for ev in Ctl(paths).events():
        if ev.get("id") != device_id:
            continue
        if ev.get("ev") == "paired":
            return True
        if ev.get("ev") == "pair_failed":
            print(f"Pairing failed: {ev.get('reason')}")
            return False
    return False


def cmd_pair(paths, args):
    ctl = Ctl(paths)
    if args.addr:
        res = ctl.call("pair", addr=args.addr)
    else:
        found = ctl.call("status")["discovered"]
        if args.device:
            found = [d for d in found if d["id"].startswith(args.device.lower()) or d["name"] == args.device]
        if not found:
            die("no unpaired device found; open Tether on the phone, or use --addr IP")
        if len(found) > 1:
            for i, d in enumerate(found, 1):
                print(f"  {i}. {short(d)}  {d['addr']}")
            try:
                d = found[int(input("Pair with which device? ")) - 1]
            except (ValueError, IndexError, EOFError):
                die("cancelled")
        else:
            d = found[0]
        res = ctl.call("pair", device=d["id"])
    _confirm(paths, ctl, res["device"], res["name"], res["sas"])


def _confirm(paths, ctl, device_id, name, sas):
    print(f"\nPairing with {name}.\nCheck that both screens show the code:\n\n        {sas[:3]} {sas[3:]}\n")
    try:
        ok = input("Do the codes match? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        ok = False
    ctl.call("pair_confirm", device=device_id, accept=ok)
    if not ok:
        print("Rejected.")
        return
    print("Waiting for the other device to confirm…")
    if _wait_paired(paths, device_id):
        print(f"Paired with {name}.")


def cmd_accept(paths, args):
    ctl = Ctl(paths)
    pending = [p for p in ctl.call("status")["pending"] if not p["confirmed"]]
    if args.device:
        pending = [p for p in pending if p["id"].startswith(args.device.lower()) or p["name"] == args.device]
    if not pending:
        die("no pairing request waiting")
    p = pending[0]
    _confirm(paths, ctl, p["id"], p["name"], p["sas"])


def cmd_unpair(paths, args):
    Ctl(paths).call("unpair", device=args.device)
    print("Unpaired.")


def pick_files() -> list[str]:
    """Ask for files with the GTK 4 file dialog (portal-backed), falling back to zenity."""
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        picked: list[str] = []
        app = Gtk.Application(application_id="dev.tether.Picker")

        def done(dialog, res):
            try:
                files = dialog.open_multiple_finish(res)
                for i in range(files.get_n_items()):
                    path = files.get_item(i).get_path()
                    if path:
                        picked.append(path)
            except GLib.Error:
                pass
            app.release()

        def activate(app):
            app.hold()
            Gtk.FileDialog(title="Send to phone").open_multiple(None, None, done)

        app.connect("activate", activate)
        app.run([])
        return picked
    except (ImportError, ValueError, AttributeError):
        pass
    if shutil.which("zenity"):
        out = subprocess.run(["zenity", "--file-selection", "--multiple", "--separator=\n",
                              "--title=Send to phone"], capture_output=True, text=True)
        return [l for l in out.stdout.splitlines() if l]
    die("no file dialog available (install python-gobject and gtk4, or zenity)")


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Tether", title, body], check=False)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def _show_progress(paths):
    try:
        for ev in Ctl(paths).events():
            if ev.get("ev") != "transfer" or ev.get("direction") != "out" or ev.get("state") != "active":
                continue
            total = ev.get("total") or 0
            pct = f"{100 * ev['done'] / total:3.0f}%" if total else ""
            bar = "█" * int(24 * ev["done"] / total) if total else ""
            print(f"\r\033[K{ev['name'][:30]}  {bar:<24} {pct} {human(ev['done'])} / {human(total)}", end="", flush=True)
    except (OSError, ValueError, SystemExit):
        pass


def cmd_send(paths, args):
    quiet = args.pick or args.notify  # started from Files or a menu: report by notification
    if args.pick:
        args.files = pick_files()
        if not args.files:
            return
    if not args.files:
        die("nothing to send")
    files = [os.path.abspath(p) for p in args.files]
    folders = [p for p in files if not os.path.isfile(p)]
    if folders:
        msg = f"{os.path.basename(folders[0]) or folders[0]} is a folder" if os.path.isdir(folders[0]) \
            else f"{folders[0]} is not a file"
        if quiet:
            notify("Nothing sent", msg + "; Tether sends files only")
            return
        die(msg)
    if quiet:
        ctl = Ctl(paths)
        ctl.f.write(json.dumps({"cmd": "send", "device": args.device, "paths": files}).encode() + b"\n")
        ctl.f.flush()
        res = json.loads(ctl.f.readline() or b"{}")
        if res.get("ok"):
            to = res.get("device_name") or "the device"
            notify(f"Sent to {to}", ", ".join(os.path.basename(p) for p in files))
        else:
            notify("Sending failed", res.get("error", "unknown error"))
        return
    if sys.stdout.isatty():
        threading.Thread(target=_show_progress, args=(paths,), daemon=True).start()
    res = Ctl(paths).call("send", device=args.device, paths=files)
    print("\r\033[K", end="")
    for s in res["sent"]:
        print(f"sent {s['path']} → {s['saved_as']}")


def cmd_clip(paths, args):
    text = sys.stdin.read() if args.text in (None, "-") else args.text
    Ctl(paths).call("clip_send", text=text)


def cmd_share(paths, args):
    ctl = Ctl(paths)
    if args.action == "add":
        ctl.call("share_add", name=args.name, path=os.path.abspath(os.path.expanduser(args.path)))
    elif args.action == "remove":
        ctl.call("share_remove", name=args.name)
    for k, v in ctl.call("status")["shares"].items():
        print(f"{k} → {v}")


def cmd_sync(paths, args):
    ctl = Ctl(paths)
    if args.action == "add":
        ctl.call("sync_add", folder=args.folder, path=os.path.abspath(os.path.expanduser(args.path)))
    elif args.action == "remove":
        ctl.call("sync_remove", folder=args.folder)
    elif args.action == "now":
        ctl.call("rescan")
    for f in ctl.call("status")["sync"]:
        print(f"{f['id']} → {f['path']}")


def cmd_notify(paths, args):
    res = Ctl(paths).call("notify", device=args.device, title=args.title, text=args.text or "")
    print("notified " + ", ".join(res["sent"]))


def cmd_audio(paths, args):
    if args.state == "volume":
        if args.level is None:
            print(f"{round(Ctl(paths).call('phone_audio_volume', volume=None)['volume'] * 100)}%")
            return
        try:
            level = float(args.level.rstrip("%"))
        except ValueError:
            die("volume is a percentage, 0–100")
        res = Ctl(paths).call("phone_audio_volume", volume=level / 100)
        print(f"{round(res['volume'] * 100)}%")
        return
    Ctl(paths).call("phone_audio", device=args.device, on=args.state == "on")
    if args.state == "on":
        print("asked the phone to stream its audio here — accept on the phone if it asks")


def cmd_command(paths, args):
    ctl = Ctl(paths)
    if args.action == "add":
        if not args.name or not args.command:
            die("usage: tether command add NAME 'COMMAND LINE'")
        line = args.command[0] if len(args.command) == 1 else shlex.join(args.command)
        ctl.call("command_add", name=args.name, command=line)
    elif args.action == "remove":
        if not args.name:
            die("usage: tether command remove NAME")
        ctl.call("command_remove", name=args.name)
    st = ctl.call("status")
    for c in st["commands"]:
        print(f"{c['name']}: {c['command']}")
    if not st["commands"]:
        print("(no saved commands)")
    print(f"Free-form shell from the phone: {'ON' if st['remote_shell'] else 'off'} (tether set remote_shell on|off)")


def cmd_wol(paths, args):
    ctl = Ctl(paths)
    if args.action == "enable":
        ifs = ctl.call("wol_status")["interfaces"]
        name = args.ifname or next((i["ifname"] for i in ifs if i["wired"]), None) or (ifs[0]["ifname"] if ifs else None)
        if not name:
            die("no network interface found")
        res = ctl.call("wol_enable", ifname=name)
        print(f"Wake-on-LAN enabled on {name} (connection “{res['connection']}”).")
    ifs = ctl.call("wol_status")["interfaces"]
    if not ifs:
        print("No physical network interface with an address.")
    for i in ifs:
        state = {True: "enabled", False: "off", None: "unknown"}[i.get("enabled")]
        kind = "wired" if i["wired"] else "Wi-Fi (wake rarely works)"
        print(f"{i['ifname']:<10} {i['mac']}  {i['ip']:<15} {kind:<26} wake-on-LAN: {state}")
    print("\nAlso enable “Wake on LAN” / “Power on by PCI-E” in the BIOS/UEFI. The phone learns these "
          "addresses whenever it connects.")


def cmd_set(paths, args):
    value = args.value
    if value.lower() in ("on", "true", "yes"):
        value = True
    elif value.lower() in ("off", "false", "no"):
        value = False
    elif value.isdigit():
        value = int(value)
    Ctl(paths).call("set", key=args.key, value=value)


def cmd_sidebar(paths, args):
    ctl = Ctl(paths)
    if args.action in ("add", "remove"):
        res = ctl.call("sidebar", device=args.device, on=args.action == "add")
    else:
        res = ctl.call("sidebar")
    for d in res["devices"]:
        print(d["name"])
    if not res["devices"]:
        print("No devices in the Files sidebar.")


def cmd_mount(paths, args):
    st = Ctl(paths).call("status")
    url = st["dav_url"]
    if args.device:
        want = args.device.lower()
        d = next((d for d in st["devices"] if d["name"].lower() == want or d["id"].startswith(want)), None)
        if d is None:
            sys.exit(f"no paired device called {args.device}")
        if not d.get("dav_url") and not args.bookmark:
            sys.exit(f"{d['name']} isn't connected")
        url = d.get("dav_url") or url
    if args.bookmark:
        phones = [d for d in st["devices"] if d.get("type") == "phone"]
        target = d if args.device else (phones[0] if phones else None)
        if target is None:
            print("Pair a phone first; it then appears in the Files sidebar by itself.")
        else:
            Ctl(paths).call("sidebar", device=target["id"], on=True)
            print(f"Added “{target['name']}” to the Files sidebar.")
    subprocess.run(["gio", "mount", url], check=False)
    if not args.no_open and shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(url)


def cmd_gui(paths, args):
    from .gui import main as gui_main

    sys.exit(gui_main(paths))


def cmd_reply(paths, args):
    from .gui import reply_main

    sys.exit(reply_main(paths, args.device, args.key, args.title or "Reply"))


def cmd_daemon(paths, args):
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    from .control import ControlServer
    from .node import Node

    async def main():
        node = Node(paths)
        await node.start(with_dav=not args.no_dav, with_discovery=not args.no_discovery)
        await ControlServer(node).start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()

    asyncio.run(main())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tether", description="Phone ⇄ computer files and clipboard")
    ap.add_argument("--home", default=os.environ.get("TETHER_HOME"), help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("daemon", help="run the background service")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--no-dav", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-discovery", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_daemon)

    p = sub.add_parser("gui", help="open the Tether window")
    p.set_defaults(fn=cmd_gui)

    p = sub.add_parser("reply", help=argparse.SUPPRESS)
    p.add_argument("--device", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--title")
    p.set_defaults(fn=cmd_reply)

    p = sub.add_parser("status", help="show devices and settings")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("pair", help="pair with a device on the network")
    p.add_argument("device", nargs="?", help="name or id prefix")
    p.add_argument("--addr", help="pair by address (IP or IP:PORT)")
    p.set_defaults(fn=cmd_pair)

    p = sub.add_parser("accept", help="answer a pairing request started on the phone")
    p.add_argument("device", nargs="?")
    p.set_defaults(fn=cmd_accept)

    p = sub.add_parser("unpair", help="forget a paired device")
    p.add_argument("device")
    p.set_defaults(fn=cmd_unpair)

    p = sub.add_parser("send", help="send files to the phone")
    p.add_argument("-d", "--device")
    p.add_argument("--pick", action="store_true", help="choose files in a dialog")
    p.add_argument("--notify", action="store_true", help="report the result as a desktop notification")
    p.add_argument("files", nargs="*")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("clip", help="push text (or stdin) to the phone's clipboard")
    p.add_argument("text", nargs="?")
    p.set_defaults(fn=cmd_clip)

    p = sub.add_parser("share", help="folders the phone may browse")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("name", nargs="?")
    p.add_argument("path", nargs="?")
    p.set_defaults(fn=cmd_share)

    p = sub.add_parser("sync", help="folders kept in sync")
    p.add_argument("action", choices=["list", "add", "remove", "now"])
    p.add_argument("folder", nargs="?", help="folder id (same on both devices)")
    p.add_argument("path", nargs="?")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("notify", help="show a notification on the phone")
    p.add_argument("-d", "--device")
    p.add_argument("title")
    p.add_argument("text", nargs="?")
    p.set_defaults(fn=cmd_notify)

    p = sub.add_parser("audio", help="play the phone's audio on this computer")
    p.add_argument("state", choices=["on", "off", "volume"])
    p.add_argument("level", nargs="?", help="for volume: 0–100")
    p.add_argument("-d", "--device")
    p.set_defaults(fn=cmd_audio)

    p = sub.add_parser("command", help="commands the phone may run here")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("name", nargs="?")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_command)

    p = sub.add_parser("wol", help="wake-on-LAN status, or enable it")
    p.add_argument("action", nargs="?", choices=["status", "enable"], default="status")
    p.add_argument("ifname", nargs="?")
    p.set_defaults(fn=cmd_wol)

    p = sub.add_parser("set", help="change a setting: name, clipboard, download_dir, scan_interval, "
                                   "remote_shell, command_timeout, notifications, media, phone_audio")
    p.add_argument("key")
    p.add_argument("value")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("mount", help="open the phone (or another device) in Files")
    p.add_argument("device", nargs="?", help="a paired device's name or id; default: the phone")
    p.add_argument("--bookmark", action="store_true", help="also add it to the Files sidebar")

    p = sub.add_parser("sidebar", help="devices shown in the Files sidebar")
    p.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    p.add_argument("device", nargs="?", help="a paired device's name or id")
    p.set_defaults(fn=cmd_sidebar)
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(fn=cmd_mount)

    args = ap.parse_args(argv)
    if args.cmd == "share" and args.action == "add" and not args.path:
        die("usage: tether share add NAME PATH")
    if args.cmd == "share" and args.action == "remove" and not args.name:
        die("usage: tether share remove NAME")
    if args.cmd == "sync" and args.action == "add" and not (args.folder and args.path):
        die("usage: tether sync add FOLDER_ID PATH")
    if args.cmd == "sidebar" and args.action != "list" and not args.device:
        die(f"usage: tether sidebar {args.action} DEVICE")
    if args.cmd == "sync" and args.action == "remove" and not args.folder:
        die("usage: tether sync remove FOLDER_ID")
    args.fn(Paths(args.home), args)


if __name__ == "__main__":
    main()
