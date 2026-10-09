"""Interop test: the Linux daemon (Python) against the Android app's Kotlin core.

Usage: python3 test_interop.py <classpath for the Kotlin harness>
"""

import asyncio
import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "linux"))
sys.path.insert(0, str(ROOT / "linux" / "tests"))

import aiohttp  # noqa: E402

from test_e2e import make_node, until  # noqa: E402


class Kt:
    def __init__(self, proc):
        self.proc = proc
        self.lines: list[str] = []
        self.cond = asyncio.Condition()
        asyncio.ensure_future(self._pump())

    async def _pump(self):
        while line := await self.proc.stdout.readline():
            async with self.cond:
                self.lines.append(line.decode().rstrip("\n"))
                self.cond.notify_all()

    async def expect(self, prefix: str, timeout=20) -> str:
        async def find():
            async with self.cond:
                while True:
                    for i, l in enumerate(self.lines):
                        if l.startswith(prefix) or l.startswith("ERR"):
                            del self.lines[i]
                            if l.startswith("ERR") and not prefix.startswith("ERR"):
                                raise AssertionError(f"kotlin side: {l}")
                            return l
                    await self.cond.wait()

        return await asyncio.wait_for(find(), timeout)

    async def cmd(self, line: str, expect: str = "OK", timeout=20) -> str:
        self.proc.stdin.write(line.encode() + b"\n")
        await self.proc.stdin.drain()
        return await self.expect(expect, timeout)


async def main(root: Path, cp: str):
    a_share, k_share = root / "a_share", root / "k_share"
    a_sync, k_sync = root / "a_sync", root / "k_sync"
    for d in (a_share, k_share, a_sync, k_sync):
        d.mkdir()
    (a_share / "x.bin").write_bytes(os.urandom(250_000))
    (k_share / "photo.jpg").write_bytes(os.urandom(180_000))
    (k_share / "Docs").mkdir()
    os.symlink("/etc", k_share / "escape")
    (a_sync / "from_linux.txt").write_text("L")
    (k_sync / "deep" / "er").mkdir(parents=True)
    (k_sync / "deep" / "er" / "from_phone.txt").write_text("P")

    A = make_node(root, "A", 49291, {"shares": {"Home": str(a_share)}, "sync": [{"id": "docs", "path": str(a_sync)}]})
    await A.start(with_dav=True, with_discovery=False)

    (root / "K").mkdir(exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "java", "-cp", cp, "HarnessKt", str(root / "K"), "49391", str(k_share), str(k_sync),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=open(root / "kt.log", "wb"),
    )
    k = Kt(proc)
    try:
        kid = (await k.expect("READY", 60)).split()[1]
        aid = A.identity.id

        # --- pairing, initiated by the phone
        sas_line = await k.cmd("pair 127.0.0.1:49291", "SAS")
        _, peer, k_sas = sas_line.split()
        assert peer == aid
        await until(lambda: A._find_pairing(kid) is not None, what="pair request on Linux")
        assert A._find_pairing(kid).hs.sas == k_sas, "SAS differs between implementations"
        await A.confirm_pairing(kid, True)
        await k.cmd(f"confirm {aid}")
        await k.expect("PAIRED")
        await until(lambda: A.is_connected(kid), what="trusted session")
        print("ok  pairing (Kotlin ⇄ Python), SAS", k_sas)

        # --- clipboard
        A.on_local_clip("héllo from linux\nline 2")
        assert (await k.expect("CLIP")) == "CLIP héllo from linux\\nline 2"
        await k.cmd("clip from-kotlin-✓")
        await until(lambda: A.clip.value == "from-kotlin-✓", what="clip K→A")
        print("ok  clipboard both ways")

        # --- quick send
        f = root / "song.flac"
        f.write_bytes(os.urandom(333_333))
        await A.send_file(kid, str(f))
        got = Path((await k.expect("FILE")).split(" ", 1)[1])
        assert got.read_bytes() == f.read_bytes()
        line = await k.cmd(f"send {aid} {k_share / 'photo.jpg'}", "SENT")
        assert (root / "A" / "downloads" / line.split(" ", 1)[1]).read_bytes() == (k_share / "photo.jpg").read_bytes()
        print("ok  quick send both ways")

        # --- Linux browses the phone (Kotlin Shares)
        ch = A.sessions[kid].channel
        names = sorted(e["name"] for e in (await ch.request({"t": "fs_list", "path": "/Internal"}))["entries"])
        assert names == ["Docs", "escape", "photo.jpg"], names
        try:
            await ch.request({"t": "fs_list", "path": "/Internal/escape"})
            raise AssertionError("symlink escape allowed on Kotlin side")
        except Exception as e:
            assert getattr(e, "code", "") == "denied", e
        try:
            await ch.request({"t": "fs_list", "path": "/Internal/../.."})
            raise AssertionError("dot-dot allowed")
        except Exception as e:
            assert getattr(e, "code", "") == "denied", e
        base = f"http://127.0.0.1:{49291 + 1000}/{A.cfg['dav_token']}/KtPhone"
        async with aiohttp.ClientSession() as http:
            r = await http.request("PROPFIND", base + "/Internal/", headers={"Depth": "1"})
            assert r.status == 207 and "photo.jpg" in await r.text()
            r = await http.get(base + "/Internal/photo.jpg", headers={"Range": "bytes=100-60099"})
            assert r.status == 206 and await r.read() == (k_share / "photo.jpg").read_bytes()[100:60100]
            r = await http.put(base + "/Internal/Docs/notes%20v2.txt", data=b"n" * 200_000)
            assert r.status == 201 and (k_share / "Docs" / "notes v2.txt").read_bytes() == b"n" * 200_000
            r = await http.request("MOVE", base + "/Internal/Docs/notes%20v2.txt",
                                   headers={"Destination": base + "/Internal/notes.txt"})
            assert r.status == 201 and (k_share / "notes.txt").exists()
            short = base.rsplit("/", 1)[0] + "/Phone"
            r = await http.get(short + "/photo.jpg", headers={"Range": "bytes=0-9"})
            assert r.status == 206 and await r.read() == (k_share / "photo.jpg").read_bytes()[:10]
            r = await http.request("PROPFIND", short + "/", headers={"Depth": "1"})
            assert "/Phone/notes.txt<" in await r.text()
            r = await http.request("MKCOL", base + "/Internal/New")
            assert r.status == 201 and (k_share / "New").is_dir()
            r = await http.delete(base + "/Internal/New")
            assert r.status == 204 and not (k_share / "New").exists()
            r = await http.get(base + "/Internal/photo.jpg")
            await r.content.read(5000)
            r.close()
        await asyncio.sleep(0.3)
        assert (await ch.request({"t": "fs_stat", "path": "/Internal/photo.jpg"}))["entry"]["size"] == 180_000
        print("ok  Linux browses phone (protocol + WebDAV)")

        # --- the phone browses Linux (Kotlin RemoteFs)
        assert (await k.cmd(f"ls {aid} /", "LS")) == "LS Home/"
        assert (await k.cmd(f"ls {aid} /Home", "LS")) == "LS x.bin"
        out = root / "part.bin"
        await k.cmd(f"get {aid} /Home/x.bin {out} 1000 5000")
        assert out.read_bytes() == (a_share / "x.bin").read_bytes()[1000:6000]
        await k.cmd(f"get {aid} /Home/x.bin {out} 0 -1")
        assert out.read_bytes() == (a_share / "x.bin").read_bytes()
        await k.cmd(f"mkdir {aid} /Home/fromphone")
        await k.cmd(f"put {aid} /Home/fromphone/p.jpg {k_share / 'photo.jpg'}")
        assert (a_share / "fromphone" / "p.jpg").read_bytes() == (k_share / "photo.jpg").read_bytes()
        await k.cmd(f"mv {aid} /Home/fromphone/p.jpg /Home/p.jpg")
        await k.cmd(f"rm {aid} /Home/fromphone")
        assert (a_share / "p.jpg").exists() and not (a_share / "fromphone").exists()
        line = await k.cmd(f"ls {aid} /Home/nope", "ERR")
        assert "not_found" in line, line
        print("ok  phone browses Linux")

        # --- folder sync
        await until(lambda: (a_sync / "deep" / "er" / "from_phone.txt").exists()
                    and (k_sync / "from_linux.txt").exists(), timeout=20, what="initial sync")
        await asyncio.sleep(1.1)
        (k_sync / "from_linux.txt").write_text("edited on phone")
        await until(lambda: (a_sync / "from_linux.txt").read_text() == "edited on phone", timeout=20, what="K→A edit")
        (a_sync / "deep" / "er" / "from_phone.txt").unlink()
        await until(lambda: not (k_sync / "deep").exists(), timeout=20, what="A→K delete")
        (a_sync / "big.bin").write_bytes(os.urandom(2_000_000))
        await until(lambda: (k_sync / "big.bin").exists()
                    and (k_sync / "big.bin").read_bytes() == (a_sync / "big.bin").read_bytes(), timeout=20,
                    what="large file")
        (k_sync / "c.txt").write_text("base")
        await until(lambda: (a_sync / "c.txt").exists(), timeout=20, what="c.txt")
        await asyncio.sleep(2.5)
        (a_sync / "c.txt").write_text("linux, newer")
        (k_sync / "c.txt").write_text("phone, older")
        os.utime(k_sync / "c.txt", (time.time() - 100, time.time() - 100))

        def converged():
            fa = sorted(p.name for p in a_sync.iterdir() if p.is_file())
            fk = sorted(p.name for p in k_sync.iterdir() if p.is_file())
            return fa == fk and any("conflict" in n for n in fa)

        await until(converged, timeout=25, what="conflict convergence")
        assert (k_sync / "c.txt").read_text() == "linux, newer"
        cf = next(p for p in k_sync.iterdir() if "conflict" in p.name)
        assert cf.read_text() == "phone, older" == (a_sync / cf.name).read_text()
        print("ok  sync (merge, edit, delete, large file, conflict)")

        # --- remote commands (phone → computer) and notifications (computer → phone)
        A.cfg["commands"] = [{"id": "greet", "name": "Greet", "command": "echo hello from $(basename $SHELL 2>/dev/null || echo sh); exit 2"}]
        assert (await k.cmd(f"cmds {aid}", "CMDS")) == "CMDS shell=false greet=Greet"
        line = await k.cmd(f"run {aid} greet", "RESULT")
        assert line.startswith("RESULT exit=2 timedout=false out=hello from"), line
        assert "denied" in await k.cmd(f"sh {aid} echo nope", "ERR")
        A.cfg["remote_shell"] = True
        assert (await k.cmd(f"sh {aid} printf 'a\\nb'", "RESULT")) == "RESULT exit=0 timedout=false out=a\\nb"
        A.cfg["command_timeout"] = 1
        assert "timedout=true" in await k.cmd(f"sh {aid} sleep 5", "RESULT")
        A.cfg["remote_shell"] = False
        sent = await A.send_notification("Build done", "all tests green ✓")
        assert sent == ["KtPhone"]
        assert (await k.expect("NOTIFY")) == "NOTIFY Build done|all tests green ✓"
        print("ok  remote commands + notifications")

        # --- battery, notifications, clipboard images, media, transfers (Kotlin ⇄ Python)
        await k.cmd("battery 42 true")
        await until(lambda: A.status()["devices"][0]["battery"] == {"level": 42, "charging": True}, what="battery")

        shown, closed = [], []

        class FakeDesk:
            available = True

            async def notify(self, app, title, body, icon="", actions=(), replaces=0):
                shown.append({"app": app, "title": title, "body": body, "actions": list(actions)})
                return 500 + len(shown)

            async def close(self, nid):
                closed.append(nid)

        A.desk = FakeDesk()
        await k.cmd("notif k1|Ana|see you at 8|reply")
        await until(lambda: shown, what="phone notification on desktop")
        assert shown[0]["title"] == "Ana" and shown[0]["actions"] == [("reply", "Reply")], shown
        assert shown[0]["app"] == "Signal · KtPhone"
        await A.notif_reply(kid, "k1", "on my way")
        assert (await k.expect("REPLY")) == "REPLY k1|on my way"
        try:
            await A.notif_reply(kid, "gone", "x")
            raise AssertionError("reply to unknown notification succeeded")
        except Exception as e:
            assert getattr(e, "code", "") == "not_found", e
        A._on_notif_closed(501, 2)
        assert (await k.expect("DISMISS")) == "DISMISS k1"
        await k.cmd("notif k2|Bob|hi|none")
        await until(lambda: len(shown) == 2, what="second notification")
        await k.cmd("unnotif k2")
        await until(lambda: closed == [502], what="closed after removal on phone")
        print("ok  battery + notifications (show, reply, dismiss, remove)")

        img = os.urandom(400_000)
        A.on_local_clip_image("image/png", img)
        line = await k.expect("CLIPIMG")
        assert line == "CLIPIMG image/png " + hashlib.sha256(img).hexdigest(), line
        img2 = root / "shot.png"
        img2.write_bytes(os.urandom(200_000))
        await k.cmd(f"clipimg {img2}")
        await until(lambda: getattr(A.clip, "image", None) == ("image/png", img2.read_bytes()), what="image K→A")
        print("ok  clipboard images both ways")

        calls = []

        class FakeMedia:
            available = True
            st = {"players": [{"name": "spotify", "identity": "Spotify", "status": "Playing", "title": "Song",
                               "artist": "Band", "album": "", "length": 200000, "position": 1000, "at": 0}],
                  "volume": {"level": 0.5, "muted": False}, "available": True}

            async def state(self):
                return self.st

            async def command(self, player, action, value=None):
                calls.append((player, action, value))
                return self.st

            async def refresh(self):
                return self.st

        A.media = FakeMedia()
        assert (await k.cmd(f"mstate {aid}", "MSTATE")) == "MSTATE Song|Band|0.5"
        await k.cmd(f"media {aid} next", "MSTATE")
        await k.cmd(f"media {aid} volume 0.25", "MSTATE")
        assert calls == [(None, "next", None), (None, "volume", 0.25)], calls
        A._on_media_change(FakeMedia.st)
        while (line := await k.expect("MEDIA")) != "MEDIA Song|Playing":
            pass  # earlier updates (the real player list at connect time) may be queued first
        print("ok  media control from the phone")

        big = root / "movie.mkv"
        big.write_bytes(os.urandom(4_000_000))
        await A.send_file(kid, str(big))
        assert (await k.expect("TRANSFER movie.mkv")) == "TRANSFER movie.mkv done 4000000"
        (root / "cancel-in.bin").write_bytes(os.urandom(4_000_000))
        try:
            await A.send_file(kid, str(root / "cancel-in.bin"))
            raise AssertionError("transfer the phone cancelled completed")
        except Exception as e:
            assert getattr(e, "code", "") == "cancelled", e
        assert (await k.expect("TRANSFER cancel-in.bin")).startswith("TRANSFER cancel-in.bin cancelled")
        (root / "cancel-out.bin").write_bytes(os.urandom(4_000_000))
        line = await k.cmd(f"send {aid} {root / 'cancel-out.bin'}", "ERR")
        assert "cancelled" in line, line
        assert not (root / "A" / "downloads" / "cancel-out.bin").exists()
        print("ok  transfer progress + cancel (Kotlin side)")

        # --- wake-on-LAN: the computer shares its network cards; the phone sends magic packets
        import socket as _socket
        import sys as _sys
        _sys.path.insert(0, str(ROOT / "linux"))
        from tether import wol as _wol
        cards = _wol.interfaces(False)
        if cards:
            line = await k.cmd(f"canwake {aid}", "CANWAKE")
            assert line.startswith("CANWAKE true") and cards[0]["mac"] in line, line
            rx = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            rx.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            rx.bind(("0.0.0.0", 40009))
            rx.setblocking(False)
            sent = int((await k.cmd(f"wake {aid} 40009", "WOKE")).split()[1])
            assert sent > 0
            loop = asyncio.get_running_loop()
            data = await asyncio.wait_for(loop.sock_recv(rx, 200), 5)
            rx.close()
            assert data == _wol.magic_packet(cards[0]["mac"]), data[:12]
            print(f"ok  wake-on-LAN ({sent} packets, magic packet verified)")
        else:
            print("skip wake-on-LAN (no physical interface here)")

        # --- reconnect
        await k.cmd(f"drop {aid}")
        await until(lambda: not A.is_connected(kid), what="drop")
        await k.cmd("reconnect")
        await until(lambda: A.is_connected(kid), timeout=15, what="reconnect")
        A.on_local_clip("after reconnect")
        assert (await k.expect("CLIP")) == "CLIP after reconnect"
        print("ok  reconnect")

        # --- unpair propagates to the phone
        await A.unpair(kid)
        await k.expect("UNPAIRED")
        print("ok  unpair")
        print("ALL INTEROP TESTS PASSED")
    finally:
        try:
            proc.stdin.write(b"quit\n")
            await asyncio.wait_for(proc.wait(), 5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    root = Path(tempfile.mkdtemp(prefix="tether-interop-"))
    try:
        asyncio.run(asyncio.wait_for(main(root, sys.argv[1]), 240))
    except BaseException:
        log = root / "kt.log"
        if log.exists():
            print("--- kotlin log ---\n" + log.read_text()[-4000:])
        raise
    finally:
        shutil.rmtree(root, ignore_errors=True)
