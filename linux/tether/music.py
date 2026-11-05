"""The computer's music folder, with the phone as its remote: mpv plays here, and the phone browses and steers."""

import logging
import os
import re
import subprocess
from pathlib import Path

from .errors import ProtoError
from .mpvcontrol import MpvControl
from .phonescreen import MPRIS_SCRIPTS

log = logging.getLogger("tether.music")

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".m4b", ".aac", ".wav", ".wv", ".ape", ".aiff",
              ".aif", ".alac", ".wma", ".mka", ".mpc", ".dsf", ".dff", ".tta"}
MAX_QUEUE = 5000


def music_dir(configured: str | None = None) -> Path:
    """The configured folder, else the desktop's own idea of it (user-dirs.dirs), else ~/Music."""
    if configured:
        return Path(configured).expanduser()
    home = Path.home()
    conf = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
    try:
        m = re.search(r'^XDG_MUSIC_DIR="([^"]+)"', conf.read_text(), re.M)
    except OSError:
        m = None
    if m and m[1].replace("$HOME", str(home)).rstrip("/") != str(home):
        return Path(m[1].replace("$HOME", str(home)))
    return home / "Music"


def resolve(root: Path, path) -> Path:
    """A path from the phone, which must stay inside the music folder, even through symlinks."""
    if not isinstance(path, str) or "\0" in path:
        raise ProtoError("bad_request")
    real = root.resolve()
    target = (real / path.lstrip("/")).resolve()
    if not target.is_relative_to(real):
        raise ProtoError("denied")
    if not target.exists():
        raise ProtoError("not_found")
    return target


def _is_track(p: Path) -> bool:
    return p.suffix.lower() in AUDIO_EXTS and not p.name.startswith(".")


def _order(name: str):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)]


def list_folder(root: Path, path) -> list[dict]:
    """Folders first, then tracks; everything else is left out."""
    folder = resolve(root, path)
    if not folder.is_dir():
        raise ProtoError("is_file")
    entries = []
    with os.scandir(folder) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    entries.append({"name": e.name, "dir": True})
                elif _is_track(Path(e.name)):
                    entries.append({"name": e.name, "dir": False, "size": e.stat().st_size})
            except OSError:
                continue
    return sorted(entries, key=lambda e: (not e["dir"], _order(e["name"])))


def make_queue(root: Path, path) -> tuple[list[str], int]:
    """What to play and where to begin: a track brings the rest of its folder, a folder everything inside it."""
    target = resolve(root, path)
    if target.is_file():
        if not _is_track(target):
            raise ProtoError("bad_request")
        tracks = sorted((p for p in target.parent.iterdir() if p.is_file() and _is_track(p)), key=lambda p: _order(p.name))
        return [str(p) for p in tracks[:MAX_QUEUE]], min(tracks.index(target), MAX_QUEUE - 1)
    tracks = []
    for base, dirs, files in os.walk(target):
        dirs[:] = sorted((d for d in dirs if not d.startswith(".")), key=_order)
        tracks += [os.path.join(base, f) for f in sorted(files, key=_order) if _is_track(Path(f))]
        if len(tracks) >= MAX_QUEUE:
            break
    if not tracks:
        raise ProtoError("not_found")
    return tracks[:MAX_QUEUE], 0


class MusicPlayer(MpvControl):
    """A windowless mpv."""

    def __init__(self, tracks: list[str], start: int, volume: float = 100):
        super().__init__("music")
        cmd = ["mpv", "--no-config", "--no-terminal", "--no-video", "--force-window=no", "--gapless-audio=weak",
               f"--input-ipc-server={self.socket}", f"--playlist-start={start}", f"--volume={volume}"]
        mpris = next((p for p in MPRIS_SCRIPTS if os.path.exists(p)), None)
        if mpris:
            cmd.append(f"--script={mpris}")
        self.proc = subprocess.Popen([*cmd, "--", *tracks], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
