"""The phone as mouse and keyboard, through GNOME's remote desktop interface (works on Wayland)."""

import logging
import queue
import threading

from .errors import ProtoError

log = logging.getLogger("tether.remote")

BUS = "org.gnome.Mutter.RemoteDesktop"
SESSION = "org.gnome.Mutter.RemoteDesktop.Session"
BUTTONS = {"left": 272, "right": 273}  # evdev codes
KEYS = {"space": 0x20, "f": 0x66, "m": 0x6d, "escape": 0xff1b, "return": 0xff0d,
        "left": 0xff51, "up": 0xff52, "right": 0xff53, "down": 0xff54, "f5": 0xffc2}  # the only keys a phone can press
MAX_MOVE = 2000.0
MAX_STEPS = 50


class RemoteInput:
    """One input session. A thread does the D-Bus calls, so that they never hold up the connection."""

    def __init__(self):
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib

            self.GLib = GLib
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            res = self.bus.call_sync(BUS, "/org/gnome/Mutter/RemoteDesktop", BUS, "CreateSession", None,
                                     GLib.VariantType("(o)"), 0, 3000, None)
            self.path = res.unpack()[0]
            self._call("Start")
        except (ImportError, ValueError, Exception) as e:  # GLib.Error has no narrower base
            log.info("remote input unavailable: %s", e)
            raise ProtoError("unsupported", "needs GNOME's remote desktop interface") from None
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.thread = threading.Thread(target=self._run, name="tether-remote", daemon=True)
        self.thread.start()

    def _call(self, method: str, args=None) -> None:
        self.bus.call_sync(BUS, self.path, SESSION, method, args, None, 0, 3000, None)

    def _run(self) -> None:
        while True:
            event = self.events.get()
            if event is None:
                break
            try:
                self._call(*event)
            except Exception as e:  # the session ended under us, say; a lost nudge doesn't matter
                log.info("remote input: %s", e)
        try:
            self._call("Stop")
        except Exception:
            pass

    def move(self, dx: float, dy: float) -> None:
        self.events.put(("NotifyPointerMotionRelative", self.GLib.Variant("(dd)", (dx, dy))))

    def click(self, button: str) -> None:
        for down in (True, False):
            self.events.put(("NotifyPointerButton", self.GLib.Variant("(ib)", (BUTTONS[button], down))))

    def scroll(self, steps: int) -> None:
        self.events.put(("NotifyPointerAxisDiscrete", self.GLib.Variant("(ui)", (0, steps))))

    def key(self, name: str) -> None:
        for down in (True, False):
            self.events.put(("NotifyKeyboardKeysym", self.GLib.Variant("(ub)", (KEYS[name], down))))

    def close(self) -> None:
        self.events.put(None)


def apply(remote, msg: dict) -> None:
    """One event from the phone; anything malformed is ignored, as it may arrive many times a second."""
    kind = msg.get("kind")
    if kind == "move":
        dx, dy = msg.get("dx"), msg.get("dy")
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (dx, dy)):
            remote.move(max(-MAX_MOVE, min(MAX_MOVE, float(dx))), max(-MAX_MOVE, min(MAX_MOVE, float(dy))))
    elif kind == "click" and msg.get("button") in BUTTONS:
        remote.click(msg["button"])
    elif kind == "scroll" and isinstance(msg.get("steps"), int) and not isinstance(msg.get("steps"), bool):
        remote.scroll(max(-MAX_STEPS, min(MAX_STEPS, msg["steps"])))
    elif kind == "key" and isinstance(msg.get("key"), str) and msg["key"] in KEYS:
        remote.key(msg["key"])
