"""The computer's Movies folder, browsed and searched from the phone, how far each film has been
watched, and what plays after what."""

import asyncio
import logging
import os
import re
import time
import unicodedata
from pathlib import Path

from .config import atomic_write_json, read_json
from .errors import ProtoError
from .music import MAX_QUEUE, _order, list_folder, resolve, user_dir

log = logging.getLogger("tether.movies")

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob",
              ".flv", ".ogv", ".3gp", ".divx", ".rmvb"}
MAX_RESULTS = 200
MAX_CONTINUE = 10
MIN_RESUME = 30  # seconds: less than this isn't worth coming back to
WATCHED_AT = 0.95  # the credits needn't be sat through
ROLL_ON_BELOW_S = 60 * 60  # shorter than this is an episode, so the next one follows; a feature film ends the evening
SAMPLE_S = 1  # how often the player is asked where it is
SAVE_S = 30  # and how often that reaches the disk, besides when it ends
EPISODE = re.compile(r"(?<![a-z0-9])s(\d+)\s*e(\d+)", re.I)


def movies_dir(configured: str | None = None) -> Path:
    """The configured folder, else ~/Movies if there is one, else the desktop's Videos folder."""
    if configured:
        return Path(configured).expanduser()
    movies = Path.home() / "Movies"
    return movies if movies.is_dir() else user_dir(None, "XDG_VIDEOS_DIR", "Videos")


def _is_movie(p: Path) -> bool:
    return p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")


def find(root: Path, path) -> tuple[Path, str]:
    """A film the phone named: where it is, and the name its progress is logged under."""
    target = resolve(root, path)
    if not target.is_file() or not _is_movie(target):
        raise ProtoError("bad_request")
    return target, target.relative_to(root.resolve()).as_posix()


def _film_order(name: str):
    """By season and episode where the name says so, whatever precedes it (WP_s1e22b belongs by s1e22a); else by name."""
    m = EPISODE.search(name.replace("_", " "))
    return (0, int(m[1]), int(m[2]), _order(name[m.end():])) if m else (1, 0, 0, _order(name))


def playlist(root: Path, path, progress: "Progress", resume) -> dict:
    """The film's whole folder, so that next and previous work, starting at the film. Each opens where it
    was left, except the chosen one if it's being started over."""
    target, _ = find(root, path)
    real = root.resolve()
    films = [p for p in target.parent.iterdir()
             if p.is_file() and _is_movie(p) and p.resolve().is_relative_to(real)]
    films.sort(key=lambda p: _film_order(p.name))
    first = next(i for i, p in enumerate(films) if p.name == target.name)
    cut = max(0, first - MAX_QUEUE // 2)
    films, first = films[cut:cut + MAX_QUEUE], first - cut
    rels = [p.relative_to(real).as_posix() for p in films]
    places = {i: progress.resume_point(rel) for i, rel in enumerate(rels)}
    if not resume:
        places[first] = 0
    return {"files": [str(p) for p in films], "rels": rels, "first": first,
            "resume": {i: place for i, place in places.items() if place}}


async def follow(player, rels: list[str], progress: "Progress", finish) -> None:
    """While a held player plays: logs each film's progress, and at the end of one moves on or calls `finish`."""
    saved = time.monotonic()
    try:
        while player.alive():
            state = await player.state()
            index, position, length = state.get("index"), state.get("position"), state.get("length")
            if position is not None and isinstance(index, int) and 0 <= index < len(rels):
                progress.note(rels[index], position, length)
                if state.get("ended"):
                    if length and length < ROLL_ON_BELOW_S and index + 1 < len(rels):
                        try:
                            await player.command("next")
                        except ProtoError:
                            log.warning("the player didn't move on to the next film")
                    else:
                        await finish()
                        return
            if time.monotonic() - saved >= SAVE_S:
                progress.save()
                saved = time.monotonic()
            await asyncio.sleep(SAMPLE_S)
    finally:
        progress.save()


def _fold(text: str) -> str:
    """For matching: no case, accents or punctuation, so "the matrix" finds The.Matrix.1999.mkv and "amelie" Amélie."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", re.sub(r"['’`]", "", plain.casefold())).strip()


def search(root: Path, query, progress: "Progress") -> list[dict]:
    """Every film anywhere in the folder whose path holds all the words of the query."""
    if not isinstance(query, str):
        raise ProtoError("bad_request")
    words = _fold(query).split()
    if not words:
        return []
    real = resolve(root, "/")
    found = []
    for base, dirs, files in os.walk(real):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            full = Path(base, name)
            if not _is_movie(full):
                continue
            rel = full.relative_to(real).as_posix()
            folded = _fold(rel)
            if all(w in folded for w in words) and full.resolve().is_relative_to(real):
                found.append(rel)
    found.sort(key=lambda rel: (rel.rpartition("/")[0].casefold(), _film_order(rel.rpartition("/")[2])))
    return [_entry(real, rel, progress) for rel in found[:MAX_RESULTS]]


def _entry(real: Path, rel: str, progress: "Progress") -> dict:
    entry = {"name": rel.rsplit("/", 1)[-1], "path": rel, "dir": False, **progress.view(rel)}
    try:
        entry["size"] = (real / rel).stat().st_size
    except OSError:
        pass
    return entry


def listing(root: Path, path, progress: "Progress") -> dict:
    """A folder with each film's progress; the top of the library also has what's being watched."""
    if path is None:
        path = "/"
    entries = sorted(list_folder(root, path, keep=_is_movie), key=lambda e: (not e["dir"], _film_order(e["name"])))
    real = root.resolve()
    folder = resolve(root, path).relative_to(real).as_posix()
    for e in entries:
        e["path"] = e["name"] if folder == "." else f"{folder}/{e['name']}"
        if not e["dir"]:
            e.update(progress.view(e["path"]))
    result = {"entries": entries, "root": root.name}
    if folder == ".":
        result["continue"] = [_entry(real, rel, progress) for rel in progress.recent() if (real / rel).is_file()][:MAX_CONTINUE]
    return result


class Progress:
    """How far each film has been watched, kept here so that every phone sees the same. Survives restarts."""

    def __init__(self, path: Path):
        self.path = path
        try:
            data = read_json(path, {})
        except ValueError:
            log.warning("couldn't read %s, starting afresh", path)
            data = {}
        self.films: dict[str, dict] = data if isinstance(data, dict) else {}
        self.dirty = False

    def note(self, rel: str, position: float, length: float | None) -> None:
        old = self.films.get(rel, {})
        if old.get("finished") and position < MIN_RESUME:  # a watched film stays watched until it's really rewatched
            return
        if old.get("position") == position:  # paused
            return
        self.films[rel] = {"position": position, "length": length or 0,
                           "finished": bool(length) and position >= length * WATCHED_AT, "at": time.time()}
        self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            atomic_write_json(self.path, self.films)
            self.dirty = False
        except OSError as e:
            log.warning("couldn't save watching progress: %s", e)

    def _resumable(self, film: dict) -> bool:
        return not film.get("finished") and film.get("position", 0) >= MIN_RESUME

    def resume_point(self, rel: str) -> float:
        film = self.films.get(rel, {})
        return film["position"] if self._resumable(film) else 0

    def view(self, rel: str) -> dict:
        """What the phone shows for a film; nothing if it hasn't really been started."""
        film = self.films.get(rel, {})
        if film.get("finished"):
            return {"finished": True, "length": film.get("length", 0)}
        if self._resumable(film):
            return {"position": film["position"], "length": film.get("length", 0)}
        return {}

    def recent(self) -> list[str]:
        """Films left part way through, latest first."""
        started = [(film.get("at", 0), rel) for rel, film in list(self.films.items()) if self._resumable(film)]
        return [rel for _, rel in sorted(started, reverse=True)]
