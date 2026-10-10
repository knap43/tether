"""Clipboard on macOS (text and images).

The Tether menu-bar app owns the clipboard: it watches NSPasteboard, skips
items password managers mark as concealed, and talks to the daemon over the
control socket. Without the app (a headless install) the daemon falls back to
polling pbpaste, which carries text only.
"""

import asyncio
import base64
import logging
import os
import shutil
import tempfile

from .macos import as_string

log = logging.getLogger("tether.clipboard")

MAX_TEXT = 1024 * 1024
MAX_IMAGE = 16 * 1024 * 1024
POLL = 1.0


async def _run(*args: str, data: bytes | None = None, limit: int = MAX_TEXT) -> bytes | None:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate(data)
    if proc.returncode != 0 or len(out) > limit:
        return None
    return out


class Backend:
    """The app when it's connected, pbpaste/pbcopy otherwise."""

    def __init__(self, on_change, on_image):
        self.on_change = on_change  # callable(text)
        self.on_image = on_image  # callable(mime, bytes)
        self.clients: set = set()  # control connections of the Tether app
        self.has_pb = bool(shutil.which("pbpaste") and shutil.which("pbcopy"))
        self._last: str | None = None

    @property
    def name(self) -> str:
        if self.clients:
            return "app"
        return "pbcopy" if self.has_pb else "none"

    @property
    def available(self) -> bool:
        return bool(self.clients) or self.has_pb

    async def start(self):
        if self.has_pb:
            asyncio.ensure_future(self._poll())

    async def _poll(self):
        while True:
            await asyncio.sleep(POLL)
            if self.clients:
                self._last = None  # pick up from the current value when the app goes away
                continue
            out = await _run("pbpaste", "-Prefer", "txt")
            if out is None:
                continue
            text = out.decode("utf-8", "replace")
            if self._last is None:
                self._last = text  # don't send what was on the clipboard before we started
                continue
            if text and text != self._last:
                self._last = text
                self.on_change(text)

    async def set(self, text: str) -> None:
        if self.clients:
            for c in list(self.clients):
                await c.push({"ev": "clip_set", "text": text})
        elif self.has_pb:
            self._last = text
            await _run("pbcopy", data=text.encode())

    async def set_image(self, mime: str, data: bytes) -> None:
        if self.clients:
            b64 = base64.b64encode(data).decode()
            for c in list(self.clients):
                await c.push({"ev": "clip_image_set", "mime": mime, "data": b64})
            return
        if mime != "image/png" or not shutil.which("osascript"):
            log.info("received a %s image, but only the Tether app can put that on the clipboard", mime)
            return
        fd, path = tempfile.mkstemp(suffix=".png")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            await _run("osascript", "-e",
                       f"set the clipboard to (read (POSIX file {as_string(path)}) as «class PNGf»)")
        finally:
            os.unlink(path)


def choose(_kind: str, on_change, on_image) -> Backend:
    return Backend(on_change, on_image)
