"""The music folder: staying inside it, listing and queueing it, and a real mpv steered over its socket."""

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tether import music, phoneplay  # noqa: E402
from tether.errors import ProtoError  # noqa: E402


def code_of(fn, *args):
    try:
        fn(*args)
    except ProtoError as e:
        return e.code
    return None


def test_folder(root: Path, outside: Path):
    (root / "Album 10").mkdir()
    (root / "Album 2").mkdir()
    (root / ".hidden").mkdir()
    for name in ("Track 10.flac", "Track 2.mp3", "cover.jpg", ".secret.mp3"):
        (root / "Album 2" / name).write_bytes(b"x")
    (root / "Album 10" / "a.ogg").write_bytes(b"x")
    (root / "loose.opus").write_bytes(b"x")
    (outside / "private.mp3").write_bytes(b"x")
    (root / "leak").symlink_to(outside)

    names = [e["name"] for e in music.list_folder(root, "/") if e["dir"]]
    assert names == ["Album 2", "Album 10", "leak"], names  # numbers in numeric order; hidden left out
    assert [e["name"] for e in music.list_folder(root, "/") if not e["dir"]] == ["loose.opus"]
    assert [e["name"] for e in music.list_folder(root, "Album 2")] == ["Track 2.mp3", "Track 10.flac"]

    assert code_of(music.list_folder, root, "../") == "denied"
    assert code_of(music.list_folder, root, "/../../etc") == "denied"
    assert code_of(music.list_folder, root, "leak") == "denied"  # a symlink out of the folder
    assert code_of(music.make_queue, root, "leak/private.mp3") == "denied"
    assert code_of(music.list_folder, root, "missing") == "not_found"
    assert code_of(music.list_folder, root, 7) == "bad_request"
    assert code_of(music.make_queue, root, "Album 2/cover.jpg") == "bad_request"

    tracks, start = music.make_queue(root, "Album 2/Track 10.flac")
    assert [Path(t).name for t in tracks] == ["Track 2.mp3", "Track 10.flac"] and start == 1
    tracks, start = music.make_queue(root, "Album 10")
    assert [Path(t).name for t in tracks] == ["a.ogg"] and start == 0

    (root / "leak").unlink()
    print("ok  music folder")


def tone(path: Path, seconds: int, title: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-metadata", f"title={title}",
                    "-metadata", "artist=Tester", str(path)], check=True)


async def until(test, what):
    for _ in range(200):
        value = await test()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


async def test_player(folder: Path):
    if not (shutil.which("mpv") and shutil.which("ffmpeg")):
        print("--  player skipped (needs mpv and ffmpeg)")
        return
    tone(folder / "1.ogg", 30, "First")
    tone(folder / "2.ogg", 30, "Second")
    # Silent output: the test mustn't play through this computer's speakers.
    real_popen = music.subprocess.Popen
    music.subprocess.Popen = lambda cmd, **k: real_popen([cmd[0], "--ao=null", *cmd[1:]], **k)
    player = music.MusicPlayer([str(folder / "1.ogg"), str(folder / "2.ogg")], 0, volume=40)
    music.subprocess.Popen = real_popen
    try:
        async def named(title):
            s = await player.state()
            return s if s.get("title") == title and s.get("position") is not None else None

        s = await until(lambda: named("First"), "the first track")
        assert s["artist"] == "Tester" and s["count"] == 2 and s["volume"] == 40 and not s["paused"], s
        assert s["length"] > 29
        await player.command("toggle")
        assert (await player.state())["paused"]
        await player.command("toggle")
        await player.command("position", 10)
        await until(lambda: _past(player, 9), "the seek")
        await player.command("volume", 75)
        assert (await player.state())["volume"] == 75
        await player.command("volume", 900)
        assert (await player.state())["volume"] == 100
        await player.command("next")
        await until(lambda: named("Second"), "the next track")
        await player.command("position", 8)
        await until(lambda: _past(player, 7), "the second seek")
        await player.command("previous")  # past the start: back to the start of this track
        await until(lambda: _before(player, 3), "the start of the track")
        assert (await player.state())["title"] == "Second"
        await player.command("previous")  # at the start: the track before
        await until(lambda: named("First"), "the track before")
        try:
            await player.command("reboot")
            raise AssertionError("unknown action accepted")
        except ProtoError as e:
            assert e.code == "bad_request"
    finally:
        await player.stop()
    assert not player.alive() and (await player.state()) == {"playing": False}
    assert not Path(player.socket).exists()
    print("ok  music player")


async def test_video_player(folder: Path):
    """A shared video answers the same controls; `stop` closes it."""
    if not (shutil.which("mpv") and shutil.which("ffmpeg")):
        return
    video = folder / "film.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=64x64:r=5:d=30",
                    "-metadata", "title=Film", str(video)], check=True)
    display_env, real_popen = phoneplay.display_env, phoneplay.subprocess.Popen
    phoneplay.display_env = dict
    phoneplay.subprocess.Popen = lambda cmd, **k: real_popen([cmd[0], "--vo=null", "--ao=null", *cmd[1:]], **k)
    try:
        player = phoneplay.Player([str(video)], "phone")
    finally:
        phoneplay.display_env, phoneplay.subprocess.Popen = display_env, real_popen
    try:
        async def started():
            s = await player.state()
            return s if s.get("position") is not None else None

        s = await until(started, "the video")
        assert s["title"] == "Film" and s["length"] > 29 and not s["paused"], s
        await player.command("toggle")
        assert (await player.state())["paused"]
        await player.command("toggle")
        await player.command("position", 20)
        await until(lambda: _past(player, 19), "the seek")
    finally:
        await player.stop()
    assert not player.alive() and not Path(player.socket).exists()
    print("ok  video player")


async def _past(player, seconds):
    return ((await player.state()).get("position") or 0) > seconds


async def _before(player, seconds):
    pos = (await player.state()).get("position")
    return pos is not None and pos < seconds


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        root, outside = Path(d) / "Music", Path(d) / "outside"
        root.mkdir()
        outside.mkdir()
        test_folder(root, outside)
        asyncio.run(test_player(outside))
        asyncio.run(test_video_player(outside))
