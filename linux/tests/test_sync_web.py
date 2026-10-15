"""Folder sync among three devices, both fully linked (a mesh) and in a chain (A — B — C).

Every device must end with the same files: additions, edits and deletions spread
to all, and concurrent edits leave exactly one conflict copy everywhere.

Run:  python3 tests/test_sync_web.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_e2e import make_node, pair, until  # noqa: E402


def snapshot(d: Path) -> dict:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*"))
            if p.is_file() and not p.name.startswith(".tether")}


async def converged(dirs, what, timeout=25):
    await until(lambda: all(snapshot(d) == snapshot(dirs[0]) for d in dirs[1:]), timeout, what)
    return snapshot(dirs[0])


def write(p: Path, data: str, mtime: float | None = None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data)
    if mtime:
        os.utime(p, (mtime, mtime))


async def scenario(root: Path, mesh: bool, base_port: int):
    names = ["A", "B", "C"]
    dirs = [root / f"{n}_sync" for n in names]
    for d in dirs:
        d.mkdir()
    write(dirs[0] / "from_a.txt", "a")
    write(dirs[1] / "from_b.txt", "b")
    write(dirs[2] / "deep" / "from_c.txt", "c")
    types = ["desktop", "desktop", "phone"]
    nodes = [make_node(root, n, base_port + 100 * i,
                       {"device_type": types[i], "sync": [{"id": "docs", "path": str(dirs[i])}],
                        "accept_introductions": mesh})
             for i, n in enumerate(names)]
    for n in nodes:
        await n.start(with_dav=False, with_discovery=False)
    A, B, C = nodes
    # B is the hub: paired with both. In the mesh it also introduces A and C.
    await pair(B, A, base_port)
    await pair(B, C, base_port + 200)
    if mesh:
        await until(lambda: A.is_connected(C.identity.id), timeout=15, what="A–C introduced")
    else:
        await asyncio.sleep(1)
        assert not A.is_connected(C.identity.id)
    label = "mesh " if mesh else "chain"

    files = await converged(dirs, f"{label}: initial merge")
    assert set(files) == {"from_a.txt", "from_b.txt", "deep/from_c.txt"}, files
    print(f"ok  {label}: three folders merge")

    write(dirs[2] / "from_a.txt", "edited on C")
    files = await converged(dirs, f"{label}: edit spreads")
    assert files["from_a.txt"] == b"edited on C"
    print(f"ok  {label}: an edit reaches every device")

    (dirs[0] / "from_b.txt").unlink()
    files = await converged(dirs, f"{label}: deletion spreads")
    assert "from_b.txt" not in files, files
    print(f"ok  {label}: a deletion reaches every device")

    # Concurrent edits on the two ends while they can't see each other.
    await apart(nodes)
    t = time.time()
    write(dirs[0] / "deep" / "from_c.txt", "A's version", t - 60)
    write(dirs[2] / "deep" / "from_c.txt", "C's version", t - 30)
    for n in nodes:
        await n.rescan()
    pairs = [(B, A, base_port), (B, C, base_port + 200)] + ([(A, C, base_port + 200)] if mesh else [])
    for x, y, _ in pairs:
        await reconnect(nodes, base_port, x, y)
    files = await converged(dirs, f"{label}: conflict settles", timeout=40)
    conflicts = [f for f in files if ".conflict-" in f]
    assert files["deep/from_c.txt"] == b"C's version", files
    assert len(conflicts) == 1 and files[conflicts[0]] == b"A's version", conflicts
    print(f"ok  {label}: concurrent edits leave one newer file and one conflict copy, everywhere")

    # All three edit one file while apart, then come back one link at a time, a couple of
    # seconds apart, so several devices settle the same conflicts at different moments.
    await apart(nodes)
    t = time.time()
    for i, d in enumerate(dirs):
        write(d / "notes.md", f"version {i}", t - 90 + 30 * i)
    for n in nodes:
        await n.rescan()
    for x, y, port in reversed(pairs):
        await reconnect(nodes, base_port, x, y)
        await asyncio.sleep(2.2)
    files = await converged(dirs, f"{label}: three-way conflict settles", timeout=60)
    copies = sorted(v for f, v in files.items() if f.startswith("notes.conflict-"))
    assert files["notes.md"] == b"version 2", files.get("notes.md")
    assert copies == [b"version 0", b"version 1"], [f for f in files if f.startswith("notes")]
    print(f"ok  {label}: a three-way conflict leaves exactly one copy of each losing version")

    await apart(nodes)
    for n in nodes:
        for t in list(n.tasks):
            t.cancel()


async def apart(nodes):
    for n in nodes:
        for s in n.connected():
            s.channel.close()
        n.server.close()
    await until(lambda: not any(n.connected() for n in nodes), what="all apart")


async def reconnect(nodes, base_port, x, y):
    for n in (x, y):
        i = nodes.index(n)
        if not n.server.is_serving():
            n.server = await asyncio.start_server(n._accept, "0.0.0.0", base_port + 100 * i)
    lo, hi = (x, y) if x.identity.id < y.identity.id else (y, x)
    await lo._try_connect(hi.identity.id, "127.0.0.1", base_port + 100 * nodes.index(hi))
    await until(lambda: x.is_connected(y.identity.id), what="reconnect")


async def main(root: Path):
    await scenario(root / "mesh", True, 50291)
    await scenario(root / "chain", False, 51291)
    print("ALL PASSED")


if __name__ == "__main__":
    root = Path(tempfile.mkdtemp(prefix="tether-syncweb-"))
    (root / "mesh").mkdir()
    (root / "chain").mkdir()
    try:
        asyncio.run(asyncio.wait_for(main(root), 300))
    finally:
        shutil.rmtree(root, ignore_errors=True)
