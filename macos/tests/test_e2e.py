"""End-to-end test: two daemons on localhost (A plays the Mac, B the phone).

Run:  python3 tests/test_e2e.py
"""

import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from tether.config import Paths  # noqa: E402
from tether.errors import ProtoError  # noqa: E402
from tether.node import Node  # noqa: E402


class FakeClip:
    name = "fake"

    def __init__(self):
        self.value = None

    async def start(self):
        pass

    async def set(self, text):
        self.value = text

    async def set_image(self, mime, data):
        self.image = (mime, data)


def make_node(root: Path, name: str, port: int, extra: dict) -> Node:
    paths = Paths(str(root / name))
    cfg = {"name": name, "port": port, "dav_port": port + 1000, "discovery_port": port + 2000,
           "scan_interval": 1, "download_dir": str(root / name / "downloads"), **extra}
    paths.config_file.write_text(json.dumps(cfg))
    n = Node(paths)
    n.clip = FakeClip()
    return n


async def until(pred, timeout=10, what="condition"):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


async def pair(a: Node, b: Node, b_port: int):
    s = await a.start_pairing(addr=f"127.0.0.1:{b_port}")
    await until(lambda: b._find_pairing(a.identity.id) is not None, what="pair request at B")
    sb = b._find_pairing(a.identity.id)
    assert sb.hs.sas == s.hs.sas, "SAS mismatch"
    await a.confirm_pairing(b.identity.id, True)
    await b.confirm_pairing(a.identity.id, True)
    await until(lambda: a.is_connected(b.identity.id) and b.is_connected(a.identity.id), what="trusted session")
    assert b.identity.id in a.peers and a.identity.id in b.peers


async def main(root: Path):
    a_share = root / "a_share"
    b_share = root / "b_share"
    for d in (a_share, b_share):
        (d / "sub").mkdir(parents=True)
    (b_share / "hello.txt").write_text("hello from phone")
    (b_share / "big.bin").write_bytes(os.urandom(300_000))
    a_sync, b_sync = root / "a_sync", root / "b_sync"
    a_sync.mkdir()
    b_sync.mkdir()
    (a_sync / "only_a.txt").write_text("A")
    (b_sync / "nested").mkdir()
    (b_sync / "nested" / "only_b.txt").write_text("B")

    A = make_node(root, "A", 48291, {"shares": {"Home": str(a_share)}, "sync": [{"id": "docs", "path": str(a_sync)}]})
    B = make_node(root, "B", 48391, {"shares": {"Internal": str(b_share)}, "sync": [{"id": "docs", "path": str(b_sync)}]})
    await A.start(with_dav=True, with_discovery=False)
    await B.start(with_dav=False, with_discovery=False)

    # --- untrusted sessions are refused service
    s = await A.connect("127.0.0.1", 48391)
    try:
        await s.channel.request({"t": "fs_list", "path": "/"}, timeout=3)
        raise AssertionError("unpaired request should not be answered")
    except (ConnectionError, asyncio.TimeoutError, ProtoError):
        pass
    await until(lambda: s.channel.closed.is_set(), what="untrusted close")
    print("ok  unpaired peer refused")

    # --- pairing
    await pair(A, B, 48391)
    print("ok  pairing")
    sa = A.sessions[B.identity.id]

    # --- clipboard both ways
    A.on_local_clip("from desktop ✓")
    await until(lambda: B.clip.value == "from desktop ✓", what="clip A→B")
    B.on_local_clip("from phone")
    await until(lambda: A.clip.value == "from phone", what="clip B→A")
    print("ok  clipboard")

    # --- quick send
    f = root / "photo.jpg"
    f.write_bytes(os.urandom(200_000))
    res = await A.send_file(B.identity.id, str(f))
    got = root / "B" / "downloads" / res["saved_as"]
    assert got.read_bytes() == f.read_bytes()
    res2 = await A.send_file(B.identity.id, str(f))
    assert res2["saved_as"] == "photo (1).jpg", res2
    print("ok  quick send")

    # --- browsing over the protocol
    ch = sa.channel
    roots = (await ch.request({"t": "fs_list", "path": "/"}))["entries"]
    assert [r["name"] for r in roots] == ["Internal"]
    names = sorted(e["name"] for e in (await ch.request({"t": "fs_list", "path": "/Internal"}))["entries"])
    assert names == ["big.bin", "hello.txt", "sub"], names
    for bad in ("/Internal/../../etc", "/Nope/x"):
        try:
            await ch.request({"t": "fs_list", "path": bad})
            raise AssertionError("escape allowed")
        except ProtoError:
            pass
    os.symlink("/etc", b_share / "escape")
    try:
        await ch.request({"t": "fs_list", "path": "/Internal/escape"})
        raise AssertionError("symlink escape allowed")
    except ProtoError as e:
        assert e.code == "denied"
    print("ok  browse + confinement")

    # --- WebDAV bridge (A exposes B)
    base = f"http://127.0.0.1:{48291 + 1000}/{A.cfg['dav_token']}"
    dev = "B"
    async with aiohttp.ClientSession() as http:
        r = await http.request("PROPFIND", base + "/", headers={"Depth": "1"})
        body = await r.text()
        assert r.status == 207 and f"/{dev}/" in body, body
        r = await http.request("PROPFIND", f"{base}/{dev}/Internal/", headers={"Depth": "1"})
        body = await r.text()
        assert r.status == 207 and "hello.txt" in body and "<D:collection/>" in body
        r = await http.get(f"{base}/{dev}/Internal/hello.txt")
        assert await r.text() == "hello from phone"
        r = await http.get(f"{base}/{dev}/Internal/big.bin", headers={"Range": "bytes=1000-1999"})
        data = await r.read()
        assert r.status == 206 and data == (b_share / "big.bin").read_bytes()[1000:2000]
        r = await http.get(f"{base}/{dev}/Internal/big.bin")
        assert await r.read() == (b_share / "big.bin").read_bytes()
        r = await http.put(f"{base}/{dev}/Internal/sub/new%20file.txt", data=b"x" * 150_000)
        assert r.status == 201, r.status
        assert (b_share / "sub" / "new file.txt").read_bytes() == b"x" * 150_000
        r = await http.request("MKCOL", f"{base}/{dev}/Internal/made")
        assert r.status == 201 and (b_share / "made").is_dir()
        r = await http.request("MOVE", f"{base}/{dev}/Internal/sub/new%20file.txt",
                               headers={"Destination": f"{base}/{dev}/Internal/made/moved.txt"})
        assert r.status == 201 and (b_share / "made" / "moved.txt").exists()
        r = await http.request("COPY", f"{base}/{dev}/Internal/made/moved.txt",
                               headers={"Destination": f"{base}/{dev}/Internal/copy.txt"})
        assert r.status == 201 and (b_share / "copy.txt").read_bytes() == b"x" * 150_000
        r = await http.delete(f"{base}/{dev}/Internal/made")
        assert r.status == 204 and not (b_share / "made").exists()
        # the Phone shortcut opens a lone share directly
        r = await http.request("PROPFIND", f"{base}/Phone/", headers={"Depth": "1"})
        body = await r.text()
        assert r.status == 207 and f"/{A.cfg['dav_token']}/Phone/hello.txt<" in body and "Internal" not in body, body
        assert f"/{A.cfg['dav_token']}/Phone/<" in body, body
        r = await http.get(f"{base}/Phone/hello.txt")
        assert await r.text() == "hello from phone"
        r = await http.put(f"{base}/Phone/via%20short.txt", data=b"short")
        assert r.status == 201 and (b_share / "via short.txt").read_bytes() == b"short"
        r = await http.request("MOVE", f"{base}/Phone/via%20short.txt",
                               headers={"Destination": f"{base}/{dev}/Internal/copy2.txt"})
        assert r.status == 201 and (b_share / "copy2.txt").exists() and not (b_share / "via short.txt").exists()
        r = await http.request("MOVE", f"{base}/{dev}/Internal/copy2.txt",
                               headers={"Destination": f"{base}/Phone/sub/back.txt"})
        assert r.status == 201 and (b_share / "sub" / "back.txt").exists()
        r = await http.request("PROPFIND", f"{base}/Phone/sub", headers={"Depth": "1"})
        assert "/Phone/sub/back.txt<" in await r.text()
        r = await http.get(f"{base}/{dev}/Internal/missing")
        assert r.status == 404
        r = await http.get(f"http://127.0.0.1:{48291 + 1000}/wrongtoken/{dev}/")
        assert r.status == 404
        # Finder: locking (or it mounts read-only), and its metadata files kept off the phone.
        r = await http.request("OPTIONS", f"{base}/Phone/")
        assert "2" in r.headers["DAV"] and "LOCK" in r.headers["Allow"], dict(r.headers)
        r = await http.request("LOCK", f"{base}/Phone/hello.txt", headers={"Depth": "0", "Timeout": "Second-600"},
                               data=b'<?xml version="1.0"?><D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/>'
                                    b'</D:lockscope><D:locktype><D:write/></D:locktype><D:owner>Finder</D:owner>'
                                    b'</D:lockinfo>')
        body = await r.text()
        token = r.headers.get("Lock-Token", "")
        assert r.status == 200 and token.startswith("<opaquelocktoken:") and "lockdiscovery" in body, body
        r = await http.request("LOCK", f"{base}/Phone/hello.txt", headers={"If": f"({token})"})
        assert r.headers.get("Lock-Token") == token, "lock refresh changed the token"
        r = await http.request("UNLOCK", f"{base}/Phone/hello.txt", headers={"Lock-Token": token})
        assert r.status == 204
        for junk in ("._hello.txt", ".DS_Store"):
            r = await http.put(f"{base}/Phone/{junk}", data=b"\x00\x05\x16\x07finder")
            assert r.status == 201, (junk, r.status)
            assert not (b_share / junk).exists(), f"{junk} reached the phone"
            r = await http.request("PROPFIND", f"{base}/Phone/{junk}", headers={"Depth": "0"})
            assert r.status == 404, (junk, r.status)
        r = await http.put(f"{base}/Phone/sub/finder.txt", data=b"written from Finder")
        assert r.status in (201, 204) and (b_share / "sub" / "finder.txt").read_bytes() == b"written from Finder"
        # abandon a download midway; the session must stay usable
        r = await http.get(f"{base}/{dev}/Internal/big.bin")
        await r.content.read(1000)
        r.close()
    await asyncio.sleep(0.3)
    assert (await ch.request({"t": "fs_stat", "path": "/Internal/hello.txt"}))["entry"]["size"] == 16
    print("ok  WebDAV bridge (with Finder locking and metadata)")

    # --- folder sync: initial merge
    await until(lambda: (a_sync / "nested" / "only_b.txt").exists() and (b_sync / "only_a.txt").exists(),
                what="initial sync")
    # modification
    await asyncio.sleep(1.1)
    (a_sync / "only_a.txt").write_text("A edited")
    await until(lambda: (b_sync / "only_a.txt").read_text() == "A edited", what="edit propagation")
    # deletion
    (b_sync / "nested" / "only_b.txt").unlink()
    await until(lambda: not (a_sync / "nested").exists(), what="delete propagation")
    # conflict: both edit the same file
    (a_sync / "c.txt").write_text("base")
    await until(lambda: (b_sync / "c.txt").exists(), what="c.txt sync")
    await asyncio.sleep(1.5)
    async with A.scan_lock, B.scan_lock:
        (a_sync / "c.txt").write_text("A version")
        os.utime(a_sync / "c.txt", (time.time() - 50, time.time() - 50))
        (b_sync / "c.txt").write_text("B version, newer")

    def converged():
        fa = sorted(p.name for p in a_sync.iterdir() if p.is_file())
        fb = sorted(p.name for p in b_sync.iterdir() if p.is_file())
        return fa == fb and any("conflict" in n for n in fa)

    try:
        await until(converged, timeout=15, what="conflict resolution")
    except AssertionError:
        print("A:", sorted(p.name for p in a_sync.iterdir()), "B:", sorted(p.name for p in b_sync.iterdir()))
        print("A idx", A.indexes["docs"].files.get("c.txt"), "B idx", B.indexes["docs"].files.get("c.txt"))
        raise
    assert (a_sync / "c.txt").read_text() == "B version, newer"
    cf = next(p for p in a_sync.iterdir() if "conflict" in p.name)
    assert cf.read_text() == "A version" and (b_sync / cf.name).read_text() == "A version"
    print("ok  sync (merge, edit, delete, conflict)")

    # --- remote commands: B (phone role) runs things on A
    sb = B.sessions[A.identity.id].channel
    A.cfg["commands"] = [{"id": "hi", "name": "Hi", "command": "echo hi; echo oops >&2; exit 3"}]
    lst = await sb.request({"t": "cmd_list"})
    assert lst["commands"] == [{"id": "hi", "name": "Hi"}] and lst["shell"] is False
    r = await sb.request({"t": "cmd_run", "id": "hi"})
    assert r["exit"] == 3 and "hi" in r["output"] and "oops" in r["output"], r
    try:
        await sb.request({"t": "cmd_run", "shell": "true"})
        raise AssertionError("shell ran while disabled")
    except ProtoError as e:
        assert e.code == "denied"
    A.cfg["remote_shell"] = True
    r = await sb.request({"t": "cmd_run", "shell": "echo $((6*7))"})
    assert r["exit"] == 0 and r["output"].strip() == "42"
    t0 = time.monotonic()
    r = await sb.request({"t": "cmd_run", "shell": "(sleep 5 &); echo bg"})
    assert r["output"].strip() == "bg" and time.monotonic() - t0 < 3, "background child blocked the reply"
    A.cfg["command_timeout"] = 1
    r = await sb.request({"t": "cmd_run", "shell": "sleep 10; echo never"})
    assert r["timed_out"] and "never" not in r["output"]
    r = await sb.request({"t": "cmd_run", "shell": "head -c 200000 /dev/zero | tr '\\0' x"})
    assert r["truncated"] and len(r["output"]) == 64 * 1024
    A.cfg["remote_shell"] = False
    print("ok  remote commands")

    # --- battery, phone notifications, clipboard images, media, transfer progress
    events = []
    cancel_names = {"cancel-out.bin", "cancel-in.bin"}

    class Sub:
        is_app = False

        def push_nowait(self, ev):
            events.append(ev)
            if ev.get("ev") == "transfer" and ev["state"] == "active" and ev["name"] in cancel_names:
                A.transfers.cancel(ev["id"])

    A.subscribers.add(Sub())
    sb = B.sessions[A.identity.id].channel
    B.cfg["device_type"] = "phone"
    A.sessions[B.identity.id].type = "phone"

    await sb.send({"t": "battery", "level": 12, "charging": False})
    await until(lambda: A.status()["devices"][0]["battery"] == {"level": 12, "charging": False}, what="battery")
    assert any(e.get("ev") == "status" for e in events)

    shown, closed, b_got = [], [], []

    class FakeDesk:
        available = True

        async def notify(self, app, title, body, icon="", actions=(), replaces=0):
            shown.append({"app": app, "title": title, "body": body, "icon": icon, "actions": list(actions)})
            return 100 + len(shown)

        async def close(self, nid):
            closed.append(nid)

    A.desk = FakeDesk()

    async def record(s_, msg):
        b_got.append(msg)
        if "req" in msg:
            await s_.channel.reply(msg)

    B._h_notif_dismiss = record
    B._h_notif_reply = record
    B._h_media_update = record
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    await sb.send({"t": "notif_posted", "key": "k1", "app": "org.signal", "app_name": "Signal",
                   "title": "Ana", "text": "see you at 8", "reply": True, "icon": png})
    await until(lambda: shown, what="desktop notification")
    n = shown[0]
    assert n["title"] == "Ana" and n["actions"] == [("reply", "Reply")] and n["icon"].endswith(".png"), n
    nid = 101
    await A.notif_reply(B.identity.id, "k1", "on my way")
    assert b_got[-1]["t"] == "notif_reply" and b_got[-1]["text"] == "on my way"
    A._on_notif_closed(nid, 2)  # user dismissed it on the desktop
    await until(lambda: any(m["t"] == "notif_dismiss" and m["key"] == "k1" for m in b_got), what="dismiss to phone")
    await sb.send({"t": "notif_posted", "key": "k2", "app": "org.signal", "app_name": "Signal", "title": "B"})
    await until(lambda: len(shown) == 2, what="second notification")
    await sb.send({"t": "notif_removed", "key": "k2"})
    await until(lambda: closed == [102], what="close on phone removal")
    A.cfg["notif_muted"] = ["org.signal"]
    await sb.send({"t": "notif_posted", "key": "k3", "app": "org.signal", "title": "muted"})
    await asyncio.sleep(0.3)
    assert len(shown) == 2 and A.status()["notif_apps"] == [{"app": "org.signal", "name": "Signal"}]
    # A reply typed into the notification itself; Notification Center then removes it (not a dismissal).
    A.cfg["notif_muted"] = []
    await sb.send({"t": "notif_posted", "key": "k4", "app": "org.signal", "title": "Ana", "text": "?", "reply": True})
    await until(lambda: len(shown) == 3, what="third notification")
    b_got.clear()
    A._on_notif_action(103, "reply", "ten minutes")
    await until(lambda: b_got, what="inline reply")
    assert b_got[-1]["t"] == "notif_reply" and b_got[-1]["key"] == "k4" and b_got[-1]["text"] == "ten minutes"
    A._on_notif_closed(103, 3)
    await asyncio.sleep(0.2)
    assert len(b_got) == 1 and 103 not in A.notif_map, b_got
    print("ok  battery + phone notifications (show, reply, dismiss, remove, mute)")

    img_a, img_b = b"\x89PNG" + os.urandom(300_000), b"\x89PNG" + os.urandom(1000)
    A.on_local_clip_image("image/png", img_a)
    await until(lambda: getattr(B.clip, "image", None) == ("image/png", img_a), what="image A→B")
    B.on_local_clip_image("image/png", img_b)
    await until(lambda: getattr(A.clip, "image", None) == ("image/png", img_b), what="image B→A")
    print("ok  clipboard images")

    # No media control on macOS: the phone is told so, and commands are refused.
    r = await sb.request({"t": "media_state"})
    assert r["players"] == [] and not r["available"], r
    try:
        await sb.request({"t": "media_cmd", "action": "next"})
        raise AssertionError("media command accepted")
    except ProtoError as e:
        assert e.code in ("denied", "unsupported"), e
    print("ok  media reported unavailable")

    big = root / "big.bin"
    big.write_bytes(os.urandom(3_000_000))
    events.clear()
    await A.send_file(B.identity.id, str(big))
    tr = [e for e in events if e.get("ev") == "transfer"]
    assert tr[0]["state"] == "active" and tr[-1]["state"] == "done" and tr[-1]["done"] == 3_000_000, tr[-1]
    for name, sender, receiver in (("cancel-out.bin", A, B), ("cancel-in.bin", B, A)):
        f = root / name
        f.write_bytes(os.urandom(5_000_000))
        try:
            await sender.send_file(receiver.identity.id, str(f))
            raise AssertionError("cancelled transfer completed")
        except ProtoError as e:
            assert e.code == "cancelled", e
        assert not (root / receiver.cfg["name"] / "downloads" / name).exists()
        assert any(e.get("name") == name and e["state"] == "cancelled" for e in events if e.get("ev") == "transfer")
    assert A.transfers.active() == []
    assert (await sb.request({"t": "fs_stat", "path": "/Home"}))["entry"]["dir"]
    print("ok  transfer progress + cancel (both directions)")
    A.subscribers.clear()

    # --- reconnect after drop
    sa.channel.close()
    await until(lambda: not A.is_connected(B.identity.id), what="drop")
    await A._try_connect(B.identity.id, "127.0.0.1", 48391)
    await until(lambda: A.is_connected(B.identity.id), what="reconnect")
    A.on_local_clip("after reconnect")
    await until(lambda: B.clip.value == "after reconnect", what="clip after reconnect")
    print("ok  reconnect")

    # --- unpair propagates
    await B.unpair(A.identity.id)
    await until(lambda: B.identity.id not in A.peers, what="unpair propagation")
    print("ok  unpair")
    print("ALL PASSED")


if __name__ == "__main__":
    root = Path(tempfile.mkdtemp(prefix="tether-test-"))
    try:
        asyncio.run(asyncio.wait_for(main(root), 90))
    finally:
        shutil.rmtree(root, ignore_errors=True)
