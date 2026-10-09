import errno


class ProtoError(Exception):
    """An error that travels over the wire as an error code."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


_ERRNO = {
    errno.ENOENT: "not_found",
    errno.EACCES: "denied",
    errno.EPERM: "denied",
    errno.EROFS: "denied",
    errno.EEXIST: "exists",
    errno.ENOTEMPTY: "exists",
    errno.ENOTDIR: "not_dir",
    errno.EISDIR: "is_dir",
}


def from_oserror(e: OSError) -> ProtoError:
    return ProtoError(_ERRNO.get(e.errno, "io"), str(e))
