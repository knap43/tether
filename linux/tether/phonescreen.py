"""Shows the phone's screen (a raw H.264 stream) in a viewer window."""

import ast
import asyncio
import fcntl
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

from .errors import ProtoError

log = logging.getLogger("tether.screen")

F_SETPIPE_SZ = 1031
MAX_BUFFER = 8 * 1024 * 1024  # beyond this the viewer has stalled; it picks up again at the next keyframe
PIECE = 64 * 1024
MAX_DELAY = 0.6  # the most the picture is held back to stay with the sound
MPRIS_SCRIPTS = ("/usr/lib/mpv-mpris/mpris.so", "/etc/mpv/scripts/mpris.so")  # lets the phone's Media screen drive mpv
DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
                "DBUS_SESSION_BUS_ADDRESS")


def available() -> bool:
    return shutil.which("mpv") is not None or shutil.which("ffplay") is not None


def mpv_look(title: str, fullscreen: bool = True) -> list[str]:
    """What every player window has in common: the best scaling the GPU offers, and either the whole screen or a small window."""
    args = ["--no-config", "--no-terminal", "--profile=high-quality",
            "--title=" + title.replace("$", "$$")]  # mpv expands ${…} in titles
    args += ["--fs"] if fullscreen else ["--geometry=480x480", "--auto-window-resize=no"]
    mpris = next((p for p in MPRIS_SCRIPTS if os.path.exists(p)), None)
    return args + [f"--script={mpris}"] if mpris else args


def viewer_command(title: str) -> list[str]:
    """The viewer to run, reading the stream from its stdin and tuned for delay over smoothness."""
    if shutil.which("mpv"):
        return ["mpv", *mpv_look(title), "--profile=low-latency", "--untimed", "--no-cache",
                "--no-demuxer-thread", "--demuxer-lavf-format=h264", "--force-window=immediate",
                "--osc=no", "--keep-open=no", "-"]
    if shutil.which("ffplay"):
        return ["ffplay", "-hide_banner", "-loglevel", "error", "-fs", "-fflags", "nobuffer", "-flags", "low_delay",
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


def proxy_env() -> dict[str, str]:
    """The desktop's proxy (GNOME's settings), which a service doesn't inherit from a login shell.

    Where a site can only be reached through one, yt-dlp and the player need it too."""
    if any(os.environ.get(k) for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")):
        return {}

    def setting(schema: str, key: str):
        out = subprocess.run(["gsettings", "get", schema, key], capture_output=True, text=True, timeout=3)
        return ast.literal_eval(out.stdout.strip()) if out.returncode == 0 else None

    env: dict[str, str] = {}
    try:
        if setting("org.gnome.system.proxy", "mode") != "manual":
            return {}
        for scheme, url_scheme, names in (("http", "http", ("http_proxy", "HTTP_PROXY")),
                                          ("https", "http", ("https_proxy", "HTTPS_PROXY")),
                                          ("socks", "socks5", ("all_proxy", "ALL_PROXY"))):
            host, port = setting(f"org.gnome.system.proxy.{scheme}", "host"), setting(f"org.gnome.system.proxy.{scheme}", "port")
            if host and port:
                env.update({n: f"{url_scheme}://{host}:{port}" for n in names})
        if env:  # always spare our own file server
            ignore = setting("org.gnome.system.proxy", "ignore-hosts") or []
            local = ",".join(dict.fromkeys([*ignore, "localhost", "127.0.0.1"]))
            env.update({"no_proxy": local, "NO_PROXY": local})
    except (OSError, subprocess.SubprocessError, ValueError, SyntaxError):
        return {}
    return env


class ViewerSink:
    """Feeds the stream to a viewer window; closing the window is how the user stops it.

    A thread does the blocking writes, because a viewer that falls behind mustn't hold up the
    connection, which everything else travels over too. It also holds the picture back by
    sync_delay() seconds, as the sound takes longer to reach the speakers than frames do the screen.
    """

    def __init__(self, title: str):
        self.pending: deque[tuple[float, bytes]] = deque()
        self.buffered = 0
        self.sync_delay = lambda: 0.0
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
            if self.buffered + len(data) > MAX_BUFFER:
                self.dropped += self.buffered
                self.pending.clear()
                self.buffered = 0
            self.pending.append((time.monotonic(), data))
            self.buffered += len(data)
            self.cond.notify()

    def _delay(self) -> float:
        try:
            return min(MAX_DELAY, max(0.0, float(self.sync_delay())))
        except (TypeError, ValueError):
            return 0.0

    def _run(self) -> None:
        out = self.proc.stdin
        try:
            while True:
                with self.cond:
                    while not self.pending and not self.closed:
                        self.cond.wait()
                    if self.closed:
                        return
                    arrived, piece = self.pending[0]
                    wait = arrived + self._delay() - time.monotonic()
                    if wait > 0:
                        self.cond.wait(min(wait, 0.05))
                        continue
                    self.pending.popleft()
                    self.buffered -= len(piece)
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
