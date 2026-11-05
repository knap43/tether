"""The phone screen's viewer: choosing a player, finding the display, feeding a real process."""

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tether import phoneplay, phonescreen  # noqa: E402
from tether.errors import ProtoError  # noqa: E402


def with_players(*names):
    phonescreen.shutil.which = lambda n: f"/usr/bin/{n}" if n in names else None


def test_viewer_command():
    which = phonescreen.shutil.which
    try:
        with_players("mpv", "ffplay")
        cmd = phonescreen.viewer_command("${shell} phone")
        assert cmd[0] == "mpv" and cmd[-1] == "-"
        assert "--title=$${shell} phone" in cmd  # a phone's name isn't a template
        with_players("ffplay")
        cmd = phonescreen.viewer_command("phone")
        assert cmd[0] == "ffplay" and cmd[-1] == "-" and cmd[cmd.index("-window_title") + 1] == "phone"
        with_players()
        assert not phonescreen.available()
        try:
            phonescreen.viewer_command("phone")
            raise AssertionError("no player, but a command")
        except ProtoError as e:
            assert e.code == "unsupported"
    finally:
        phonescreen.shutil.which = which
    print("ok  viewer command")


def test_display_env():
    run, environ, platform = subprocess.run, phonescreen.os.environ, phonescreen.sys.platform
    answer = "PATH=/nope\nWAYLAND_DISPLAY=wayland-7\nDISPLAY=:7\nSECRET=x\n"
    phonescreen.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=answer)
    phonescreen.sys.platform = "linux"
    try:
        phonescreen.os.environ = {"PATH": "/bin"}
        env = phonescreen.display_env()
        assert env == {"PATH": "/bin", "WAYLAND_DISPLAY": "wayland-7", "DISPLAY": ":7"}, env
        phonescreen.os.environ = {"DISPLAY": ":1"}  # already has one: nothing to ask
        assert phonescreen.display_env() == {"DISPLAY": ":1"}
        answer = "PATH=/nope\n"
        phonescreen.os.environ = {}
        try:
            phonescreen.display_env()
            raise AssertionError("no session, but a display")
        except ProtoError as e:
            assert e.code == "unsupported"
    finally:
        phonescreen.subprocess.run, phonescreen.os.environ, phonescreen.sys.platform = run, environ, platform
    print("ok  display environment")


async def test_sink():
    command, env = phonescreen.viewer_command, phonescreen.display_env
    phonescreen.display_env = lambda: None
    try:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "video"
            phonescreen.viewer_command = lambda title: ["sh", "-c", f'cat > "{out}"']
            sink = phonescreen.ViewerSink("phone")
            data = bytes(range(256)) * 2000  # several pipe buffers
            sink.write(data)
            for _ in range(100):
                if out.exists() and out.stat().st_size == len(data):
                    break
                await asyncio.sleep(0.05)
            assert out.read_bytes() == data
            await sink.close()
            assert sink.proc.poll() is not None

            # The picture waits for the sound.
            out.unlink()
            sink = phonescreen.ViewerSink("phone")
            sink.sync_delay = lambda: 0.4
            sink.write(b"late")
            await asyncio.sleep(0.2)
            assert not out.exists() or out.stat().st_size == 0
            for _ in range(60):
                if out.stat().st_size == 4:
                    break
                await asyncio.sleep(0.05)
            assert out.read_bytes() == b"late"
            sink.sync_delay = lambda: "nonsense"  # a bad setting mustn't stop the picture
            assert sink._delay() == 0.0
            await sink.close()
            await asyncio.wait_for(sink.wait_exit(), 1)

            # The window closes: wait_exit() says so, and writing afterwards does no harm.
            phonescreen.viewer_command = lambda title: ["sh", "-c", "exit 0"]
            sink = phonescreen.ViewerSink("phone")
            await asyncio.wait_for(sink.wait_exit(), 5)
            sink.write(b"x" * 1024)
            sink.write(b"y" * phonescreen.MAX_BUFFER)  # a stalled viewer costs bounded memory
            sink.write(b"z" * phonescreen.MAX_BUFFER)
            assert sink.buffered <= phonescreen.MAX_BUFFER
            await sink.close()
    finally:
        phonescreen.viewer_command, phonescreen.display_env = command, env
    print("ok  viewer sink")


def test_urls():
    for good in ("https://youtu.be/abc", "http://example.com/a.mp4?x=1"):
        assert phoneplay.valid_url(good), good
    for bad in ("file:///etc/passwd", "av://v4l2:/dev/video0", "--script=/tmp/x.lua", "https://", "ftp://a/b",
                "https://a b/", "https://a/\n--x", None, 7, "https://a/" + "x" * 5000):
        assert not phoneplay.valid_url(bad), bad
    if phonescreen.shutil.which("yt-dlp"):
        assert asyncio.run(phoneplay.can_play("https://youtu.be/abc"))  # answered without asking the web
    print("ok  link checks")


async def test_file_server():
    from aiohttp import ClientSession

    data = bytes(range(256)) * 400

    class FakeIncoming:
        def __init__(self, part):
            self.part, self.done = part, False

        async def chunks(self):
            yield self.part
            self.done = True

        def abort(self):
            self.done = True

    class FakeChannel:
        reads = []

        async def request(self, msg):
            FakeChannel.reads.append((msg["offset"], msg["length"]))
            return {"_in": FakeIncoming(data[msg["offset"] : msg["offset"] + msg["length"]])}

    server = phoneplay.FileServer(FakeChannel())
    url = (await server.start([{"id": "1", "name": "a b.mkv", "size": len(data), "mime": "video/x-matroska"}]))[0]
    assert "a%20b.mkv" in url
    async with ClientSession() as http:
        async with http.get(url) as r:
            assert r.status == 200 and await r.read() == data
        async with http.get(url, headers={"Range": "bytes=100-199"}) as r:
            assert r.status == 206 and await r.read() == data[100:200]
            assert r.headers["Content-Range"] == f"bytes 100-199/{len(data)}"
        async with http.get(url, headers={"Range": "bytes=-10"}) as r:
            assert r.status == 206 and await r.read() == data[-10:]
        async with http.get(url, headers={"Range": f"bytes={len(data)}-"}) as r:
            assert r.status == 416
        async with http.get(url.replace(server.token, "wrong")) as r:
            assert r.status == 404  # the address is the secret
    await server.stop()
    print("ok  file server")


if __name__ == "__main__":
    test_urls()
    asyncio.run(test_file_server())
    test_viewer_command()
    test_display_env()
    asyncio.run(test_sink())
