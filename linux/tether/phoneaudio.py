"""Plays the phone's audio stream (raw PCM) through the desktop's sound server."""

import asyncio
import fcntl
import logging
import os
import shutil
import subprocess

from .errors import ProtoError

log = logging.getLogger("tether.audio")

F_SETPIPE_SZ = 1031
# A small pipe keeps latency bounded: when pacat falls behind (the phone's clock runs
# slightly fast, or the sound server stalls), chunks are dropped instead of queueing up.
PIPE_SIZE = 16384
PIECE = 4096  # ≤ PIPE_BUF, so each non-blocking write is all-or-nothing; a whole number of frames
LATENCY_MS = 80


def available() -> bool:
    return shutil.which("pacat") is not None


class PacatSink:
    """Feeds s16le PCM to `pacat` (libpulse; works with PipeWire's pulse server)."""

    def __init__(self, rate: int, channels: int, title: str):
        if not available():
            raise ProtoError("unsupported", "pacat isn't installed on the computer")
        self.frame = 2 * channels
        self.dropped = 0
        self.proc = subprocess.Popen(
            ["pacat", "--playback", "--raw", "--format=s16le", f"--rate={rate}", f"--channels={channels}",
             f"--latency-msec={LATENCY_MS}", "--client-name=Tether", f"--stream-name={title}",
             "--property=media.role=music", "--property=application.icon_name=phone"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        self.fd = self.proc.stdin.fileno()
        os.set_blocking(self.fd, False)
        try:
            fcntl.fcntl(self.fd, F_SETPIPE_SZ, PIPE_SIZE)
        except OSError:
            pass
        self.rest = b""

    def write(self, data: bytes) -> None:
        data = self.rest + data
        whole = len(data) - len(data) % self.frame
        self.rest = data[whole:]
        piece = PIECE - PIECE % self.frame
        for i in range(0, whole, piece):
            try:
                os.write(self.fd, data[i : min(i + piece, whole)])
            except BlockingIOError:
                self.dropped += whole - i
                return
            except BrokenPipeError:
                raise ProtoError("io", "the audio player stopped") from None

    async def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            await asyncio.to_thread(self.proc.wait, 1)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            await asyncio.to_thread(self.proc.wait)
        if self.dropped:
            log.info("audio: dropped %d bytes to stay in sync", self.dropped)
