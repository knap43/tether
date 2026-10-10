"""Progress tracking and cancellation for file transfers."""

import itertools
import time

from .errors import ProtoError

EMIT_INTERVAL = 0.25
_ids = itertools.count(1)


class Transfer:
    def __init__(self, registry: "Transfers", name: str, total: int | None, direction: str, peer: str, peer_name: str):
        self.registry = registry
        self.id = next(_ids)
        self.name = name
        self.total = total
        self.done = 0
        self.direction = direction  # "in" or "out"
        self.peer = peer
        self.peer_name = peer_name
        self.state = "active"
        self.cancelled = False
        self.incoming = None  # channel.Incoming, for receives
        self._last = 0.0

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "total": self.total, "done": self.done,
                "direction": self.direction, "peer": self.peer, "peer_name": self.peer_name, "state": self.state}

    def add(self, n: int) -> None:
        if self.cancelled:
            raise ProtoError("cancelled")
        self.done += n
        now = time.monotonic()
        if now - self._last >= EMIT_INTERVAL:
            self._last = now
            self.registry.emit(self)

    def cancel(self) -> None:
        self.cancelled = True
        if self.incoming is not None:
            self.incoming.abort()

    def finish(self, state: str) -> None:
        if self.state != "active":
            return
        self.state = "cancelled" if self.cancelled else state
        self.registry.items.pop(self.id, None)
        self.registry.emit(self)


class Transfers:
    def __init__(self, emit_event):
        self.items: dict[int, Transfer] = {}
        self._emit_event = emit_event

    def start(self, name, total, direction, peer, peer_name) -> Transfer:
        t = Transfer(self, name, total, direction, peer, peer_name)
        self.items[t.id] = t
        self.emit(t)
        return t

    def emit(self, t: Transfer) -> None:
        self._emit_event({"ev": "transfer", **t.info()})

    def cancel(self, tid: int) -> bool:
        t = self.items.get(tid)
        if t is None:
            return False
        t.cancel()
        return True

    def active(self) -> list[dict]:
        return [t.info() for t in self.items.values()]
