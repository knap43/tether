"""Desktop clipboard backends (text and images).

On GNOME (Wayland or X11) the shell extension owns clipboard access and talks
to the daemon over the control socket, because Mutter does not let ordinary
background clients watch the clipboard. Elsewhere we fall back to
wl-clipboard (wlroots and KDE Wayland) or xclip (X11).
"""

import asyncio
import base64
import hashlib
import logging
import os
import shutil

log = logging.getLogger("tether.clipboard")

MAX_TEXT = 1024 * 1024
MAX_IMAGE = 16 * 1024 * 1024
TEXT_TYPES = ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain", "STRING", "TEXT")
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")


def pick_image_type(types) -> str | None:
    for t in IMAGE_TYPES:
        if t in types:
            return t
    return None


class Backend:
    name = "none"

    def __init__(self, on_change, on_image):
        self.on_change = on_change  # callable(text)
        self.on_image = on_image  # callable(mime, bytes)

    async def start(self):
        pass

    async def set(self, text: str) -> None:
        pass

    async def set_image(self, mime: str, data: bytes) -> None:
        pass

    @property
    def available(self) -> bool:
        return False


class ExtensionBackend(Backend):
    """Driven by the GNOME Shell extension via the control socket."""

    name = "gnome-extension"

    def __init__(self, on_change, on_image):
        super().__init__(on_change, on_image)
        self.clients: set = set()  # control connections that registered as clipboard providers

    @property
    def available(self) -> bool:
        return bool(self.clients)

    async def set(self, text: str) -> None:
        for c in list(self.clients):
            await c.push({"ev": "clip_set", "text": text})

    async def set_image(self, mime: str, data: bytes) -> None:
        b64 = base64.b64encode(data).decode()
        for c in list(self.clients):
            await c.push({"ev": "clip_image_set", "mime": mime, "data": b64})


async def _read(*args: str, limit: int) -> bytes | None:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    if proc.returncode != 0 or len(out) > limit:
        return None
    return out


async def _write(data: bytes, *args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.communicate(data)


class WlBackend(Backend):
    name = "wl-clipboard"

    @property
    def available(self) -> bool:
        return True

    async def start(self):
        asyncio.ensure_future(self._watch())

    async def _watch(self):
        while True:
            # wl-paste runs the given command for every change; we print a marker and re-read.
            proc = await asyncio.create_subprocess_exec(
                "wl-paste", "--watch", "sh", "-c", "echo x",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            while await proc.stdout.readline():
                await self._changed()
            await proc.wait()
            await asyncio.sleep(2)

    async def _changed(self):
        types = await _read("wl-paste", "--list-types", limit=65536)
        types = types.decode(errors="replace").split() if types else []
        if any(t in types for t in TEXT_TYPES):
            out = await _read("wl-paste", "--no-newline", "--type", "text", limit=MAX_TEXT)
            if out is not None:
                self.on_change(out.decode("utf-8", "replace"))
        elif mime := pick_image_type(types):
            out = await _read("wl-paste", "--type", mime, limit=MAX_IMAGE)
            if out:
                self.on_image(mime, out)

    async def set(self, text: str) -> None:
        await _write(text.encode(), "wl-copy", "--type", "text/plain;charset=utf-8")

    async def set_image(self, mime: str, data: bytes) -> None:
        await _write(data, "wl-copy", "--type", mime)


class XclipBackend(Backend):
    name = "xclip"

    @property
    def available(self) -> bool:
        return True

    async def start(self):
        asyncio.ensure_future(self._poll())

    async def _snapshot(self):
        targets = await _read("xclip", "-o", "-selection", "clipboard", "-t", "TARGETS", limit=65536)
        types = targets.decode(errors="replace").split() if targets else []
        if any(t in types for t in TEXT_TYPES):
            out = await _read("xclip", "-o", "-selection", "clipboard", "-t", "UTF8_STRING", limit=MAX_TEXT)
            return ("text", out.decode("utf-8", "replace")) if out is not None else None
        if mime := pick_image_type(types):
            out = await _read("xclip", "-o", "-selection", "clipboard", "-t", mime, limit=MAX_IMAGE)
            return ("image", mime, out) if out else None
        return None

    async def _poll(self):
        last = None
        snap = await self._snapshot()
        if snap:
            last = hashlib.sha256(repr(snap).encode()).hexdigest()
        while True:
            await asyncio.sleep(1)
            snap = await self._snapshot()
            if not snap:
                continue
            h = hashlib.sha256(repr(snap).encode()).hexdigest()
            if h == last:
                continue
            last = h
            if snap[0] == "text":
                self.on_change(snap[1])
            else:
                self.on_image(snap[1], snap[2])

    async def set(self, text: str) -> None:
        await _write(text.encode(), "xclip", "-i", "-selection", "clipboard")

    async def set_image(self, mime: str, data: bytes) -> None:
        await _write(data, "xclip", "-i", "-selection", "clipboard", "-t", mime)


def choose(kind: str, on_change, on_image) -> Backend:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if kind == "auto":
        if "GNOME" in desktop.upper():
            kind = "gnome-extension"
        elif os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
            kind = "wl-clipboard"
        elif os.environ.get("DISPLAY") and shutil.which("xclip"):
            kind = "xclip"
        else:
            kind = "gnome-extension"
    cls = {"gnome-extension": ExtensionBackend, "wl-clipboard": WlBackend, "xclip": XclipBackend}.get(kind, Backend)
    log.info("clipboard backend: %s", cls.name)
    return cls(on_change, on_image)
