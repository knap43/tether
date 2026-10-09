"""Local control socket: newline-delimited JSON for the CLI and the GNOME extension."""

import asyncio
import base64
import json
import logging
import os
import re

from .errors import ProtoError

log = logging.getLogger("tether.control")


class Client:
    def __init__(self, server: "ControlServer", reader, writer):
        self.server, self.reader, self.writer = server, reader, writer
        self.is_extension = False
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=256)

    def push_nowait(self, ev: dict) -> None:
        try:
            self.queue.put_nowait(ev)
        except asyncio.QueueFull:
            pass

    async def push(self, ev: dict) -> None:
        self.push_nowait(ev)

    async def write(self, obj: dict) -> None:
        self.writer.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
        await self.writer.drain()

    async def pump(self) -> None:
        while True:
            await self.write(await self.queue.get())


class ControlServer:
    def __init__(self, node):
        self.node = node

    async def start(self) -> None:
        path = str(self.node.paths.socket)
        if os.path.exists(path):
            os.unlink(path)
        self.server = await asyncio.start_unix_server(self._client, path=path, limit=32 * 1024 * 1024)
        os.chmod(path, 0o600)

    async def _client(self, reader, writer) -> None:
        c = Client(self, reader, writer)
        pump = None
        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line)
                    cmd = req.get("cmd")
                    if cmd == "subscribe":
                        c.is_extension = bool(req.get("clipboard"))
                        self.node.subscribers.add(c)
                        if c.is_extension and hasattr(self.node.clip, "clients"):
                            self.node.clip.clients.add(c)
                        pump = pump or asyncio.ensure_future(c.pump())
                        c.push_nowait({"ev": "status", **self.node.status()})
                        continue
                    if cmd == "clip_local":
                        self.node.on_local_clip(req.get("text"))
                        continue
                    if cmd == "clip_local_image":
                        try:
                            data = base64.b64decode(req.get("data") or "", validate=True)
                        except ValueError:
                            continue
                        self.node.on_local_clip_image(str(req.get("mime") or "image/png"), data)
                        continue
                    result = await self.dispatch(cmd, req)
                    out = {"ok": True, **(result or {})}
                except ProtoError as e:
                    out = {"ok": False, "error": str(e)}
                except (ValueError, TypeError, KeyError) as e:
                    out = {"ok": False, "error": f"bad request: {e}"}
                except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                    out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                except Exception as e:  # never drop the client silently
                    log.exception("control command %r failed", req.get("cmd") if isinstance(req, dict) else req)
                    out = {"ok": False, "error": f"internal error: {e}"}
                if "id" in req:
                    out["rid"] = req["id"]
                if pump:
                    c.push_nowait(out)
                else:
                    await c.write(out)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.node.subscribers.discard(c)
            if hasattr(self.node.clip, "clients"):
                self.node.clip.clients.discard(c)
            if pump:
                pump.cancel()
            writer.close()

    async def dispatch(self, cmd, req) -> dict | None:
        n = self.node
        if cmd == "status":
            return n.status()
        if cmd == "pair":
            s = await n.start_pairing(req.get("device"), req.get("addr"))
            return {"device": s.peer_id, "name": s.name, "sas": s.hs.sas}
        if cmd == "pair_confirm":
            await n.confirm_pairing(n_resolve(n, req["device"], pending=True), bool(req.get("accept")))
            return None
        if cmd == "unpair":
            await n.unpair(n_resolve(n, req["device"]))
            return None
        if cmd == "send":
            target = n_resolve(n, req.get("device"), connected=True)
            sent = []
            for p in req["paths"]:
                sent.append(await n.send_file(target, p))
            return {"sent": sent}
        if cmd == "clip_send":
            text = req.get("text")
            if not isinstance(text, str):
                raise ProtoError("bad_request")
            n.last_clip = None
            n.on_local_clip(text)
            return None
        if cmd == "set":
            key, value = req["key"], req["value"]
            if key not in ("name", "clipboard", "download_dir", "scan_interval", "remote_shell", "command_timeout",
                           "notifications", "media"):
                raise ProtoError("bad_request", f"cannot set {key}")
            n.cfg[key] = value
            if key == "media":
                await n.media_setting_changed()
            return None
        if cmd == "share_add":
            shares = dict(n.cfg["shares"])
            path = os.path.expanduser(req["path"])
            if "/" in req["name"] or not os.path.isdir(path):
                raise ProtoError("bad_request", "name must not contain / and path must be a directory")
            shares[req["name"]] = req["path"]
            n.cfg["shares"] = shares
            n.shares.__init__(shares)
            return None
        if cmd == "share_remove":
            shares = dict(n.cfg["shares"])
            if shares.pop(req["name"], None) is None:
                raise ProtoError("not_found")
            n.cfg["shares"] = shares
            n.shares.__init__(shares)
            return None
        if cmd == "sync_add":
            fid, path = req["folder"], os.path.expanduser(req["path"])
            if not fid.replace("-", "").replace("_", "").isalnum():
                raise ProtoError("bad_request", "folder id may contain letters, digits, - and _")
            os.makedirs(path, exist_ok=True)
            folders = [f for f in n.cfg["sync"] if f["id"] != fid] + [{"id": fid, "path": req["path"]}]
            n.cfg["sync"] = folders
            n.load_sync_folders()
            asyncio.ensure_future(n.rescan())
            return None
        if cmd == "sync_remove":
            folders = [f for f in n.cfg["sync"] if f["id"] != req["folder"]]
            if len(folders) == len(n.cfg["sync"]):
                raise ProtoError("not_found")
            n.cfg["sync"] = folders
            n.load_sync_folders()
            return None
        if cmd == "notify":
            device = n_resolve(n, req["device"], connected=True) if req.get("device") else None
            return {"sent": await n.send_notification(str(req["title"]), str(req.get("text") or ""), device)}
        if cmd == "command_add":
            name, line = str(req["name"]).strip(), str(req["command"]).strip()
            if not name or not line:
                raise ProtoError("bad_request", "name and command are required")
            base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "command"
            ids = {c["id"] for c in n.cfg["commands"] if c["name"] != name}
            cid, i = base, 2
            while cid in ids:
                cid, i = f"{base}-{i}", i + 1
            cmds = [c for c in n.cfg["commands"] if c["name"] != name] + [{"id": cid, "name": name, "command": line}]
            n.cfg["commands"] = cmds
            return None
        if cmd == "command_remove":
            ref = str(req["name"])
            cmds = [c for c in n.cfg["commands"] if ref not in (c["name"], c["id"])]
            if len(cmds) == len(n.cfg["commands"]):
                raise ProtoError("not_found")
            n.cfg["commands"] = cmds
            return None
        if cmd == "notif_reply":
            await n.notif_reply(n_resolve(n, req["device"], connected=True), str(req["key"]), str(req["text"]))
            return None
        if cmd == "notif_mute":
            app = str(req["app"])
            muted = [a for a in n.cfg["notif_muted"] if a != app] + ([app] if req.get("muted") else [])
            n.cfg["notif_muted"] = muted
            return None
        if cmd == "transfer_cancel":
            if not n.transfers.cancel(int(req["id"])):
                raise ProtoError("not_found", "no such transfer")
            return None
        if cmd == "media_state":
            return await n.media.state()
        if cmd == "debug_send" and os.environ.get("TETHER_DEBUG"):
            target = n.sessions.get(n_resolve(n, req["device"], connected=True))
            await target.channel.send(req["msg"])
            return None
        if cmd == "wol_status":
            from . import wol

            return {"interfaces": await asyncio.to_thread(wol.interfaces, True)}
        if cmd == "wol_enable":
            from . import wol

            try:
                conn = await asyncio.to_thread(wol.enable, str(req["ifname"]))
            except RuntimeError as e:
                raise ProtoError("io", str(e)) from None
            return {"connection": conn}
        if cmd == "rescan":
            await n.rescan()
            return None
        raise ProtoError("bad_request", f"unknown command {cmd!r}")


def n_resolve(node, ref, connected=False, pending=False) -> str:
    """Resolve a device reference (id, id prefix, or name) to a device id."""
    if pending:
        pool = {d["id"]: d["name"] for d in node.status()["pending"]}
    elif connected:
        pool = {s.peer_id: s.name for s in node.connected()}
    else:
        pool = {pid: p["name"] for pid, p in node.peers.peers.items()}
    if not ref:
        if len(pool) == 1:
            return next(iter(pool))
        if pool:
            raise ProtoError("bad_request", "several devices match; name one")
        raise ProtoError("not_found", "no device is connected" if connected else "no matching device")
    ref = str(ref)
    hits = [pid for pid, name in pool.items() if pid.startswith(ref.lower()) or name.lower() == ref.lower()]
    if len(hits) != 1:
        raise ProtoError("not_found" if not hits else "bad_request", f"device {ref!r} is ambiguous or unknown")
    return hits[0]
