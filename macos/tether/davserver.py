"""Local WebDAV bridge so Finder can browse connected phones.

Listens on 127.0.0.1 only; the URL carries a random token:
    http://127.0.0.1:47292/<token>/<device>/<share>/...
    http://127.0.0.1:47292/<token>/Phone/...     the connected phone; when it shares
                                                 a single folder, that folder itself

Finder mounts a WebDAV server read-only unless it supports locking, so LOCK
and UNLOCK are answered (the phone has no locks; they always succeed). The
AppleDouble (._*) and .DS_Store files Finder writes alongside everything are
accepted and thrown away rather than cluttering the phone.
"""

import asyncio
import html
import logging
import mimetypes
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import formatdate
from urllib.parse import quote, unquote, urlparse
from xml.sax.saxutils import escape

from aiohttp import web

from .errors import ProtoError

log = logging.getLogger("tether.dav")

STATUS = {
    "not_found": 404,
    "exists": 405,
    "denied": 403,
    "not_dir": 409,
    "is_dir": 409,
    "bad_request": 400,
    "changed": 409,
    "unsupported": 501,
    "cancelled": 499,
}

ALLOW = "OPTIONS, GET, HEAD, PUT, DELETE, MKCOL, MOVE, COPY, PROPFIND, PROPPATCH, LOCK, UNLOCK"
XML = 'application/xml; charset="utf-8"'


SHORTCUT = "Phone"


def finder_junk(name: str) -> bool:
    return name.startswith("._") or name in (".DS_Store", ".localized", ".hidden", ".ql_disablethumbnails",
                                             ".ql_disablecache", ".metadata_never_index",
                                             ".metadata_never_index_unless_rootfs", ".metadata_direct_scope_only")


def lock_xml(href: str, token: str, depth: str, owner: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
        "<D:locktype><D:write/></D:locktype><D:lockscope><D:exclusive/></D:lockscope>"
        f"<D:depth>{escape(depth)}</D:depth>{owner}<D:timeout>Second-3600</D:timeout>"
        f"<D:locktoken><D:href>{escape(token)}</D:href></D:locktoken>"
        f"<D:lockroot><D:href>{escape(href)}</D:href></D:lockroot>"
        "</D:activelock></D:lockdiscovery></D:prop>"
    )


@dataclass
class Mount:
    dev: str        # first URL segment
    session: object
    prefix: str     # phone-side path the URL segment stands for ("" = the share list)

    @property
    def ch(self):
        return self.session.channel


def http_date(ms: int) -> str:
    return formatdate(max(ms, 0) / 1000, usegmt=True)


def response_xml(href: str, name: str, is_dir: bool, size: int, mtime: int) -> str:
    if is_dir:
        rt = "<D:resourcetype><D:collection/></D:resourcetype>"
        extra = ""
    else:
        rt = "<D:resourcetype/>"
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        extra = f"<D:getcontentlength>{size}</D:getcontentlength><D:getcontenttype>{ctype}</D:getcontenttype>"
    return (
        f"<D:response><D:href>{escape(href)}</D:href><D:propstat><D:prop>"
        f"<D:displayname>{escape(name)}</D:displayname>{rt}"
        f"<D:getlastmodified>{http_date(mtime)}</D:getlastmodified>{extra}"
        f"</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
    )


def multistatus(parts: list[str]) -> web.Response:
    body = '<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">' + "".join(parts) + "</D:multistatus>"
    return web.Response(status=207, body=body.encode(), content_type="application/xml", charset="utf-8")


class DavServer:
    def __init__(self, node):
        self.node = node
        self.token = node.cfg["dav_token"]
        self._shares: dict[str, tuple] = {}

    async def start(self) -> None:
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", self.node.cfg["dav_port"])
        await site.start()
        log.info("WebDAV bridge on dav://127.0.0.1:%d/%s/", self.node.cfg["dav_port"], self.token)

    # -- helpers -------------------------------------------------------------

    def devices(self) -> dict:
        out = {}
        for s in self.node.connected():
            label = re.sub(r"[/\\\x00-\x1f]", "_", s.name).strip() or "device"
            if label in out:
                label = f"{label} ({s.peer_id[:6]})"
            out[label] = s
        return out

    async def mount(self, dev: str) -> Mount | None:
        if dev == SHORTCUT:
            conn = self.node.connected()
            s = next((s for s in conn if s.type == "phone"), conn[0] if conn else None)
            if s is None:
                return None
            shares = await self.shares(s)
            return Mount(dev, s, "/" + shares[0] if len(shares) == 1 else "")
        s = self.devices().get(dev)
        return Mount(dev, s, "") if s else None

    async def shares(self, s) -> list[str]:
        hit = self._shares.get(s.peer_id)
        if hit and hit[0] > time.monotonic() and hit[2] is s:
            return hit[1]
        res = await s.channel.request({"t": "fs_list", "path": "/"})
        names = [e["name"] for e in res["entries"] if e["dir"]]
        self._shares[s.peer_id] = (time.monotonic() + 5, names, s)
        return names

    def parse(self, raw_path: str):
        parts = [p for p in raw_path.split("/") if p]
        if not parts or parts[0] != self.token:
            raise web.HTTPNotFound()
        return parts[1:]

    def href(self, m: "Mount | str | None", vpath: str = "/", is_dir: bool = True) -> str:
        """URL for a phone-side path as seen through mount m."""
        h = f"/{self.token}/"
        if m is not None:
            dev = m if isinstance(m, str) else m.dev
            if isinstance(m, Mount) and m.prefix:
                vpath = vpath[len(m.prefix):] or "/"
            h += quote(dev) + quote(vpath, safe="/")
            if is_dir and not h.endswith("/"):
                h += "/"
        return h

    # -- entry point --------------------------------------------------------

    async def handle(self, request: web.Request) -> web.StreamResponse:
        try:
            parts = self.parse(request.path)
            if request.method == "OPTIONS":
                return web.Response(headers={"DAV": "1, 2", "Allow": ALLOW, "MS-Author-Via": "DAV"})
            if not parts:
                return await self.root(request)
            mnt = await self.mount(parts[0])
            if mnt is None:
                if parts[0] == SHORTCUT and len(parts) == 1 and request.method == "PROPFIND":
                    # Nothing connected: an empty folder keeps the bookmark mountable.
                    return multistatus([response_xml(self.href(SHORTCUT), SHORTCUT, True, 0, 0)])
                raise web.HTTPNotFound()
            if len(parts) > 1 and finder_junk(parts[-1]):
                return await self.junk(request, mnt, mnt.prefix + "/" + "/".join(parts[1:]))
            vpath = mnt.prefix + "/" + "/".join(parts[1:])
            if mnt.prefix:
                vpath = vpath.rstrip("/")
            m = getattr(self, "do_" + request.method.lower(), None)
            if m is None:
                raise web.HTTPMethodNotAllowed(request.method, ALLOW.split(", "))
            return await m(request, mnt.ch, mnt, vpath)
        except ProtoError as e:
            return web.Response(status=STATUS.get(e.code, 500), text=e.code)
        except (ConnectionError, asyncio.TimeoutError):
            return web.Response(status=502, text="device disconnected")

    async def root(self, request):
        if request.method != "PROPFIND":
            if request.method in ("GET", "HEAD"):
                links = "".join(f'<li><a href="{self.href(d)}">{html.escape(d)}</a></li>' for d in self.devices())
                return web.Response(text=f"<ul>{links}</ul>", content_type="text/html")
            raise web.HTTPMethodNotAllowed(request.method, ["PROPFIND", "GET", "OPTIONS"])
        parts = [response_xml(self.href(None), "Tether", True, 0, 0)]
        if request.headers.get("Depth", "1") != "0":
            parts += [response_xml(self.href(d), d, True, 0, 0) for d in self.devices()]
        return multistatus(parts)

    async def junk(self, request, mnt, vpath):
        """Finder metadata: swallow writes, report it missing otherwise."""
        m = request.method
        if m == "PUT":
            async for _ in request.content.iter_chunked(64 * 1024):
                pass
            return web.Response(status=201)
        if m in ("DELETE", "UNLOCK"):
            return web.Response(status=204)
        if m == "LOCK":
            return await self.do_lock(request, mnt.ch, mnt, vpath)
        if m == "PROPPATCH":
            return multistatus([])
        raise web.HTTPNotFound()

    # -- methods --------------------------------------------------------------

    async def do_lock(self, request, ch, dev, vpath):
        body = await request.read()
        owner = ""
        if body:
            try:
                o = ET.fromstring(body).find("{DAV:}owner")
                if o is not None:
                    owner = "<D:owner>" + escape("".join(o.itertext()).strip()) + "</D:owner>"
            except ET.ParseError:
                pass
        token = request.headers.get("If", "")
        m = re.search(r"<(opaquelocktoken:[^>]+)>", token)
        token = m.group(1) if m else f"opaquelocktoken:{uuid.uuid4()}"  # a refresh keeps its token
        href = self.href(dev, vpath, False)
        xml = lock_xml(href, token, request.headers.get("Depth", "0"), owner)
        return web.Response(status=200, body=xml.encode(), content_type="application/xml", charset="utf-8",
                            headers={"Lock-Token": f"<{token}>"})

    async def do_unlock(self, request, ch, dev, vpath):
        return web.Response(status=204)

    async def _stat(self, ch, vpath):
        return (await ch.request({"t": "fs_stat", "path": vpath}))["entry"]

    async def _exists(self, ch, vpath) -> bool:
        try:
            await self._stat(ch, vpath)
            return True
        except ProtoError as e:
            if e.code == "not_found":
                return False
            raise

    async def do_propfind(self, request, ch, dev, vpath):
        e = await self._stat(ch, vpath)
        name = dev.dev if vpath.rstrip("/") == dev.prefix else vpath.rstrip("/").rsplit("/", 1)[-1]
        parts = [response_xml(self.href(dev, vpath, e["dir"]), name, e["dir"], e["size"], e["mtime"])]
        if e["dir"] and request.headers.get("Depth", "1") != "0":
            res = await ch.request({"t": "fs_list", "path": vpath})
            base = vpath.rstrip("/")
            for c in res["entries"]:
                child = f"{base}/{c['name']}"
                parts.append(response_xml(self.href(dev, child, c["dir"]), c["name"], c["dir"], c["size"], c["mtime"]))
        return multistatus(parts)

    async def do_proppatch(self, request, ch, dev, vpath):
        # Accept and ignore property changes (GVfs sets times this way); report success.
        await self._stat(ch, vpath)
        props = ""
        try:
            root = ET.fromstring(await request.read() or b"<x/>")
            for prop in root.iter("{DAV:}prop"):
                for p in prop:
                    ns, _, local = p.tag[1:].partition("}") if p.tag.startswith("{") else ("", "", p.tag)
                    props += f'<x:{local} xmlns:x="{escape(ns)}"/>' if ns else f"<{local}/>"
        except ET.ParseError:
            pass
        body = (
            f"<D:response><D:href>{escape(self.href(dev, vpath, False))}</D:href><D:propstat>"
            f"<D:prop>{props}</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        )
        return multistatus([body])

    async def do_head(self, request, ch, dev, vpath):
        e = await self._stat(ch, vpath)
        if e["dir"]:
            return web.Response(content_type="text/html")
        return web.Response(headers={
            "Content-Length": str(e["size"]),
            "Content-Type": mimetypes.guess_type(e["name"])[0] or "application/octet-stream",
            "Last-Modified": http_date(e["mtime"]),
            "Accept-Ranges": "bytes",
        })

    async def do_get(self, request, ch, dev, vpath):
        e = await self._stat(ch, vpath)
        if e["dir"]:
            res = await ch.request({"t": "fs_list", "path": vpath})
            base = vpath.rstrip("/")
            items = "".join(
                f'<li><a href="{self.href(dev, base + "/" + c["name"], c["dir"])}">{html.escape(c["name"])}</a></li>'
                for c in res["entries"]
            )
            return web.Response(text=f"<ul>{items}</ul>", content_type="text/html")
        size = e["size"]
        offset, length, status = 0, size, 200
        try:
            rng = request.http_range
        except ValueError:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
        if rng.start is not None or rng.stop is not None:
            start, stop = rng.start, rng.stop
            if start is None:
                start = 0
            if start < 0:
                start = max(size + start, 0)
            stop = size if stop is None else min(stop, size)
            if start >= size or stop <= start:
                raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
            offset, length, status = start, stop - start, 206
        res = await ch.request({"t": "fs_read", "path": vpath, "offset": offset, "length": length})
        inc = res["_in"]
        length = res.get("length", length)
        headers = {
            "Content-Length": str(length),
            "Content-Type": mimetypes.guess_type(e["name"])[0] or "application/octet-stream",
            "Last-Modified": http_date(e["mtime"]),
            "Accept-Ranges": "bytes",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {offset}-{offset + length - 1}/{res.get('size', size)}"
        resp = web.StreamResponse(status=status, headers=headers)
        try:
            await resp.prepare(request)
            async for c in inc.chunks():
                await resp.write(c)
            await resp.write_eof()
        finally:
            if not inc.done:
                inc.abort()
        return resp

    async def do_put(self, request, ch, dev, vpath):
        existed = await self._exists(ch, vpath)

        async def body():
            async for chunk in request.content.iter_chunked(64 * 1024):
                yield chunk

        await ch.request({"t": "fs_write", "path": vpath}, upload=body(), timeout=300)
        return web.Response(status=204 if existed else 201)

    async def do_delete(self, request, ch, dev, vpath):
        await ch.request({"t": "fs_delete", "path": vpath})
        return web.Response(status=204)

    async def do_mkcol(self, request, ch, dev, vpath):
        if request.can_read_body and await request.read():
            return web.Response(status=415)
        await ch.request({"t": "fs_mkdir", "path": vpath})
        return web.Response(status=201)

    async def _destination(self, request, src: Mount):
        dest = request.headers.get("Destination")
        if not dest:
            raise ProtoError("bad_request")
        parts = [unquote(p) for p in urlparse(dest).path.split("/") if p]
        m = await self.mount(parts[1]) if len(parts) >= 2 and parts[0] == self.token else None
        if m is None or m.session is not src.session:
            raise web.HTTPBadGateway(text="cross-device moves are not supported")
        return (m.prefix + "/" + "/".join(parts[2:])).rstrip("/") or "/"

    async def do_move(self, request, ch, dev, vpath):
        dst = await self._destination(request, dev)
        overwrite = request.headers.get("Overwrite", "T").upper() != "F"
        existed = await self._exists(ch, dst)
        if existed and not overwrite:
            return web.Response(status=412)
        await ch.request({"t": "fs_move", "from": vpath, "to": dst, "overwrite": overwrite})
        return web.Response(status=204 if existed else 201)

    async def do_copy(self, request, ch, dev, vpath):
        dst = await self._destination(request, dev)
        overwrite = request.headers.get("Overwrite", "T").upper() != "F"
        e = await self._stat(ch, vpath)
        if e["dir"]:
            raise ProtoError("unsupported")
        existed = await self._exists(ch, dst)
        if existed and not overwrite:
            return web.Response(status=412)
        res = await ch.request({"t": "fs_read", "path": vpath, "offset": 0, "length": -1})
        inc = res["_in"]
        try:
            await ch.request({"t": "fs_write", "path": dst}, upload=inc.chunks(), timeout=300)
        finally:
            if not inc.done:
                inc.abort()
        return web.Response(status=204 if existed else 201)
