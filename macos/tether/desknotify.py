"""Phone notifications in Notification Center.

The Tether app posts them (UNUserNotificationCenter), with an inline Reply
field when the phone says the original supports replies, and reports back
replies and dismissals over the control socket. Without the app they fall
back to plain banners from osascript, which can't reply or be closed.
"""

import itertools
import logging

from . import macos

log = logging.getLogger("tether.notify")

REASON_DISMISSED = 2


class DesktopNotifier:
    def __init__(self, on_action, on_closed):
        self.on_action = on_action  # (nid, action_key, text)
        self.on_closed = on_closed  # (nid, reason)
        self.clients: set = set()  # control connections of the Tether app
        self._ids = itertools.count(1)
        self.available = True

    async def notify(self, app: str, title: str, body: str, icon: str = "", actions: list[tuple[str, str]] = (),
                     replaces: int = 0) -> int | None:
        nid = replaces or next(self._ids)
        if self.clients:
            ev = {"ev": "desk_notify", "nid": nid, "app": app, "title": title, "body": body, "icon": icon,
                  "actions": [{"key": k, "label": label} for k, label in actions]}
            for c in list(self.clients):
                await c.push(ev)
        else:
            macos.notify(app, body, subtitle=title)
        return nid

    async def close(self, nid: int) -> None:
        for c in list(self.clients):
            await c.push({"ev": "desk_close", "nid": nid})

    async def watch(self) -> None:
        """Nothing to watch: the app reports actions with `desk_action` and `desk_closed`."""
