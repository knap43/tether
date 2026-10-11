"""Plays the phone's audio stream (raw PCM) through the desktop's sound server."""

import asyncio
import fcntl
import logging
import shutil
import subprocess
import sys
import threading
from array import array

from .errors import ProtoError

log = logging.getLogger("tether.audio")

F_SETPIPE_SZ = 1031
PACAT_LATENCY_MS = 60
TARGET_MS = 100  # jitter buffer kept ahead of playback, absorbing Wi-Fi hiccups
MAX_MS = 400  # beyond this (the phone's clock runs fast, or a burst after a stall) we skip back to TARGET_MS
PIECE_MS = 10


def available() -> bool:
    return shutil.which("pacat") is not None


def gain_for(volume: float) -> float:
    """0–1 slider position → amplitude; squared, so the slider feels even to the ear."""
    v = min(1.0, max(0.0, float(volume)))
    return v * v


class PacatSink:
    """Feeds s16le PCM to `pacat` (libpulse; works with PipeWire's pulse server).

    Network arrival is bursty while the sound card drains at its own steady clock, so
    incoming audio goes into a jitter buffer and a writer thread hands pacat one 10 ms
    piece at a time, blocking on pacat for pacing. When the buffer runs dry the thread
    plays silence until it has refilled, so pacat itself never underruns; when it grows
    too long the excess is skipped in one go. Either way the stream stays frame-aligned.
    """

    def __init__(self, rate: int, channels: int, title: str, volume: float = 1.0):
        if not available():
            raise ProtoError("unsupported", "pacat isn't installed on the computer")
        self.frame = 2 * channels
        per_ms = rate * self.frame / 1000
        align = lambda n: int(n) - int(n) % self.frame  # noqa: E731
        self.piece = align(per_ms * PIECE_MS)
        self.target = align(per_ms * TARGET_MS)
        self.limit = align(per_ms * MAX_MS)
        self.slack = align(per_ms * 30)
        self.silence = bytes(self.piece)
        self.gain = gain_for(volume)
        self.buf = bytearray()
        self.rest = b""
        self.primed = False
        self.closed = False
        self.failed = False
        self.dropped = self.underruns = 0
        self.cond = threading.Condition()
        self.proc = subprocess.Popen(
            ["pacat", "--playback", "--raw", "--format=s16le", f"--rate={rate}", f"--channels={channels}",
             f"--latency-msec={PACAT_LATENCY_MS}", "--client-name=Tether", f"--stream-name={title}",
             "--property=media.role=music", "--property=application.icon_name=phone"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:  # the pipe is latency too; keep it to one page
            fcntl.fcntl(self.proc.stdin.fileno(), F_SETPIPE_SZ, 4096)
        except OSError:
            pass
        self.thread = threading.Thread(target=self._run, name="tether-audio", daemon=True)
        self.thread.start()

    def set_volume(self, volume: float) -> None:
        self.gain = gain_for(volume)

    def write(self, data: bytes) -> None:
        if self.failed:
            raise ProtoError("io", "the audio player stopped")
        data = self.rest + data
        whole = len(data) - len(data) % self.frame
        self.rest = data[whole:]
        with self.cond:
            self.buf += data[:whole]
            if len(self.buf) > self.limit:
                drop = len(self.buf) - self.target
                del self.buf[:drop]
                self.dropped += drop
            self.cond.notify()

    def _next(self) -> bytes:
        with self.cond:
            if not self.primed and len(self.buf) >= self.target:
                self.primed = True
            if self.primed and len(self.buf) >= self.piece:
                level = len(self.buf)
                out = bytes(self.buf[: self.piece])
                del self.buf[: self.piece]
                # The phone's and the sound card's clocks never quite agree. Rather than
                # letting the buffer creep until it has to skip, nudge it back toward the
                # target by dropping or repeating a single frame per piece (inaudible).
                if level > self.target + self.slack and len(self.buf) >= 4 * self.frame:
                    n = self.frame * (6 if level > self.target * 2 else 2)
                    del self.buf[:n]
                    self.dropped += n
                elif level < self.target - self.slack:
                    out += out[-self.frame :]
                return out
            if self.primed:  # ran dry: refill to the target before playing again
                self.primed = False
                self.underruns += 1
            return self.silence

    def _scale(self, pcm: bytes) -> bytes:
        g = self.gain
        if g >= 0.999:
            return pcm
        if g <= 0.0:
            return self.silence[: len(pcm)]
        a = array("h", pcm)
        if sys.byteorder == "big":
            a.byteswap()
        a = array("h", [int(x * g) for x in a])
        if sys.byteorder == "big":
            a.byteswap()
        return a.tobytes()

    def _run(self) -> None:
        out = self.proc.stdin
        try:
            while not self.closed:
                out.write(self._scale(self._next()))
                out.flush()
        except (BrokenPipeError, OSError, ValueError):
            if not self.closed:
                self.failed = True

    async def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify()
        await asyncio.to_thread(self.thread.join, 1)
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            await asyncio.to_thread(self.proc.wait, 1)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            await asyncio.to_thread(self.proc.wait)
        if self.dropped or self.underruns:
            log.info("audio: skipped %d bytes, refilled %d times", self.dropped, self.underruns)
