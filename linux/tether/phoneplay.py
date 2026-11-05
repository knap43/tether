"""Plays what the phone shares directly on the computer, at its original quality, instead of
capturing the phone's screen: web links through mpv and yt-dlp, files by reading them from the phone."""

import asyncio
import logging
import os
import secrets
import shutil
import subprocess
from urllib.parse import quote, urlsplit

from .errors import ProtoError
from .mpvcontrol import MpvControl
from .phonescreen import display_env, mpv_look, proxy_env

log = logging.getLogger("tether.play")

PROBE_TIMEOUT = 30
MAX_URL = 4096
# YouTube refuses many of its plain stream addresses unless each request names a byte range in the
# address itself, which yt-dlp does and mpv can't, so HLS comes first. The Safari client offers HLS
# for most videos, and the default clients cover the rest.
YTDL_FORMAT = ("bestvideo[protocol^=m3u8]+bestaudio[protocol^=m3u8]/best[protocol^=m3u8]"
               "/bestvideo+bestaudio[audio_channels<=2]/bestvideo+bestaudio/best")
YTDL_CLIENTS = "youtube:player_client=default,web_safari"
SURE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}  # the check would only add seconds


def available() -> bool:
    return shutil.which("mpv") is not None


def valid_url(url) -> bool:
    """Web links only: mpv also opens local files and devices, which a phone has no business naming."""
    if not isinstance(url, str) or len(url) > MAX_URL or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


async def can_play(url: str) -> bool:
    """Whether yt-dlp finds something to play at the link; a page behind a login answers no."""
    if not shutil.which("yt-dlp"):
        return False
    if urlsplit(url).hostname in SURE_HOSTS:
        return True
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--simulate", "--quiet", "--no-warnings", "--no-playlist", "--", url,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, **proxy_env()},
    )
    try:
        return await asyncio.wait_for(proc.wait(), PROBE_TIMEOUT) == 0
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False


def open_in_browser(url: str) -> bool:
    """For what yt-dlp can't fetch: the computer's own browser can, once logged in there."""
    if not shutil.which("xdg-open"):
        return False
    try:
        subprocess.Popen(["xdg-open", url], env={**display_env(), **proxy_env()}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, ProtoError):
        return False
    return True


class Player(MpvControl):
    """An mpv window: fullscreen for video, small for music."""

    def __init__(self, targets: list[str], title: str, fullscreen: bool = True, start: float = 0):
        super().__init__("player")
        self.log_path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", f"tether-player-{os.getpid()}.log")
        # The window waits for the first frame: a link takes many seconds to open, and a black screen says nothing.
        cmd = ["mpv", *mpv_look(title, fullscreen), "--hwdec=auto-safe", "--force-window=yes", "--keep-open=no",
               f"--ytdl-format={YTDL_FORMAT}", f"--ytdl-raw-options-append=extractor-args={YTDL_CLIENTS}",
               "--ytdl-raw-options-append=no-playlist=", f"--log-file={self.log_path}",
               f"--input-ipc-server={self.socket}", f"--start={start}", "--", *targets]
        self.proc = subprocess.Popen(cmd, env={**display_env(), **proxy_env()}, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    @property
    def returncode(self):
        return self.proc.returncode

    def problems(self) -> list[str]:
        """What the player complained of, for the log: it has no terminal to say so on."""
        try:
            with open(self.log_path, errors="replace") as f:
                return [line.strip()[:200] for line in f if "][e][" in line or "][f][" in line][-4:]
        except OSError:
            return []

    def forget_log(self) -> None:
        try:
            os.unlink(self.log_path)
        except OSError:
            pass


class FileServer:
    """Serves the phone's shared files to the player over local HTTP, so that seeking just works:
    each range the player asks for becomes a read from the phone. The address holds a secret,
    since any local program may connect to it."""

    def __init__(self, channel):
        self.channel = channel
        self.token = secrets.token_hex(16)
        self.files: dict[str, dict] = {}
        self.runner = None
        self.port = 0

    async def start(self, files: list[dict]) -> list[str]:
        from aiohttp import web

        self.files = {f["id"]: f for f in files}
        app = web.Application()
        app.router.add_route("*", f"/{self.token}/{{id}}/{{name}}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return [f"http://127.0.0.1:{self.port}/{self.token}/{quote(f['id'], safe='')}/{quote(f['name'], safe='')}"
                for f in files]

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    async def handle(self, request):
        from aiohttp import web

        if request.method not in ("GET", "HEAD"):
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "HEAD"])
        f = self.files.get(request.match_info["id"])
        if f is None:
            raise web.HTTPNotFound()
        size = f["size"]
        offset, length, status = 0, size, 200
        try:
            rng = request.http_range
        except ValueError:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
        if rng.start is not None or rng.stop is not None:
            start = rng.start or 0
            if start < 0:
                start = max(size + start, 0)
            stop = size if rng.stop is None else min(rng.stop, size)
            if start >= size or stop <= start:
                raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
            offset, length, status = start, stop - start, 206
        headers = {"Content-Length": str(length), "Accept-Ranges": "bytes",
                   "Content-Type": f.get("mime") or "application/octet-stream"}
        if status == 206:
            headers["Content-Range"] = f"bytes {offset}-{offset + length - 1}/{size}"
        resp = web.StreamResponse(status=status, headers=headers)
        if request.method == "HEAD":
            await resp.prepare(request)
            return resp
        try:
            res = await self.channel.request({"t": "play_read", "id": f["id"], "offset": offset, "length": length})
        except (ProtoError, ConnectionError):
            raise web.HTTPBadGateway()
        inc = res["_in"]
        try:
            await resp.prepare(request)
            async for chunk in inc.chunks():
                await resp.write(chunk)
        finally:
            if not inc.done:
                inc.abort()
        return resp
