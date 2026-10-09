"""Drives the GTK window against real daemons and saves a PNG of each page.

Run headless:
    dbus-run-session -- xvfb-run -a -s "-screen 0 1600x1200x24" \
        env GDK_BACKEND=x11 GSK_RENDERER=cairo python3 tests/gui_snapshot.py OUT_DIR
Needs a Python with GTK 4 + libadwaita bindings; the daemons run with $DAEMON_PYTHON (default python3).
"""

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LINUX = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LINUX))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from tether.config import Paths  # noqa: E402
from tether.gui import TetherApp  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "gui-shots")
ROOT = Path(tempfile.mkdtemp(prefix="tether-gui-"))
PROCS = []
atexit.register(lambda: [p.terminate() for p in PROCS])


def daemon(name, port, extra, dav=False):
    home = ROOT / name
    (home / "config").mkdir(parents=True)
    cfg = {"name": extra.pop("display", name), "port": port, "discovery_port": port + 2000, "dav_port": port + 1000,
           "download_dir": str(home / "downloads"), "scan_interval": 2, **extra}
    (home / "config" / "config.json").write_text(json.dumps(cfg))
    args = [os.environ.get("DAEMON_PYTHON", "python3"), "-m", "tether", "daemon", "--no-discovery"]
    if not dav:
        args.append("--no-dav")
    env = {**os.environ, "TETHER_HOME": str(home), "PYTHONPATH": str(LINUX), "TETHER_DEBUG": "1"}
    PROCS.append(subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=open(home / "log", "wb")))
    return home


def ctl(home, cmd, **kw):
    for _ in range(50):
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(str(home / "run" / "ctl.sock"))
                f = s.makefile("rwb")
                f.write(json.dumps({"cmd": cmd, **kw}).encode() + b"\n")
                f.flush()
                line = f.readline()
                if not line:
                    raise RuntimeError(f"{cmd}: daemon closed the connection:\n" + (home / "log").read_text()[-2500:])
                return json.loads(line)
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.1)
    raise RuntimeError("daemon did not start")


def pair(a, b, b_port):
    res = ctl(a, "pair", addr=f"127.0.0.1:{b_port}")
    assert res["ok"], res
    ctl(a, "pair_confirm", device=res["device"], accept=True)
    a_id = ctl(a, "status")["id"]
    for _ in range(50):
        if any(p["id"] == a_id for p in ctl(b, "status")["pending"]):
            break
        time.sleep(0.1)
    assert ctl(b, "pair_confirm", device=a_id, accept=True)["ok"]


shares = ROOT / "Projects"
shares.mkdir()
photos = ROOT / "Phone photos"
photos.mkdir()
A = daemon("A", 52291, {
    "display": "arch-desktop",
    "shares": {"Home": str(ROOT), "Projects": str(shares)},
    "sync": [{"id": "camera", "path": str(photos)}],
    "commands": [{"id": "lock-screen", "name": "Lock screen", "command": "loginctl lock-session"},
                 {"id": "suspend", "name": "Suspend", "command": "systemctl suspend"}],
}, dav=True)
B = daemon("B", 52391, {"display": "Pixel 8", "device_type": "phone"})
C = daemon("C", 52491, {"display": "Graphene test phone", "device_type": "phone"})
ctl(A, "status")
ctl(B, "status")
ctl(C, "status")
pair(A, B, 52391)

app = TetherApp(Paths(str(A)))
results = []


def shot(win, name):
    GLib.MainContext.default().iteration(False)
    w, h = win.get_width(), win.get_height()
    paintable = Gtk.WidgetPaintable.new(win)
    node = None
    for _ in range(20):
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, w, h)
        node = snap.to_node()
        if node is not None:
            break
        wait_for(lambda: False, 0.05)
    tex = win.get_renderer().render_texture(node, None)
    OUT.mkdir(parents=True, exist_ok=True)
    tex.save_to_png(str(OUT / f"{name}.png"))
    print("saved", name)


def wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    ctx = GLib.MainContext.default()
    while time.monotonic() < end:
        while ctx.pending():
            ctx.iteration(False)
        if pred():
            return True
        time.sleep(0.05)
    return False


def scenario():
    win = app.get_active_window()
    assert wait_for(lambda: win.status and any(d["connected"] for d in win.status["devices"])), "no status"
    wait_for(lambda: False, 0.5)
    a_id = ctl(A, "status")["id"]
    ctl(B, "debug_send", device=a_id, msg={"t": "battery", "level": 76, "charging": True})
    for pkg, label in (("org.thoughtcrime.securesms", "Signal"), ("com.fsck.k9", "K-9 Mail")):
        ctl(B, "debug_send", device=a_id, msg={"t": "notif_posted", "key": pkg, "app": pkg, "app_name": label,
                                                "title": "Hello", "text": "test"})
    assert wait_for(lambda: win.status and win.status["devices"][0].get("battery")
                    and len(win.status["notif_apps"]) == 2), "battery/apps not shown"
    for page in ("devices", "folders", "commands", "settings"):
        win.stack.set_visible_child_name(page)
        wait_for(lambda: False, 0.4)
        shot(win, page)
    win.stack.set_visible_child_name("devices")

    # An incoming pairing request pops the code dialog.
    res = ctl(C, "pair", addr="127.0.0.1:52291")
    c_id = ctl(C, "status")["id"]
    assert wait_for(lambda: c_id in win.pair_dialogs), "pair dialog did not appear"
    wait_for(lambda: False, 0.6)
    shot(win, "pair-dialog")
    win.pair_dialogs[c_id].emit("response", "accept")
    ctl(C, "pair_confirm", device=res["device"], accept=True)
    assert wait_for(lambda: win.status and len(win.status["devices"]) == 2), "second device did not pair"
    results.append("pairing from dialog")

    # A large transfer shows a progress row.
    b_id0 = next(d["id"] for d in win.status["devices"] if d["name"] == "Pixel 8")
    big = ROOT / "holiday-video.mkv"
    with open(big, "wb") as fh:
        fh.truncate(400_000_000)
    win.send_paths(b_id0, [str(big)])
    assert wait_for(lambda: win.transfer_rows and next(iter(win.transfer_rows.values()))[1].get_fraction() > 0.05,
                    20), "no progress row"
    shot(win, "transfer")
    assert wait_for(lambda: not win.transfer_rows, 60), "transfer row did not finish"
    results.append("transfer progress")

    # Actions through the window.
    b_id = next(d["id"] for d in win.status["devices"] if d["name"] == "Pixel 8")
    f = ROOT / "report.pdf"
    f.write_bytes(os.urandom(50_000))
    win.send_paths(b_id, [str(f)])
    assert wait_for(lambda: (B / "downloads" / "report.pdf").exists(), 10), "send failed"
    results.append("send files")
    win.cmd_name.set_text("Say hi")
    win.cmd_line.set_text("notify-send hi")
    win.add_command()
    assert wait_for(lambda: any(c["name"] == "Say hi" for c in win.status["commands"])), "command add failed"
    results.append("add command")
    win.call("set", key="remote_shell", value=True)
    assert wait_for(lambda: win.status["remote_shell"] and win.shell_row.get_active()), "shell toggle not reflected"
    results.append("shell switch reflects daemon")
    win.call("share_remove", name="Projects")
    assert wait_for(lambda: "Projects" not in win.status["shares"]), "share remove failed"
    results.append("remove share")
    ctl(A, "notify", device=b_id, title="x")  # B is a Python stand-in; it should refuse cleanly
    win.stack.set_visible_child_name("devices")
    wait_for(lambda: False, 0.5)
    shot(win, "devices-after")
    win.set_default_size(400, 760)
    win.unmaximize()
    wait_for(lambda: False, 0.8)
    shot(win, "narrow")
    print("OK:", ", ".join(results))
    app.quit()
    return False


def guarded():
    try:
        scenario()
    except BaseException:
        import traceback

        traceback.print_exc()
        FAILED.append(True)
        app.quit()
    return False


FAILED = []


def on_activate(_app):
    GLib.timeout_add(300, guarded)


app.connect_after("activate", on_activate)
try:
    app.run([])
finally:
    for p in PROCS:
        p.terminate()
    for name in ("A", "B", "C"):
        log = ROOT / name / "log"
        if log.exists() and ("Traceback" in log.read_text() or os.environ.get("SHOW_LOGS")):
            print(f"--- daemon {name} log ---\n" + log.read_text()[-3000:])
    shutil.rmtree(ROOT, ignore_errors=True)
    if FAILED:
        sys.exit(1)
