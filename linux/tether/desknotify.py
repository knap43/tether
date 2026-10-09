"""Desktop notifications through org.freedesktop.Notifications, driven with gdbus.

Phone notifications are shown with a "Reply" button when the phone says the
original supports replies; closing one on the desktop dismisses it on the phone.
"""

import asyncio
import json
import logging
import re
import shutil

log = logging.getLogger("tether.notify")

DEST = ["--session", "--dest", "org.freedesktop.Notifications", "--object-path", "/org/freedesktop/Notifications"]
SIGNAL = re.compile(r"org\.freedesktop\.Notifications\.(ActionInvoked|NotificationClosed) \(uint32 (\d+), (.*)\)\s*$")
REASON_DISMISSED = 2


def gv_string(s: str) -> str:
    """A GVariant text-format string literal (JSON string syntax is a compatible subset)."""
    return json.dumps(s)


def parse_signal(line: str):
    """Returns ("action", id, key) or ("closed", id, reason) for a `gdbus monitor` line, else None."""
    m = SIGNAL.search(line)
    if not m:
        return None
    kind, nid, rest = m.group(1), int(m.group(2)), m.group(3)
    if kind == "ActionInvoked":
        a = re.match(r"'(.*)'$", rest.strip())
        return ("action", nid, a.group(1) if a else rest.strip())
    r = re.search(r"uint32 (\d+)", rest)
    return ("closed", nid, int(r.group(1)) if r else 0)


class DesktopNotifier:
    def __init__(self, on_action, on_closed):
        self.on_action = on_action  # (nid, action_key)
        self.on_closed = on_closed  # (nid, reason)
        self.available = bool(shutil.which("gdbus"))

    async def notify(self, app: str, title: str, body: str, icon: str = "", actions: list[tuple[str, str]] = (),
                     replaces: int = 0) -> int | None:
        if not self.available:
            return None
        acts = "[" + ", ".join(f"{gv_string(k)}, {gv_string(label)}" for k, label in actions) + "]"
        if not actions:
            acts = "@as []"
        hints = "{'desktop-entry': <'dev.tether.Tether'>}"
        proc = await asyncio.create_subprocess_exec(
            "gdbus", "call", *DEST, "--method", "org.freedesktop.Notifications.Notify", "--",
            gv_string(app), str(replaces), gv_string(icon), gv_string(title), gv_string(body), acts, hints, "-1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        m = re.search(r"uint32 (\d+)", out.decode())
        if not m:
            log.debug("Notify failed: %s", err.decode().strip())
            return None
        return int(m.group(1))

    async def close(self, nid: int) -> None:
        if not self.available:
            return
        proc = await asyncio.create_subprocess_exec(
            "gdbus", "call", *DEST, "--method", "org.freedesktop.Notifications.CloseNotification", "--", str(nid),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()

    async def watch(self) -> None:
        if not self.available:
            return
        while True:
            proc = await asyncio.create_subprocess_exec(
                "gdbus", "monitor", *DEST, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                while line := await proc.stdout.readline():
                    sig = parse_signal(line.decode("utf-8", "replace"))
                    if sig and sig[0] == "action":
                        self.on_action(sig[1], sig[2])
                    elif sig:
                        self.on_closed(sig[1], sig[2])
            finally:
                if proc.returncode is None:
                    proc.kill()
            await asyncio.sleep(5)
