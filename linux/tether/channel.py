"""Message and stream multiplexing over an encrypted Framer."""

import asyncio
import json
import logging
import time

from .crypto import KIND_DATA, KIND_JSON
from .errors import ProtoError

log = logging.getLogger("tether.channel")

CHUNK = 64 * 1024
QUEUE_DEPTH = 64
PING_INTERVAL = 30
IDLE_TIMEOUT = 90

# Messages whose "stream" field announces data flowing *to* us.
_INBOUND_STREAM_TYPES = {"send", "fs_write", "res", "clip_image"}

_ABORTED = object()
_CLOSED = object()


class StreamAborted(Exception):
    pass


class Incoming:
    """Receiving end of a stream."""

    def __init__(self, ch: "Channel", sid: int):
        self.ch, self.sid = ch, sid
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_DEPTH)
        self.done = False

    async def read(self) -> bytes:
        """Next chunk; b"" at the end of the stream."""
        if self.done:
            return b""
        item = await self.queue.get()
        if item is _CLOSED:
            self.done = True
            raise ConnectionError("connection closed")
        if item is _ABORTED:
            self.done = True
            raise StreamAborted()
        if item == b"":
            self.done = True
        return item

    async def chunks(self):
        while True:
            data = await self.read()
            if not data:
                return
            yield data

    def abort(self) -> None:
        """Stop receiving; the rest of the stream is discarded."""
        if self.done:
            return
        self.done = True
        if self.ch.streams.pop(self.sid, None) is not None:
            self.ch.discard.add(self.sid)
            if not self.ch.closed.is_set():
                self.ch.spawn(self.ch.send({"t": "cancel", "stream": self.sid}))
        # Unblock the receive loop if it is waiting to enqueue into this stream.
        while not self.queue.empty():
            self.queue.get_nowait()

    async def drain(self) -> None:
        try:
            while await self.read():
                pass
        except (StreamAborted, ConnectionError):
            pass


class Channel:
    def __init__(self, framer, is_client: bool, handler, label: str = ""):
        self.framer = framer
        self.handler = handler  # async (channel, msg) -> None
        self.label = label
        self._next_stream = 1 if is_client else 2
        self._next_req = 1
        self.pending: dict[int, asyncio.Future] = {}
        self.streams: dict[int, Incoming] = {}
        self.discard: set[int] = set()
        self.cancelled: set[int] = set()
        self.closed = asyncio.Event()
        self._finished = False
        self.tasks: set[asyncio.Task] = set()
        self.last_rx = time.monotonic()

    # -- plumbing ---------------------------------------------------------

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, t: asyncio.Task) -> None:
        self.tasks.discard(t)
        if not t.cancelled() and t.exception() and not isinstance(t.exception(), (ConnectionError, StreamAborted)):
            log.warning("%s: task failed: %r", self.label, t.exception())

    def new_stream(self) -> int:
        s = self._next_stream
        self._next_stream += 2
        return s

    async def send(self, msg: dict) -> None:
        if self.closed.is_set():
            raise ConnectionError("connection closed")
        await self.framer.send(bytes([KIND_JSON]) + json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode())

    async def send_chunk(self, sid: int, data: bytes) -> None:
        await self.framer.send(bytes([KIND_DATA]) + sid.to_bytes(4, "big") + data)

    async def send_stream(self, sid: int, chunks) -> bool:
        """Send an async iterable of bytes as stream `sid`. Returns False if the receiver cancelled."""
        try:
            async for data in chunks:
                if sid in self.cancelled:
                    self.cancelled.discard(sid)
                    return False
                for i in range(0, len(data), CHUNK):
                    await self.send_chunk(sid, data[i : i + CHUNK])
        except (ConnectionError, StreamAborted):
            raise
        except asyncio.CancelledError:
            if not self.closed.is_set():
                self.spawn(self.send({"t": "abort", "stream": sid}))
            raise
        except Exception:
            await self.send({"t": "abort", "stream": sid})
            raise
        await self.send_chunk(sid, b"")
        return True

    async def request(self, msg: dict, upload=None, timeout: float = 60) -> dict:
        """Send a request (optionally followed by an upload stream) and await its answer."""
        req = self._next_req
        self._next_req += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[req] = fut
        msg = {**msg, "req": req}
        try:
            if upload is not None:
                sid = self.new_stream()
                msg["stream"] = sid
                await self.send(msg)
                if not await self.send_stream(sid, upload):
                    raise ProtoError("cancelled")
            else:
                await self.send(msg)
            res = await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(req, None)
        if not res.get("ok"):
            if "_in" in res:
                res["_in"].abort()
            raise ProtoError(str(res.get("error") or "io"))
        return res

    async def reply(self, req_msg: dict, **fields) -> None:
        await self.send({"t": "res", "req": req_msg.get("req"), "ok": True, **fields})

    async def reply_error(self, req_msg: dict, code: str) -> None:
        if "req" in req_msg:
            await self.send({"t": "res", "req": req_msg["req"], "ok": False, "error": code})

    async def reply_stream(self, req_msg: dict, chunks, **fields) -> None:
        sid = self.new_stream()
        await self.reply(req_msg, stream=sid, **fields)
        await self.send_stream(sid, chunks)

    # -- receive loop -------------------------------------------------------

    async def run(self) -> None:
        pinger = self.spawn(self._keepalive())
        try:
            while True:
                pt = await self.framer.recv()
                self.last_rx = time.monotonic()
                kind = pt[0]
                if kind == KIND_JSON:
                    await self._on_message(json.loads(pt[1:]))
                elif kind == KIND_DATA and len(pt) >= 5:
                    await self._on_chunk(int.from_bytes(pt[1:5], "big"), pt[5:])
                else:
                    raise ConnectionError(f"unknown frame kind {kind}")
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError) as e:
            log.info("%s: closed (%s)", self.label, e or type(e).__name__)
        finally:
            pinger.cancel()
            self._shutdown()

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            if time.monotonic() - self.last_rx > IDLE_TIMEOUT:
                log.info("%s: idle timeout", self.label)
                self.close()
                return
            await self.send({"t": "ping"})

    async def _on_message(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            raise ValueError("message is not an object")
        t = msg.get("t")
        if t in _INBOUND_STREAM_TYPES and isinstance(msg.get("stream"), int):
            inc = Incoming(self, msg["stream"])
            self.streams[inc.sid] = inc
            msg["_in"] = inc
        if t == "res":
            fut = self.pending.get(msg.get("req"))
            if fut and not fut.done():
                fut.set_result(msg)
            elif "_in" in msg:
                msg["_in"].abort()
        elif t == "ping":
            self.spawn(self.send({"t": "pong"}))
        elif t == "pong":
            pass
        elif t == "cancel":
            self.cancelled.add(msg.get("stream"))
        elif t == "abort":
            inc = self.streams.pop(msg.get("stream"), None)
            if inc:
                await inc.queue.put(_ABORTED)
        else:
            self.spawn(self._dispatch(msg))

    async def _dispatch(self, msg: dict) -> None:
        try:
            await self.handler(self, msg)
        except ProtoError as e:
            await self._fail(msg, e.code)
        except (ConnectionError, StreamAborted):
            raise
        except Exception:
            log.exception("%s: handler for %s failed", self.label, msg.get("t"))
            await self._fail(msg, "io")

    async def _fail(self, msg: dict, code: str) -> None:
        if "_in" in msg:
            msg["_in"].abort()
        try:
            await self.reply_error(msg, code)
        except ConnectionError:
            pass

    async def _on_chunk(self, sid: int, data: bytes) -> None:
        if sid in self.discard:
            if not data:
                self.discard.discard(sid)
            return
        inc = self.streams.get(sid)
        if inc is None:
            return
        if not data:
            del self.streams[sid]
        await inc.queue.put(data)

    def close(self) -> None:
        self.closed.set()
        self.framer.close()

    def _shutdown(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.closed.set()
        self.framer.close()
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("connection closed"))
                fut.exception()  # mark retrieved; a waiting request still sees the error
        for inc in self.streams.values():
            while not inc.queue.empty():
                inc.queue.get_nowait()
            inc.queue.put_nowait(_CLOSED)
        self.streams.clear()
        for t in list(self.tasks):
            t.cancel()
