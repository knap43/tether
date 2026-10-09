"""Shared folders exposed for live browsing: /<share>/<relative path>."""

import os
import shutil
import stat as stat_mod
import tempfile

from .errors import ProtoError, from_oserror

TMP_PREFIX = ".tether-tmp-"


def split_path(vpath) -> list[str]:
    if not isinstance(vpath, str) or "\0" in vpath:
        raise ProtoError("bad_request")
    parts = [p for p in vpath.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ProtoError("denied")
    return parts


def within(root: str, path: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def entry_for(name: str, st: os.stat_result) -> dict:
    is_dir = stat_mod.S_ISDIR(st.st_mode)
    return {"name": name, "dir": is_dir, "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime_ns // 1_000_000}


class Shares:
    def __init__(self, shares: dict[str, str]):
        self.roots = {n: os.path.realpath(os.path.expanduser(p)) for n, p in shares.items() if "/" not in n and n}

    def resolve(self, vpath, must_exist: bool = True, follow: bool = True) -> str | None:
        """Real path for a virtual path, or None for the virtual root.

        With follow=False the last component is not dereferenced, so operations
        act on a symlink itself rather than on its target.
        """
        parts = split_path(vpath)
        if not parts:
            return None
        root = self.roots.get(parts[0])
        if root is None:
            raise ProtoError("not_found")
        if follow or len(parts) == 1:
            real = os.path.realpath(os.path.join(root, *parts[1:]))
        else:
            real = os.path.join(os.path.realpath(os.path.join(root, *parts[1:-1])), parts[-1])
        if not within(root, real):
            raise ProtoError("denied")
        if must_exist and not os.path.lexists(real):
            raise ProtoError("not_found")
        return real

    def _is_root(self, real: str) -> bool:
        return real in self.roots.values()

    def list(self, vpath) -> list[dict]:
        real = self.resolve(vpath)
        if real is None:
            out = []
            for name, root in self.roots.items():
                try:
                    out.append({**entry_for(name, os.stat(root)), "name": name})
                except OSError:
                    pass
            return out
        try:
            if not os.path.isdir(real):
                raise ProtoError("not_dir")
            out = []
            with os.scandir(real) as it:
                for de in it:
                    if de.name.startswith(TMP_PREFIX):
                        continue
                    try:
                        out.append(entry_for(de.name, de.stat()))
                    except OSError:
                        continue
            return out
        except OSError as e:
            raise from_oserror(e) from None

    def stat(self, vpath) -> dict:
        real = self.resolve(vpath)
        if real is None:
            return {"name": "", "dir": True, "size": 0, "mtime": 0}
        try:
            return entry_for(os.path.basename(real), os.stat(real))
        except OSError as e:
            raise from_oserror(e) from None

    def open_read(self, vpath, offset: int, length: int):
        """Returns (file object positioned at offset, total size, bytes to send)."""
        real = self.resolve(vpath)
        if real is None:
            raise ProtoError("is_dir")
        try:
            f = open(real, "rb")
        except OSError as e:
            raise from_oserror(e) from None
        size = os.fstat(f.fileno()).st_size
        if stat_mod.S_ISDIR(os.fstat(f.fileno()).st_mode):
            f.close()
            raise ProtoError("is_dir")
        offset = max(0, min(int(offset or 0), size))
        length = size - offset if length is None or length < 0 else min(int(length), size - offset)
        f.seek(offset)
        return f, size, length

    def begin_write(self, vpath) -> tuple[str, str]:
        """Returns (temporary path, final path) for an atomic write."""
        real = self.resolve(vpath, must_exist=False, follow=False)
        if real is None or self._is_root(real):
            raise ProtoError("denied")
        parent = os.path.dirname(real)
        if not os.path.isdir(parent):
            raise ProtoError("not_found")
        if os.path.isdir(real):
            raise ProtoError("is_dir")
        try:
            fd, tmp = tempfile.mkstemp(dir=parent, prefix=TMP_PREFIX)
            os.close(fd)
        except OSError as e:
            raise from_oserror(e) from None
        return tmp, real

    def mkdir(self, vpath) -> None:
        real = self.resolve(vpath, must_exist=False, follow=False)
        if real is None:
            raise ProtoError("denied")
        try:
            os.mkdir(real)
        except OSError as e:
            raise from_oserror(e) from None

    def delete(self, vpath) -> None:
        real = self.resolve(vpath, follow=False)
        if real is None or self._is_root(real):
            raise ProtoError("denied")
        try:
            if os.path.isdir(real) and not os.path.islink(real):
                shutil.rmtree(real)
            else:
                os.remove(real)
        except OSError as e:
            raise from_oserror(e) from None

    def move(self, src, dst, overwrite: bool) -> None:
        s = self.resolve(src, follow=False)
        d = self.resolve(dst, must_exist=False, follow=False)
        if s is None or d is None or self._is_root(s) or self._is_root(d):
            raise ProtoError("denied")
        if within(s, d):
            raise ProtoError("bad_request")
        try:
            if os.path.lexists(d):
                if not overwrite:
                    raise ProtoError("exists")
                if os.path.isdir(d) and not os.path.islink(d):
                    shutil.rmtree(d)
            os.replace(s, d)
        except OSError as e:
            raise from_oserror(e) from None
