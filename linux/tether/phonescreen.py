"""Shows the phone's screen (a raw H.264 stream) in a viewer window."""

import asyncio
import fcntl
import logging
import os
import shutil
import subprocess
import sys
import threading

from .errors import ProtoError

log = logging.getLogger("tether.screen")

F_SETPIPE_SZ = 1031
MAX_BUFFER = 8 * 1024 * 1024  # beyond this the viewer has stalled; it picks up again at the next keyframe
PIECE = 64 * 1024
DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
                "DBUS_SESSION_BUS_ADDRESS")


def available() -> bool:
    return shutil.which("mpv") is not None or shutil.which("ffplay") is not None


def viewer_command(title: str) -> list[str]:
    """The viewer to run, reading the stream from its stdin and tuned for delay over smoothness."""
    if shutil.which("mpv"):
        return ["mpv", "--no-config", "--no-terminal", "--profile=low-latency", "--untimed", "--no-cache",
                "--no-demuxer-thread", "--demuxer-lavf-format=h264", "--force-window=immediate",
                "--autofit-larger=90%x90%", "--osc=no", "--keep-open=no",
                "--title=" + title.replace("$", "$$"), "-"]  # mpv expands ${…} in titles
    if shutil.which("ffplay"):
        return ["ffplay", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
                "-framedrop", "-probesize", "32768", "-analyzeduration", "100000", "-autoexit",
                "-window_title", title, "-f", "h264", "-i", "-"]
    raise ProtoError("unsupported", "showing the phone's screen needs mpv or ffplay")


def display_env() -> dict[str, str]:
    """Our environment, plus the graphical session's.

    A systemd user service may start before the desktop does, and then never has these variables
    itself; the session hands them to the service manager instead.
    """
    env = dict(os.environ)
    if sys.platform != "linux" or env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
        return env
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, text=True,
                             timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key in DISPLAY_VARS:
            env.setdefault(key, value)
    if not env.get("WAYLAND_DISPLAY") and not env.get("DISPLAY"):
        raise ProtoError("unsupported", "no one is logged in to a desktop on the computer")
    return env


class ViewerSink:
    """Feeds the stream to a viewer window; closing the window is how the user stops it.

    A thread does the blocking writes, because a viewer that falls behind mustn't hold up the
    connection, which everything else travels over too.
    """

    def __init__(self, title: str):
        self.buf = bytearray()
        self.closed = False
        self.dropped = 0
        self.cond = threading.Condition()
        self.proc = subprocess.Popen(
            viewer_command(title), env=display_env(),
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:  # the pipe is latency too
            fcntl.fcntl(self.proc.stdin.fileno(), F_SETPIPE_SZ, PIECE)
        except OSError:
            pass
        self.thread = threading.Thread(target=self._run, name="tether-screen", daemon=True)
        self.thread.start()

    def write(self, data: bytes) -> None:
        with self.cond:
            if len(self.buf) + len(data) > MAX_BUFFER:
                self.dropped += len(self.buf)
                self.buf.clear()
            self.buf += data
            self.cond.notify()

    def _run(self) -> None:
        out = self.proc.stdin
        try:
            while True:
                with self.cond:
                    while not self.buf and not self.closed:
                        self.cond.wait()
                    if self.closed:
                        return
                    piece = bytes(self.buf[:PIECE])
                    del self.buf[:PIECE]
                out.write(piece)
                out.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass  # the viewer is gone; wait_exit() reports it

    async def wait_exit(self) -> None:
        """Returns once the viewer has exited, normally because its window was closed."""
        while self.proc.poll() is None:
            await asyncio.sleep(0.2)

    async def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify()
        self.proc.terminate()  # first: this also frees a writer stuck on a stalled viewer
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
        if self.dropped:
            log.info("screen: the viewer stalled; skipped %d bytes", self.dropped)
