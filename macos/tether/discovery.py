"""UDP beacons on the local network."""

import asyncio
import json
import logging
import socket
import time

log = logging.getLogger("tether.discovery")

BEACON_INTERVAL = 10
REPLY_INTERVAL = 5


class Discovery(asyncio.DatagramProtocol):
    def __init__(self, node, port: int):
        self.node = node
        self.port = port
        self.transport = None
        self.seen: dict[str, dict] = {}  # id -> {name, type, addr, port, at}
        self._replied: dict[str, float] = {}
        self._broadcasts: list[str] = []

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", self.port))
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=sock)
        asyncio.ensure_future(self._beacon_loop())

    def beacon(self) -> bytes:
        n = self.node
        return json.dumps(
            {"tether": 1, "id": n.identity.id, "name": n.cfg["name"], "port": n.cfg["port"], "type": n.cfg.get("device_type", "desktop")}
        ).encode()

    async def _beacon_loop(self) -> None:
        from .wol import broadcast_addresses

        n = 0
        while True:
            if n % 6 == 0:  # networks come and go; look again every minute
                try:
                    self._broadcasts = await asyncio.to_thread(broadcast_addresses)
                except OSError:
                    pass
            n += 1
            self.announce()
            await asyncio.sleep(BEACON_INTERVAL)

    def announce(self) -> None:
        # macOS sends 255.255.255.255 out of the primary interface only; directed broadcasts reach the rest.
        for addr in ["255.255.255.255", *self._broadcasts]:
            try:
                self.transport.sendto(self.beacon(), (addr, self.port))
            except OSError as e:
                log.debug("broadcast to %s failed: %s", addr, e)

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            b = json.loads(data)
            if b.get("tether") != 1:
                return
            pid, port = str(b["id"]), int(b["port"])
            if len(pid) != 64 or not 0 < port < 65536:
                return
        except (ValueError, KeyError, TypeError):
            return
        if pid == self.node.identity.id:
            return
        self.seen[pid] = {
            "name": str(b.get("name", "?"))[:100],
            "type": str(b.get("type", "?"))[:20],
            "addr": addr[0],
            "port": port,
            "at": time.time(),
        }
        if self.node.is_connected(pid):
            return
        now = time.monotonic()
        if now - self._replied.get(pid, 0) > REPLY_INTERVAL:
            self._replied[pid] = now
            try:
                self.transport.sendto(self.beacon(), addr)
            except OSError:
                pass
        self.node.on_beacon(pid, addr[0], port)

    def recent(self, max_age: float = 60) -> dict[str, dict]:
        now = time.time()
        return {k: v for k, v in self.seen.items() if now - v["at"] < max_age}
