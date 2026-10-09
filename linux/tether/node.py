"""The daemon core: sessions, pairing, clipboard, transfers, browsing, sync."""

import asyncio
import base64
import sys
import hashlib
import json
import logging
import os
import re
import shutil
import signal
from pathlib import Path

from . import clipboard
from .channel import CHUNK, Channel
from .config import Config, Paths, PeerStore
from .crypto import HandshakeError, Identity, b64e, handshake
from .desknotify import REASON_DISMISSED, DesktopNotifier
from .discovery import Discovery
from .errors import ProtoError, from_oserror
from .fsops import TMP_PREFIX, Shares
from .media import Media
from .transfers import Transfer, Transfers
from .sync import BaseState, FolderIndex, now_ms, plan, remove_empty_parents, safe_rel, tmp_path_for

log = logging.getLogger("tether")

PAIR_TIMEOUT = 120
RECONNECT_INTERVAL = 30
MAX_UNTRUSTED = 4
INDEX_PART = 5000
MAX_OUTPUT = 64 * 1024


async def run_command(line: str, timeout: float) -> dict:
    """Runs a shell command line; returns exit status and combined output (capped)."""
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", line,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=os.path.expanduser("~"), start_new_session=True,
    )
    buf = bytearray()
    truncated = False

    async def pump():
        nonlocal truncated
        while chunk := await proc.stdout.read(65536):
            room = MAX_OUTPUT - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True

    reader = asyncio.ensure_future(pump())
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
    # Programs started in the background may keep the pipe open; don't wait for them.
    try:
        await asyncio.wait_for(asyncio.shield(reader), 1.0)
    except asyncio.TimeoutError:
        reader.cancel()
    return {"exit": proc.returncode, "output": buf.decode("utf-8", "replace"),
            "truncated": truncated, "timed_out": timed_out}


async def file_chunks(f, length: int, transfer: Transfer | None = None):
    left = length
    while left > 0:
        data = await asyncio.to_thread(f.read, min(CHUNK * 4, left))
        if not data:
            raise ProtoError("changed", "file shrank while sending")
        left -= len(data)
        if transfer is not None:
            transfer.add(len(data))
        yield data


async def bytes_chunks(data: bytes):
    for i in range(0, len(data), CHUNK):
        yield data[i : i + CHUNK]


async def read_all(inc, limit: int) -> bytes:
    buf = bytearray()
    async for c in inc.chunks():
        buf.extend(c)
        if len(buf) > limit:
            inc.abort()
            raise ProtoError("bad_request", "too large")
    return bytes(buf)


def disk_state(path):
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        return None


def clean_name(name) -> str:
    name = os.path.basename(str(name or "")).replace("\0", "")
    name = re.sub(r"[\x00-\x1f]", "", name).strip()
    if name in ("", ".", "..") or name.startswith(".tether"):
        name = "file"
    return name[:200]


def unique_path(d: Path, name: str) -> Path:
    p = d / name
    stem, ext = os.path.splitext(name)
    i = 1
    while p.exists():
        p = d / f"{stem} ({i}){ext}"
        i += 1
    return p


class Session:
    def __init__(self, node: "Node", hs, is_client: bool, addr):
        self.node = node
        self.hs = hs
        self.peer_id = hs.peer_id
        self.name = hs.peer_name
        self.type = hs.peer_type
        self.addr = addr
        self.is_client = is_client
        self.client_id = node.identity.id if is_client else hs.peer_id
        self.trusted = hs.peer_id in node.peers
        self.channel = Channel(hs.framer, is_client, node.handle, label=self.name)
        self.channel.session = self
        self.pair: dict | None = None
        self.remote_index: dict[str, dict] = {}
        self.index_parts: dict[str, dict] = {}
        self.bases: dict[str, BaseState] = {}
        self.sent_digest: dict[str, str] = {}
        self.sync_event = asyncio.Event()
        self.index_lock = asyncio.Lock()
        self.battery: dict | None = None
        self.notifs: dict[str, int] = {}  # phone notification key -> desktop notification id

    def info(self) -> dict:
        return {"id": self.peer_id, "name": self.name, "type": self.type, "addr": self.addr[0] if self.addr else None}


class Node:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.cfg = Config(paths)
        self.identity = Identity.load_or_create(paths.identity_file)
        self.peers = PeerStore(paths.peers_file)
        self.sessions: dict[str, Session] = {}
        self.untrusted: set[Session] = set()
        self.connecting: set[str] = set()
        self.shares = Shares(self.cfg["shares"])
        self.indexes: dict[str, FolderIndex] = {}
        self.scan_lock = asyncio.Lock()
        self.subscribers: set = set()
        self.discovery = Discovery(self, self.cfg["discovery_port"])
        self.clip = clipboard.choose(self.cfg["clipboard_backend"], self.on_local_clip, self.on_local_clip_image)
        self.last_clip = None
        self.last_clip_image: str | None = None
        self.transfers = Transfers(self.emit)
        self.media = Media(self._on_media_change)
        self.desk = DesktopNotifier(self._on_notif_action, self._on_notif_closed)
        self.notif_map: dict[int, dict] = {}  # desktop id -> {peer, key, title, app, acted}
        self.notif_apps: dict[str, str] = {}  # package -> label, seen this run
        self.low_battery: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self.load_sync_folders()

    # -- lifecycle ----------------------------------------------------------

    def spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, t):
        self.tasks.discard(t)
        if not t.cancelled() and t.exception():
            log.warning("background task failed: %r", t.exception())

    async def start(self, with_dav: bool = True, with_discovery: bool = True) -> None:
        self.server = await asyncio.start_server(self._accept, "0.0.0.0", self.cfg["port"])
        log.info("device %s (%s) listening on %d", self.cfg["name"], self.identity.id[:12], self.cfg["port"])
        if with_discovery:
            await self.discovery.start()
        await self.clip.start()
        if with_dav:
            from .davserver import DavServer

            self.dav = DavServer(self)
            await self.dav.start()
        self.spawn(self._scan_loop())
        self.spawn(self._reconnect_loop())
        self.spawn(self.media.watch())
        self.spawn(self._media_poll())
        self.spawn(self.desk.watch())

    # -- connections --------------------------------------------------------

    def is_connected(self, peer_id: str) -> bool:
        s = self.sessions.get(peer_id)
        return bool(s and not s.channel.closed.is_set())

    def connected(self) -> list[Session]:
        return [s for s in self.sessions.values() if not s.channel.closed.is_set()]

    async def _accept(self, reader, writer):
        s = await self._establish(reader, writer, False, writer.get_extra_info("peername"))
        if s:
            await self._serve(s)

    async def connect(self, host: str, port: int) -> Session:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
        s = await self._establish(reader, writer, True, (host, port))
        if s is None:
            raise ConnectionError("handshake failed or duplicate session")
        self.spawn(self._serve(s))
        return s

    async def _establish(self, reader, writer, is_client, addr) -> Session | None:
        try:
            hs = await handshake(reader, writer, self.identity, is_client, self.cfg["name"],
                                 self.cfg.get("device_type", "desktop"))
        except (HandshakeError, asyncio.TimeoutError, asyncio.IncompleteReadError, OSError, ValueError) as e:
            log.info("handshake with %s failed: %s", addr, e)
            writer.close()
            return None
        s = Session(self, hs, is_client, addr)
        if s.trusted:
            if not self._register(s):
                hs.framer.close()
                return None
            if is_client:
                self.peers.set_addr(s.peer_id, addr, s.name)
            self.spawn(self._on_trusted(s))
        else:
            if len(self.untrusted) >= MAX_UNTRUSTED:
                hs.framer.close()
                return None
            self.untrusted.add(s)
            s.channel.spawn(self._expire_untrusted(s))
        log.info("session with %s (%s, %s)", s.name, s.peer_id[:12], "trusted" if s.trusted else "unpaired")
        return s

    async def _expire_untrusted(self, s: Session) -> None:
        await asyncio.sleep(PAIR_TIMEOUT)
        if not s.trusted and s.pair is None:
            s.channel.close()

    def _register(self, s: Session) -> bool:
        old = self.sessions.get(s.peer_id)
        if old is not None and old is not s and not old.channel.closed.is_set():
            smaller = min(self.identity.id, s.peer_id)
            if not (s.client_id == smaller or old.client_id != smaller):
                return False
            old.channel.close()
        self.sessions[s.peer_id] = s
        return True

    async def _serve(self, s: Session) -> None:
        try:
            await s.channel.run()
        finally:
            self.untrusted.discard(s)
            if self.sessions.get(s.peer_id) is s:
                del self.sessions[s.peer_id]
                self.emit_status()
                for nid in list(s.notifs.values()):
                    self.notif_map.pop(nid, None)
                    self.spawn(self.desk.close(nid))

    async def _on_trusted(self, s: Session) -> None:
        self.emit_status()
        s.channel.spawn(self._sync_worker(s))
        if s.type == "phone":
            await self._send_host_info(s)
        if s.type == "phone" and (self.media.available or not self.cfg["media"]):
            await s.channel.send({"t": "media_update", **await self._media_state()})
        await self._send_indexes(s)

    def on_beacon(self, peer_id: str, host: str, port: int) -> None:
        if peer_id not in self.peers:
            return
        self.peers.set_addr(peer_id, (host, port))
        if self.identity.id < peer_id:
            self.spawn(self._try_connect(peer_id, host, port))

    async def _try_connect(self, peer_id, host, port) -> None:
        pairing = any(s.peer_id == peer_id and not s.channel.closed.is_set() for s in self.untrusted)
        if self.is_connected(peer_id) or pairing or peer_id in self.connecting:
            return
        self.connecting.add(peer_id)
        try:
            await self.connect(host, port)
        except (OSError, asyncio.TimeoutError, ConnectionError) as e:
            log.debug("connect to %s:%s failed: %s", host, port, e)
        finally:
            self.connecting.discard(peer_id)

    async def _reconnect_loop(self) -> None:
        while True:
            for pid, p in list(self.peers.peers.items()):
                if p.get("addr") and not self.is_connected(pid):
                    self.spawn(self._try_connect(pid, p["addr"][0], p["addr"][1]))
            for hp in self.cfg["static_peers"]:
                host, _, port = hp.rpartition(":")
                if host and not any(s.addr and s.addr[0] == host for s in self.connected()):
                    self.spawn(self._try_connect("static:" + hp, host, int(port)))
            await asyncio.sleep(RECONNECT_INTERVAL)

    # -- dispatch -----------------------------------------------------------

    async def handle(self, ch: Channel, msg: dict) -> None:
        s: Session = ch.session
        t = msg.get("t")
        if t in ("pair_req", "pair_ok", "pair_no"):
            return await self._on_pair(s, t)
        if t == "unpaired":
            if self.peers.remove(s.peer_id):
                log.info("%s forgot us; unpaired", s.name)
                self.notify("Unpaired", f"{s.name} no longer trusts this computer")
            ch.close()
            return
        if not s.trusted:
            # Mid-pairing the peer may already trust us; don't make it forget us.
            if self._find_pairing(s.peer_id) is None:
                await ch.send({"t": "unpaired"})
            ch.close()
            return
        h = getattr(self, "_h_" + str(t), None)
        if h is None:
            raise ProtoError("unsupported")
        await h(s, msg)

    # -- pairing ------------------------------------------------------------

    def _find_pairing(self, peer_id) -> Session | None:
        for s in list(self.untrusted) + list(self.sessions.values()):
            if s.peer_id == peer_id and s.pair is not None and not s.channel.closed.is_set():
                return s
        return None

    async def start_pairing(self, peer_id: str | None = None, addr: str | None = None) -> Session:
        if addr:
            host, _, port = addr.rpartition(":")
            host, port = (host, int(port)) if host else (addr, self.cfg["port"])
        else:
            d = self.discovery.recent().get(peer_id)
            if not d:
                raise ProtoError("not_found", "device not seen on the network")
            host, port = d["addr"], d["port"]
        s = await self.connect(host, port)
        s.pair = {"local": None, "remote": None, "initiator": True}
        await s.channel.send({"t": "pair_req"})
        self._pair_prompt(s)
        return s

    def _pair_prompt(self, s: Session) -> None:
        self.emit({"ev": "pair_request", "id": s.peer_id, "name": s.name, "sas": s.hs.sas,
                   "initiator": s.pair["initiator"]})
        if not s.pair["initiator"]:
            self.notify("Pairing request", f"{s.name} wants to pair. Code: {s.hs.sas}")
        s.channel.spawn(self._pair_timeout(s))

    async def _pair_timeout(self, s: Session) -> None:
        await asyncio.sleep(PAIR_TIMEOUT)
        if s.pair is not None:
            s.pair = None
            self.emit({"ev": "pair_failed", "id": s.peer_id, "reason": "timeout"})
            if not s.trusted:
                s.channel.close()

    async def _on_pair(self, s: Session, t: str) -> None:
        if t == "pair_req":
            if s.pair is None:
                s.pair = {"local": None, "remote": None, "initiator": False}
                self._pair_prompt(s)
        elif t == "pair_ok":
            if s.pair is not None:
                s.pair["remote"] = True
                self._maybe_paired(s)
        elif t == "pair_no":
            if s.pair is not None:
                s.pair = None
                self.emit({"ev": "pair_failed", "id": s.peer_id, "reason": "rejected"})
                if not s.trusted:
                    s.channel.close()

    async def confirm_pairing(self, peer_id: str, accept: bool) -> None:
        s = self._find_pairing(peer_id)
        if s is None:
            raise ProtoError("not_found", "no pairing in progress with that device")
        if not accept:
            s.pair = None
            await s.channel.send({"t": "pair_no"})
            self.emit({"ev": "pair_failed", "id": peer_id, "reason": "rejected"})
            if not s.trusted:
                s.channel.close()
            return
        s.pair["local"] = True
        await s.channel.send({"t": "pair_ok"})
        self._maybe_paired(s)

    def _maybe_paired(self, s: Session) -> None:
        if not (s.pair and s.pair["local"] and s.pair["remote"]):
            return
        s.pair = None
        self.peers.add(s.peer_id, s.name, b64e(s.hs.peer_spki), s.type, list(s.addr) if s.is_client else None)
        s.trusted = True
        self.untrusted.discard(s)
        old = self.sessions.get(s.peer_id)
        if old is not None and old is not s:
            old.channel.close()
        self.sessions[s.peer_id] = s
        log.info("paired with %s", s.name)
        self.emit({"ev": "paired", "id": s.peer_id, "name": s.name})
        self.notify("Paired", f"Paired with {s.name}")
        self.spawn(self._on_trusted(s))

    async def unpair(self, peer_id: str) -> None:
        s = self.sessions.get(peer_id)
        if s:
            try:
                await s.channel.send({"t": "unpaired"})
            except ConnectionError:
                pass
            s.channel.close()
        if not self.peers.remove(peer_id):
            raise ProtoError("not_found")
        for f in self.paths.sync_dir.glob(f"base-{peer_id[:16]}-*.json"):
            f.unlink(missing_ok=True)
        self.emit_status()

    # -- clipboard ----------------------------------------------------------

    def on_local_clip(self, text: str) -> None:
        if not self.cfg["clipboard"] or not isinstance(text, str) or not text:
            return
        if len(text.encode()) > clipboard.MAX_TEXT or text == self.last_clip:
            return
        self.last_clip = text
        for s in self.connected():
            s.channel.spawn(s.channel.send({"t": "clip", "text": text}))

    async def _h_clip(self, s: Session, msg: dict) -> None:
        text = msg.get("text")
        if not isinstance(text, str):
            raise ProtoError("bad_request")
        if not self.cfg["clipboard"]:
            return
        self.last_clip = text
        await self.clip.set(text)
        self.emit({"ev": "clip_received", "from": s.peer_id, "length": len(text)})

    # -- quick send ---------------------------------------------------------

    async def send_file(self, peer_id: str, path: str) -> dict:
        s = self.sessions.get(peer_id)
        if s is None or s.channel.closed.is_set():
            raise ProtoError("not_found", "device not connected")
        p = Path(path).expanduser()
        if not p.is_file():
            raise ProtoError("not_found", f"{p} is not a file")

        def digest():
            h = hashlib.sha256()
            with open(p, "rb") as f:
                while b := f.read(1 << 20):
                    h.update(b)
            return h.hexdigest()

        sha = await asyncio.to_thread(digest)
        with open(p, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            t = self.transfers.start(p.name, size, "out", s.peer_id, s.name)
            try:
                res = await s.channel.request(
                    {"t": "send", "name": p.name, "size": size, "sha256": sha}, upload=file_chunks(f, size, t)
                )
            except BaseException:
                t.finish("failed")
                raise
            t.finish("done")
        return {"path": str(p), "saved_as": res.get("name")}

    async def _h_send(self, s: Session, msg: dict) -> None:
        inc = msg["_in"]
        name = clean_name(msg.get("name"))
        size, sha = msg.get("size"), msg.get("sha256")
        d = self.cfg.download_dir
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{TMP_PREFIX}{os.urandom(6).hex()}"
        h = hashlib.sha256()
        n = 0
        t = self.transfers.start(name, size if isinstance(size, int) else None, "in", s.peer_id, s.name)
        t.incoming = inc
        try:
            with open(tmp, "wb") as f:
                async for c in inc.chunks():
                    await asyncio.to_thread(f.write, c)
                    h.update(c)
                    n += len(c)
                    t.add(len(c))
            if t.cancelled:
                raise ProtoError("cancelled")
            if (isinstance(size, int) and n != size) or (isinstance(sha, str) and h.hexdigest() != sha):
                raise ProtoError("io", "integrity check failed")
            final = unique_path(d, name)
            os.replace(tmp, final)
        except BaseException:
            t.finish("failed")
            raise
        finally:
            tmp.unlink(missing_ok=True)
        t.finish("done")
        await s.channel.reply(msg, name=final.name)
        log.info("received %s from %s", final, s.name)
        self.emit({"ev": "file_received", "from": s.peer_id, "path": str(final)})
        self.notify(f"File from {s.name}", final.name)

    # -- browsing (we are the server) -----------------------------------------

    async def _h_fs_list(self, s, msg):
        await s.channel.reply(msg, entries=await asyncio.to_thread(self.shares.list, msg.get("path")))

    async def _h_fs_stat(self, s, msg):
        await s.channel.reply(msg, entry=await asyncio.to_thread(self.shares.stat, msg.get("path")))

    async def _h_fs_read(self, s, msg):
        offset, length = msg.get("offset", 0), msg.get("length", -1)
        if not isinstance(offset, int) or not isinstance(length, int):
            raise ProtoError("bad_request")
        f, size, length = self.shares.open_read(msg.get("path"), offset, length)
        with f:
            await s.channel.reply_stream(msg, file_chunks(f, length), size=size, length=length)

    async def _h_fs_write(self, s, msg):
        inc = msg["_in"]
        tmp, final = self.shares.begin_write(msg.get("path"))
        try:
            with open(tmp, "wb") as f:
                async for c in inc.chunks():
                    await asyncio.to_thread(f.write, c)
            os.replace(tmp, final)
        except OSError as e:
            raise from_oserror(e) from None
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        await s.channel.reply(msg)

    async def _h_fs_mkdir(self, s, msg):
        await asyncio.to_thread(self.shares.mkdir, msg.get("path"))
        await s.channel.reply(msg)

    async def _h_fs_delete(self, s, msg):
        await asyncio.to_thread(self.shares.delete, msg.get("path"))
        await s.channel.reply(msg)

    async def _h_fs_move(self, s, msg):
        await asyncio.to_thread(self.shares.move, msg.get("from"), msg.get("to"), bool(msg.get("overwrite")))
        await s.channel.reply(msg)

    # -- sync ---------------------------------------------------------------

    def load_sync_folders(self) -> None:
        self.paths.sync_dir.mkdir(parents=True, exist_ok=True)
        self.indexes = {
            fid: FolderIndex(fid, path, self.paths.sync_dir) for fid, path in self.cfg.sync_folders().items()
        }

    async def _scan_loop(self) -> None:
        while True:
            await self.rescan()
            await asyncio.sleep(self.cfg["scan_interval"])

    async def rescan(self) -> None:
        changed = False
        async with self.scan_lock:
            for idx in list(self.indexes.values()):
                if await asyncio.to_thread(idx.scan):
                    changed = True
        if changed:
            await self._broadcast_indexes()

    async def _broadcast_indexes(self) -> None:
        for s in self.connected():
            s.channel.spawn(self._send_indexes(s))
            s.sync_event.set()

    async def _send_indexes(self, s: Session) -> None:
        async with s.index_lock:
            await self._send_indexes_locked(s)

    async def _send_indexes_locked(self, s: Session) -> None:
        for fid, idx in list(self.indexes.items()):
            wire = idx.wire()
            digest = hashlib.sha256(json.dumps(wire, sort_keys=True).encode()).hexdigest()
            if s.sent_digest.get(fid) == digest:
                continue
            s.sent_digest[fid] = digest
            items = list(wire.items())
            for i in range(0, max(len(items), 1), INDEX_PART):
                part = dict(items[i : i + INDEX_PART])
                more = i + INDEX_PART < len(items)
                await s.channel.send({"t": "sync_index", "folder": fid, "files": part, "more": more})

    async def _h_sync_index(self, s, msg):
        fid, files = msg.get("folder"), msg.get("files")
        if not isinstance(fid, str) or not isinstance(files, dict):
            raise ProtoError("bad_request")
        acc = s.index_parts.setdefault(fid, {})
        acc.update(files)
        if not msg.get("more"):
            s.remote_index[fid] = s.index_parts.pop(fid)
            s.sync_event.set()

    async def _h_sync_get(self, s, msg):
        idx = self.indexes.get(msg.get("folder"))
        if idx is None:
            raise ProtoError("not_found")
        rel = safe_rel(msg.get("path"))
        e = idx.files.get(rel)
        if not e or e["h"] != msg.get("hash") or not idx.matches_disk(rel):
            raise ProtoError("changed")
        try:
            f = open(os.path.join(idx.root, rel), "rb")
        except OSError as err:
            raise from_oserror(err) from None
        with f:
            size = os.fstat(f.fileno()).st_size
            await s.channel.reply_stream(msg, file_chunks(f, size), size=size, mtime=e["m"])

    async def _sync_worker(self, s: Session) -> None:
        while True:
            await s.sync_event.wait()
            s.sync_event.clear()
            acted = False
            for fid, remote in list(s.remote_index.items()):
                idx = self.indexes.get(fid)
                if idx is not None:
                    acted |= await self._sync_folder(s, idx, remote)
            if acted:
                await self._broadcast_indexes()

    async def _sync_folder(self, s: Session, idx: FolderIndex, remote: dict) -> bool:
        base = s.bases.get(idx.id)
        if base is None:
            base = s.bases[idx.id] = BaseState(self.paths.sync_dir, s.peer_id, idx.id)
        if not idx.root.is_dir():
            return False
        async with self.scan_lock:
            actions = plan(idx.wire(), remote, base, self.identity.id, s.peer_id)
        base.save()
        done = False
        for action, rel, r in actions:
            try:
                done |= await self._apply(s, idx, base, action, rel, r)
            except ProtoError as e:
                log.info("sync %s/%s: %s", idx.id, rel, e)
            except OSError as e:
                log.warning("sync %s/%s: %s", idx.id, rel, e)
            base.save()
        if done:
            idx.save()
        return done

    async def _apply(self, s, idx: FolderIndex, base: BaseState, action, rel, r) -> bool:
        target = os.path.join(idx.root, rel)
        if action == "delete":
            async with self.scan_lock:
                if not idx.matches_disk(rel):
                    return False
                try:
                    os.remove(target)
                except FileNotFoundError:
                    pass
                remove_empty_parents(idx.root, rel)
                if rel in idx.files:
                    idx.files[rel] = {"h": None, "s": 0, "m": now_ms(), "n": 0}
                base.set(rel, None)
            log.info("sync %s: deleted %s", idx.id, rel)
            return True

        async with self.scan_lock:
            if not idx.matches_disk(rel):
                return False
            if action == "conflict":
                cname = sync_conflict_name(idx.root, rel)
                os.rename(target, os.path.join(idx.root, cname))
                log.info("sync %s: conflict, kept local copy as %s", idx.id, cname)
                idx.files.pop(rel, None)
            before = disk_state(target)

        res = await s.channel.request({"t": "sync_get", "folder": idx.id, "path": rel, "hash": r[0]})
        inc = res["_in"]
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = tmp_path_for(target)
        h = hashlib.sha256()
        try:
            with open(tmp, "wb") as f:
                async for c in inc.chunks():
                    await asyncio.to_thread(f.write, c)
                    h.update(c)
            if h.hexdigest() != r[0]:
                raise ProtoError("changed", "hash mismatch")
            mtime_ms = res.get("mtime") if isinstance(res.get("mtime"), int) else r[2]
            async with self.scan_lock:
                if disk_state(target) != before:
                    raise ProtoError("changed", "local file changed during download")
                os.utime(tmp, ns=(mtime_ms * 1_000_000, mtime_ms * 1_000_000))
                os.replace(tmp, target)
                st = os.stat(target)
                idx.files[rel] = {"h": r[0], "s": st.st_size, "m": mtime_ms, "n": st.st_mtime_ns}
                base.set(rel, r[0])
        finally:
            if not inc.done:
                inc.abort()
            if os.path.exists(tmp):
                os.unlink(tmp)
        log.info("sync %s: pulled %s", idx.id, rel)
        return True

    # -- remote commands (the phone asks, we run) --------------------------------

    async def _h_cmd_list(self, s, msg):
        cmds = [{"id": c["id"], "name": c["name"]} for c in self.cfg["commands"]]
        await s.channel.reply(msg, commands=cmds, shell=bool(self.cfg["remote_shell"]))

    async def _h_cmd_run(self, s, msg):
        if isinstance(msg.get("id"), str):
            c = next((c for c in self.cfg["commands"] if c["id"] == msg["id"]), None)
            if c is None:
                raise ProtoError("not_found")
            line = c["command"]
            log.info("%s runs saved command %r", s.name, c["name"])
        elif isinstance(msg.get("shell"), str) and msg["shell"].strip():
            if not self.cfg["remote_shell"]:
                raise ProtoError("denied", "remote shell is off")
            line = msg["shell"]
            log.info("%s runs shell command %r", s.name, line)
            self.notify(f"{s.name} ran a command", line[:200])
        else:
            raise ProtoError("bad_request")
        result = await run_command(line, float(self.cfg["command_timeout"]))
        await s.channel.reply(msg, **result)

    # -- notifications to phones ---------------------------------------------------

    async def send_notification(self, title: str, text: str, device: str | None = None) -> list[str]:
        targets = [s for s in self.connected() if (s.peer_id == device if device else s.type == "phone")]
        if not targets:
            raise ProtoError("not_found", "no phone is connected")
        sent = []
        for s in targets:
            await s.channel.request({"t": "notify", "title": title[:200], "text": text[:4000]})
            sent.append(s.name)
        return sent

    # -- clipboard images ---------------------------------------------------------

    def on_local_clip_image(self, mime: str, data: bytes) -> None:
        if not self.cfg["clipboard"] or not data or len(data) > clipboard.MAX_IMAGE:
            return
        h = hashlib.sha256(data).hexdigest()
        if h == self.last_clip_image:
            return
        self.last_clip_image = h
        for s in self.connected():
            s.channel.spawn(s.channel.request({"t": "clip_image", "mime": mime, "size": len(data)},
                                              upload=bytes_chunks(data)))

    async def _h_clip_image(self, s, msg):
        inc = msg["_in"]
        mime = str(msg.get("mime") or "")
        data = await read_all(inc, clipboard.MAX_IMAGE)
        if not mime.startswith("image/"):
            raise ProtoError("bad_request")
        await s.channel.reply(msg)
        if self.cfg["clipboard"]:
            self.last_clip_image = hashlib.sha256(data).hexdigest()
            await self.clip.set_image(mime, data)

    # -- battery --------------------------------------------------------------------

    async def _h_battery(self, s, msg):
        level, charging = msg.get("level"), bool(msg.get("charging"))
        if not isinstance(level, int):
            raise ProtoError("bad_request")
        s.battery = {"level": max(0, min(100, level)), "charging": charging}
        if level <= 15 and not charging and s.peer_id not in self.low_battery:
            self.low_battery.add(s.peer_id)
            self.notify(f"{s.name} battery low", f"{level}% left")
        elif charging or level > 20:
            self.low_battery.discard(s.peer_id)
        self.emit_status()

    # -- phone notifications on the desktop --------------------------------------------

    def _icon_path(self, app: str, b64: str | None) -> str:
        if not b64:
            return ""
        d = self.paths.runtime_dir / "icons"
        p = d / (hashlib.sha256(app.encode()).hexdigest()[:16] + ".png")
        if not p.exists():
            try:
                d.mkdir(exist_ok=True)
                p.write_bytes(base64.b64decode(b64, validate=True)[:256 * 1024])
            except (ValueError, OSError):
                return ""
        return str(p)

    async def _h_notif_posted(self, s, msg):
        key, app = msg.get("key"), str(msg.get("app") or "")
        if not isinstance(key, str):
            raise ProtoError("bad_request")
        label = str(msg.get("app_name") or app)[:100]
        if app and (msg.get("app_name") or app not in self.notif_apps):
            self.notif_apps[app] = label
        if not self.cfg["notifications"] or app in self.cfg["notif_muted"]:
            return
        title = str(msg.get("title") or label)[:300]
        text = str(msg.get("text") or "")[:2000]
        actions = [("reply", "Reply")] if msg.get("reply") else []
        old = s.notifs.get(key, 0)
        nid = await self.desk.notify(f"{label} · {s.name}", title, text, self._icon_path(app, msg.get("icon")),
                                     actions, replaces=old)
        if nid is None:
            return
        if old and old != nid:
            self.notif_map.pop(old, None)
        s.notifs[key] = nid
        self.notif_map[nid] = {"peer": s.peer_id, "key": key, "title": title, "app": label, "acted": False}

    async def _h_notif_removed(self, s, msg):
        nid = s.notifs.pop(str(msg.get("key")), None)
        if nid is not None:
            self.notif_map.pop(nid, None)
            await self.desk.close(nid)

    def _on_notif_action(self, nid: int, action: str) -> None:
        n = self.notif_map.get(nid)
        if n is None or action != "reply":
            return
        n["acted"] = True
        try:
            import subprocess

            subprocess.Popen([sys.executable, "-m", "tether", "reply", "--device", n["peer"], "--key", n["key"],
                              "--title", f"{n['app']}: {n['title']}"], start_new_session=True)
        except OSError as e:
            log.warning("could not open the reply window: %s", e)

    def _on_notif_closed(self, nid: int, reason: int) -> None:
        n = self.notif_map.pop(nid, None)
        if n is None:
            return
        s = self.sessions.get(n["peer"])
        if s is not None:
            s.notifs.pop(n["key"], None)
            if reason == REASON_DISMISSED and not n["acted"] and not s.channel.closed.is_set():
                s.channel.spawn(s.channel.send({"t": "notif_dismiss", "key": n["key"]}))

    async def notif_reply(self, peer_id: str, key: str, text: str) -> None:
        s = self.sessions.get(peer_id)
        if s is None or s.channel.closed.is_set():
            raise ProtoError("not_found", "phone not connected")
        await s.channel.request({"t": "notif_reply", "key": key, "text": text})

    # -- wake-on-LAN ------------------------------------------------------------------

    async def _send_host_info(self, s: Session) -> None:
        """Tells the phone how to wake this computer later."""
        from . import wol

        try:
            ifs = await asyncio.to_thread(wol.interfaces, False)
        except OSError:
            ifs = []
        targets = [{k: i[k] for k in ("mac", "broadcast", "ip", "ifname", "wired")} for i in ifs]
        await s.channel.send({"t": "host_info", "wol": targets})

    # -- media (the phone controls players here) ---------------------------------------

    async def _media_state(self) -> dict:
        """What phones are told: the players here, or nothing while media control is off."""
        if not self.cfg["media"]:
            return {"players": [], "volume": None, "available": self.media.available, "disabled": True}
        return await self.media.state()

    async def media_setting_changed(self) -> None:
        """Tells connected phones at once, so their controls and playback notification follow."""
        state = await self._media_state()
        for s in self.connected():
            if s.type == "phone":
                s.channel.spawn(s.channel.send({"t": "media_update", **state}))

    def _on_media_change(self, state: dict) -> None:
        if not self.cfg["media"]:
            return
        for s in self.connected():
            if s.type == "phone":
                s.channel.spawn(s.channel.send({"t": "media_update", **state}))

    async def _media_poll(self) -> None:
        while True:
            await asyncio.sleep(5)
            if self.cfg["media"] and self.media.available and any(s.type == "phone" for s in self.connected()):
                await self.media.refresh()

    async def _h_media_state(self, s, msg):
        await s.channel.reply(msg, **await self._media_state())

    async def _h_media_cmd(self, s, msg):
        if not self.cfg["media"]:
            raise ProtoError("denied", "media control is off")
        player = msg.get("player") if isinstance(msg.get("player"), str) else None
        try:
            state = await self.media.command(player, str(msg.get("action")), msg.get("value"))
        except (ValueError, TypeError) as e:
            raise ProtoError("unsupported", str(e)) from None
        await s.channel.reply(msg, **state)

    # -- events / notifications -------------------------------------------------

    def emit(self, ev: dict) -> None:
        for c in list(self.subscribers):
            c.push_nowait(ev)

    def emit_status(self) -> None:
        self.emit({"ev": "status", **self.status()})

    def notify(self, title: str, body: str) -> None:
        if any(c.is_extension for c in self.subscribers):
            self.emit({"ev": "notify", "title": title, "body": body})
        elif shutil.which("notify-send"):
            self.spawn(asyncio.create_subprocess_exec("notify-send", "-a", "Tether", title, body))

    def status(self) -> dict:
        devices = []
        for pid, p in self.peers.peers.items():
            s = self.sessions.get(pid)
            devices.append({"id": pid, "name": p["name"], "type": p.get("type"), "paired": True,
                            "connected": self.is_connected(pid),
                            "battery": s.battery if s and self.is_connected(pid) else None})
        pending = [
            {"id": s.peer_id, "name": s.name, "sas": s.hs.sas, "initiator": s.pair["initiator"],
             "confirmed": bool(s.pair["local"])}
            for s in list(self.untrusted) + list(self.sessions.values()) if s.pair is not None
        ]
        discovered = [
            {"id": pid, **{k: v for k, v in d.items() if k != "at"}}
            for pid, d in self.discovery.recent().items() if pid not in self.peers
        ]
        return {
            "id": self.identity.id,
            "name": self.cfg["name"],
            "devices": devices,
            "discovered": discovered,
            "pending": pending,
            "clipboard": self.cfg["clipboard"],
            "clipboard_backend": self.clip.name,
            "dav_url": f"dav://127.0.0.1:{self.cfg['dav_port']}/{self.cfg['dav_token']}/Phone/",
            "dav_root": f"dav://127.0.0.1:{self.cfg['dav_port']}/{self.cfg['dav_token']}/",
            "shares": self.cfg["shares"],
            "sync": self.cfg["sync"],
            "download_dir": str(self.cfg.download_dir),
            "commands": self.cfg["commands"],
            "remote_shell": self.cfg["remote_shell"],
            "command_timeout": self.cfg["command_timeout"],
            "transfers": self.transfers.active(),
            "notifications": self.cfg["notifications"],
            "notif_muted": self.cfg["notif_muted"],
            "notif_apps": [{"app": a, "name": n} for a, n in sorted(self.notif_apps.items(), key=lambda x: x[1].lower())],
            "media_available": self.media.available,
            "media": self.cfg["media"],
        }


def sync_conflict_name(root, rel: str) -> str:
    from .sync import conflict_name

    name = conflict_name(rel)
    i = 1
    while os.path.lexists(os.path.join(root, name)):
        stem, ext = os.path.splitext(conflict_name(rel))
        name = f"{stem}-{i}{ext}"
        i += 1
    return name
