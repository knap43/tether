"""Folder sync: local indexes, three-way planning, and applying pulls safely."""

import hashlib
import os
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from .config import atomic_write_json, read_json
from .errors import ProtoError
from .fsops import TMP_PREFIX, split_path

TOMBSTONE_TTL_MS = 30 * 24 * 3600 * 1000


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def ignored(name: str) -> bool:
    """Our temp files, and the metadata Finder litters folders with."""
    return name.startswith((".tether", "._")) or name in (".DS_Store", ".localized", "Icon\r")


def safe_rel(rel) -> str:
    parts = split_path(rel)
    if not parts or any(ignored(p) for p in parts):
        raise ProtoError("bad_request")
    return "/".join(parts)


def sha256_file(path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def conflict_name(rel: str, when: float | None = None) -> str:
    d, _, base = rel.rpartition("/")
    stem, dot, ext = base.rpartition(".")
    if not stem:  # no extension, or a dotfile
        stem, dot, ext = base, "", ""
    stamp = datetime.fromtimestamp(when or time.time()).strftime("%Y%m%d-%H%M%S")
    name = f"{stem}.conflict-{stamp}{dot}{ext}"
    return f"{d}/{name}" if d else name


class FolderIndex:
    """relpath -> {"h": sha256|None, "s": size, "m": mtime_ms, "n": mtime_ns}"""

    def __init__(self, folder_id: str, root: Path, state_dir: Path):
        self.id = folder_id
        self.root = Path(root)
        self.file = state_dir / f"index-{folder_id}.json"
        self.files: dict[str, dict] = read_json(self.file, {})

    def scan(self) -> bool:
        """Rescan the folder (blocking). Returns True if the index changed."""
        if not self.root.is_dir():
            return False
        seen: dict[str, os.stat_result] = {}
        self._walk(str(self.root), "", seen)
        changed = False
        for rel, st in seen.items():
            old = self.files.get(rel)
            if old and old["h"] and old["s"] == st.st_size and old.get("n") == st.st_mtime_ns:
                continue
            try:
                h = sha256_file(os.path.join(self.root, rel))
                st = os.stat(os.path.join(self.root, rel))
            except OSError:
                continue
            if old and old["h"] == h and old["s"] == st.st_size:
                old["n"] = st.st_mtime_ns  # touched, not modified
                old["m"] = st.st_mtime_ns // 1_000_000
                changed = True
                continue
            self.files[rel] = {"h": h, "s": st.st_size, "m": st.st_mtime_ns // 1_000_000, "n": st.st_mtime_ns}
            changed = True
        now = now_ms()
        for rel, e in list(self.files.items()):
            if rel in seen:
                continue
            if e["h"] is not None:
                self.files[rel] = {"h": None, "s": 0, "m": now, "n": 0}
                changed = True
            elif now - e["m"] > TOMBSTONE_TTL_MS:
                del self.files[rel]
                changed = True
        if changed:
            self.save()
        return changed

    def _walk(self, abs_dir: str, rel_dir: str, out: dict) -> None:
        try:
            it = os.scandir(abs_dir)
        except OSError:
            return
        with it:
            for de in it:
                if ignored(de.name):
                    continue
                # APFS and HFS+ ignore normalization on lookup, so NFC names open fine and match Android's.
                name = unicodedata.normalize("NFC", de.name)
                rel = f"{rel_dir}/{name}" if rel_dir else name
                try:
                    if de.is_symlink():
                        continue
                    if de.is_dir():
                        self._walk(de.path, rel, out)
                    elif de.is_file():
                        out[rel] = de.stat()
                except OSError:
                    continue

    def save(self) -> None:
        atomic_write_json(self.file, self.files)

    def wire(self) -> dict:
        return {rel: [e["h"], e["s"], e["m"]] for rel, e in self.files.items()}

    def matches_disk(self, rel: str) -> bool:
        """True if the file on disk is still what the index says."""
        e = self.files.get(rel)
        p = os.path.join(self.root, rel)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            return e is None or e["h"] is None
        except OSError:
            return False
        return bool(e and e["h"] and e["s"] == st.st_size and e.get("n") == st.st_mtime_ns)


class BaseState:
    """Per (peer, folder): relpath -> hash both sides last agreed on."""

    def __init__(self, state_dir: Path, peer_id: str, folder_id: str):
        self.file = state_dir / f"base-{peer_id[:16]}-{folder_id}.json"
        self.map: dict[str, str] = read_json(self.file, {})
        self.dirty = False

    def get(self, rel):
        return self.map.get(rel)

    def set(self, rel, h) -> None:
        if h is None:
            if self.map.pop(rel, None) is not None:
                self.dirty = True
        elif self.map.get(rel) != h:
            self.map[rel] = h
            self.dirty = True

    def save(self) -> None:
        if self.dirty:
            atomic_write_json(self.file, self.map)
            self.dirty = False


def plan(local: dict, remote: dict, base: BaseState, my_id: str, peer_id: str) -> list[tuple]:
    """Decide what to do for each path. local/remote: rel -> [hash|None, size, mtime_ms].

    Returns a list of (action, rel, remote_entry) with action in
    {"pull", "delete", "conflict"}; records agreements in `base`.
    """
    actions = []
    for rel, r in remote.items():
        try:
            if safe_rel(rel) != rel:
                continue
        except ProtoError:
            continue
        if not (isinstance(r, list) and len(r) == 3):
            continue
        rh = r[0]
        l = local.get(rel)
        lh = l[0] if l else None
        b = base.get(rel)
        if lh == rh:
            base.set(rel, lh)
        elif rh == b:
            continue
        elif lh == b:
            actions.append(("delete" if rh is None else "pull", rel, r))
        elif rh is None:
            continue
        elif lh is None:
            actions.append(("pull", rel, r))
        elif (l[2], my_id) < (r[2], peer_id):
            actions.append(("conflict", rel, r))
    return actions


def remove_empty_parents(root: Path, rel: str) -> None:
    parts = rel.split("/")[:-1]
    while parts:
        try:
            os.rmdir(os.path.join(root, *parts))
        except OSError:
            return
        parts.pop()


def tmp_path_for(target: str) -> str:
    return os.path.join(os.path.dirname(target), f"{TMP_PREFIX}{os.urandom(6).hex()}")
