"""Three daemons on localhost: a phone paired with the PC, a laptop paired with the PC.

The PC introduces them; they must then link directly with every feature, and an
unpairing must not be undone by the next introduction.

Run:  python3 tests/test_web.py
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp  # noqa: E402

from test_e2e import make_node, pair, until  # noqa: E402


async def main(root: Path):
    ph_share = root / "ph_share"
    ph_share.mkdir()
    (ph_share / "photo.txt").write_text("from the phone")

    PC = make_node(root, "PC", 49291, {})
    LAP = make_node(root, "LAP", 49391, {})
    PH = make_node(root, "PH", 49491, {"device_type": "phone", "shares": {"Internal": str(ph_share)}})
    await PC.start(with_dav=False, with_discovery=False)
    await LAP.start(with_dav=True, with_discovery=False)
    await PH.start(with_dav=False, with_discovery=False)
    lap, ph = LAP.identity.id, PH.identity.id

    # --- the laptop's "Phone" shortcut must not open the PC
    await pair(PC, LAP, 49391)
    tok = LAP.cfg["dav_token"]
    async with aiohttp.ClientSession() as http:
        r = await http.request("PROPFIND", f"http://127.0.0.1:{LAP.cfg['dav_port']}/{tok}/Phone/", headers={"Depth": "1"})
        body = await r.text()
        assert r.status == 207 and "Home" not in body, body
    print("ok  Phone shortcut never falls back to a computer")
    await pair(PC, PH, 49491)

    # --- introduction: laptop and phone pair through the PC and connect directly
    await until(lambda: lap in PH.peers and ph in LAP.peers, what="introduced pairing")
    await until(lambda: LAP.is_connected(ph) and PH.is_connected(lap), what="direct session")
    assert PH.peers.get(lap)["key"] and not LAP.intros.pending and not PH.intros.pending
    print("ok  introduction")

    # --- full functionality on the direct link
    s = LAP.sessions[ph]
    names = [e["name"] for e in (await s.channel.request({"t": "fs_list", "path": "/Internal"}))["entries"]]
    assert names == ["photo.txt"], names
    async with aiohttp.ClientSession() as http:
        r = await http.get(f"http://127.0.0.1:{LAP.cfg['dav_port']}/{tok}/Phone/photo.txt")
        assert await r.text() == "from the phone"
    PH.on_local_clip("phone → laptop")
    await until(lambda: LAP.clip.value == "phone → laptop", what="clipboard")
    f = root / "doc.bin"
    f.write_bytes(b"x" * 100_000)
    await LAP.send_file(ph, str(f))
    print("ok  laptop browses, mounts, sends to and shares a clipboard with the phone")

    # --- reconnect after a drop, without the PC
    for sess in list(PC.connected()):
        sess.channel.close()
    PC.server.close()
    s.channel.close()
    await until(lambda: not LAP.is_connected(ph) and not PH.is_connected(lap), what="drop")
    lo, hi = (LAP, PH) if lap < ph else (PH, LAP)
    hi_port = 49491 if hi is PH else 49391
    await lo._try_connect(hi.identity.id, "127.0.0.1", hi_port)
    await until(lambda: LAP.is_connected(ph), what="reconnect without the PC")
    print("ok  the direct link stands without the PC")

    # --- unpairing sticks, even though the PC keeps introducing
    await LAP.unpair(ph)
    await until(lambda: lap not in PH.peers, what="unpair")
    assert ph in LAP.intros.blocked and lap in PH.intros.blocked
    PC.server = await asyncio.start_server(PC._accept, "0.0.0.0", 49291)
    await PC._try_connect(ph, "127.0.0.1", 49491)
    await PC._try_connect(lap, "127.0.0.1", 49391)
    await until(lambda: PC.is_connected(ph) and PC.is_connected(lap), what="PC back")
    await asyncio.sleep(1.5)
    assert lap not in PH.peers and ph not in LAP.peers and not LAP.is_connected(ph)
    print("ok  unpairing is not undone by introductions")

    # --- explicit pairing lifts the block
    await pair(LAP, PH, 49491)
    assert ph not in LAP.intros.blocked and lap not in PH.intros.blocked
    print("ok  explicit pairing lifts the block")
    print("ALL PASSED")


if __name__ == "__main__":
    root = Path(tempfile.mkdtemp(prefix="tether-web-"))
    try:
        asyncio.run(asyncio.wait_for(main(root), 60))
    finally:
        shutil.rmtree(root, ignore_errors=True)
