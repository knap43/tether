"""The computer's music folder, with the phone as its remote: mpv plays here, and the phone browses and steers."""

import logging
import os
import re
import subprocess
import unicodedata
from pathlib import Path

from .errors import ProtoError
from .mpvcontrol import MpvControl

log = logging.getLogger("tether.music")

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".m4b", ".aac", ".wav", ".wv", ".ape", ".aiff",
              ".aif", ".alac", ".wma", ".mka", ".mpc", ".dsf", ".dff", ".tta"}
MAX_QUEUE = 5000
MAX_RESULTS = 200


def user_dir(configured: str | None, xdg_key: str, fallback: str) -> Path:
    """The configured folder, else the desktop's own idea of it (user-dirs.dirs), else the usual place in the home folder."""
    if configured:
        return Path(configured).expanduser()
    home = Path.home()
    conf = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
    try:
        m = re.search(rf'^{xdg_key}="([^"]+)"', conf.read_text(), re.M)
    except OSError:
        m = None
    if m and m[1].replace("$HOME", str(home)).rstrip("/") != str(home):
        return Path(m[1].replace("$HOME", str(home)))
    return home / fallback


def music_dir(configured: str | None = None) -> Path:
    return user_dir(configured, "XDG_MUSIC_DIR", "Music")


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


def list_folder(root: Path, path, keep=_is_track) -> list[dict]:
    """Folders first, then the files worth showing; everything else is left out."""
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
                elif keep(Path(e.name)):
                    entries.append({"name": e.name, "dir": False, "size": e.stat().st_size})
            except OSError:
                continue
    return sorted(entries, key=lambda e: (not e["dir"], _order(e["name"])))


def _fold(text: str) -> str:
    """For matching: no case, accents or punctuation, so "the matrix" finds The.Matrix.1999.mkv and "amelie" Amélie."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", re.sub(r"['’`]", "", plain.casefold())).strip()


def search(root: Path, query) -> list[dict]:
    """Folders and tracks anywhere in the library whose path holds all the words of the query.

    A folder that matches stands for everything inside it, so an album found by name comes back once,
    not as every one of its tracks. Folders come first.
    """
    if not isinstance(query, str):
        raise ProtoError("bad_request")
    words = _fold(query).split()
    if not words:
        return []
    real = resolve(root, "/")
    folders, tracks = [], []
    for base, dirs, files in os.walk(real):
        keep = []
        for d in dirs:
            if d.startswith("."):
                continue
            full = Path(base, d)
            rel = full.relative_to(real).as_posix()
            if not full.resolve().is_relative_to(real):
                continue
            if all(w in _fold(rel) for w in words):
                folders.append(rel)
            else:
                keep.append(d)
        dirs[:] = keep
        for name in files:
            full = Path(base, name)
            if not _is_track(full):
                continue
            rel = full.relative_to(real).as_posix()
            if all(w in _fold(rel) for w in words) and full.resolve().is_relative_to(real):
                tracks.append(rel)
        if len(folders) + len(tracks) >= MAX_RESULTS * 4:
            break

    def order(rel):
        return _order(rel.rpartition("/")[0]), _order(rel.rpartition("/")[2])

    found = [(rel, True) for rel in sorted(folders, key=order)] + [(rel, False) for rel in sorted(tracks, key=order)]
    entries = []
    for rel, is_dir in found[:MAX_RESULTS]:
        entry = {"name": rel.rsplit("/", 1)[-1], "path": rel, "dir": is_dir}
        if not is_dir:
            try:
                entry["size"] = (real / rel).stat().st_size
            except OSError:
                pass
        entries.append(entry)
    return entries


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
        self.proc = subprocess.Popen([*cmd, "--", *tracks], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
