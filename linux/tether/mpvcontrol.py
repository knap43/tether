"""Steering a running mpv over its IPC socket, whether it's playing music or a video."""

import asyncio
import json
import os
import signal

from .errors import ProtoError

IPC_TIMEOUT = 2
PROPERTIES = ["media-title", "metadata/by-key/artist", "metadata/by-key/album", "time-pos", "duration", "pause",
              "volume", "playlist-pos", "playlist-count", "eof-reached"]


class MpvControl:
    """Subclasses start mpv as self.proc, passing it `--input-ipc-server={self.socket}`."""

    def __init__(self, name: str):
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        self.socket = os.path.join(runtime, f"tether-{name}-{os.getpid()}.sock")
        self.proc = None

    def alive(self) -> bool:
        return self.proc.poll() is None

    async def wait_exit(self) -> None:
        while self.alive():
            await asyncio.sleep(0.2)

    async def _ipc(self, commands: list[list]) -> list:
        """Answers in order: each command's data, or None where it failed (a track without an album, say)."""
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.socket), IPC_TIMEOUT)
        try:
            for i, c in enumerate(commands):
                writer.write(json.dumps({"command": c, "request_id": i + 1}).encode() + b"\n")
            await writer.drain()
            answers: dict[int, object] = {}
            while len(answers) < len(commands):
                line = await asyncio.wait_for(reader.readline(), IPC_TIMEOUT)
                if not line:
                    raise ConnectionError("mpv closed its socket")
                msg = json.loads(line)
                if "request_id" in msg:
                    answers[msg["request_id"]] = msg.get("data") if msg.get("error") == "success" else None
            return [answers.get(i + 1) for i in range(len(commands))]
        finally:
            writer.close()

    async def state(self) -> dict:
        if not self.alive():
            return {"playing": False}
        try:
            title, artist, album, pos, length, paused, volume, index, count, ended = await self._ipc(
                [["get_property", n] for n in PROPERTIES])
        except (OSError, ConnectionError, asyncio.TimeoutError, ValueError):
            return {"playing": True, "loading": True}
        if pos is None:  # still opening: until then its "title" is only the address
            return {"playing": True, "loading": True, "paused": bool(paused), "volume": volume, "index": index, "count": count}
        return {"playing": True, "title": title, "artist": artist, "album": album, "position": pos, "length": length,
                "paused": bool(paused), "volume": volume, "index": index, "count": count, "ended": bool(ended)}

    async def command(self, action, value=None) -> None:
        try:
            if action == "toggle":
                cmd = ["cycle", "pause"]
            elif action == "next":
                cmd = ["playlist-next"]
            elif action == "previous":  # as players do: back to the start first, then the track before
                pos = (await self._ipc([["get_property", "time-pos"]]))[0]
                cmd = ["seek", 0, "absolute"] if isinstance(pos, (int, float)) and pos > 3 else ["playlist-prev"]
            elif action in ("position", "volume"):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ProtoError("bad_request")
                cmd = (["seek", max(0, value), "absolute"] if action == "position"
                       else ["set_property", "volume", min(100, max(0, value))])
            else:
                raise ProtoError("bad_request")
            await self._ipc([cmd])
        except (OSError, ConnectionError, asyncio.TimeoutError, ValueError):
            raise ProtoError("io", "the player isn't answering")

    async def stop(self) -> None:
        if self.alive():
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        while self.alive():
            await asyncio.sleep(0.05)
        try:
            os.unlink(self.socket)
        except OSError:
            pass
