"""The phone screen's viewer: choosing a player, finding the display, feeding a real process."""

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tether import phonescreen  # noqa: E402
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
            await asyncio.wait_for(sink.wait_exit(), 1)

            # The window closes: wait_exit() says so, and writing afterwards does no harm.
            phonescreen.viewer_command = lambda title: ["sh", "-c", "exit 0"]
            sink = phonescreen.ViewerSink("phone")
            await asyncio.wait_for(sink.wait_exit(), 5)
            sink.write(b"x" * 1024)
            sink.write(b"y" * phonescreen.MAX_BUFFER)  # a stalled viewer costs bounded memory
            sink.write(b"z" * phonescreen.MAX_BUFFER)
            assert len(sink.buf) <= phonescreen.MAX_BUFFER
            await sink.close()
    finally:
        phonescreen.viewer_command, phonescreen.display_env = command, env
    print("ok  viewer sink")


if __name__ == "__main__":
    test_viewer_command()
    test_display_env()
    asyncio.run(test_sink())
