"""Three daemons on localhost: a phone paired with the PC, a laptop paired with the PC.

The PC introduces them; they must then link directly with every feature, and an
unpairing must not be undone by the next introduction.

Run:  python3 tests/test_web.py
"""

import asyncio
import os
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

    lap_share = root / "lap_share"
    lap_share.mkdir()
    (lap_share / "thesis.odt").write_text("from the laptop")
    PC = make_node(root, "PC", 49291, {})
    LAP = make_node(root, "LAP", 49391, {"shares": {"Home": str(lap_share)}})
    PH = make_node(root, "PH", 49491, {"device_type": "phone", "shares": {"Internal": str(ph_share)}})
    await PC.start(with_dav=True, with_discovery=False)
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

    # --- the laptop's files, from the phone and from the PC
    names = [e["name"] for e in (await PH.sessions[lap].channel.request({"t": "fs_list", "path": "/Home"}))["entries"]]
    assert names == ["thesis.odt"], names
    d = next(d for d in PC.status()["devices"] if d["id"] == lap)
    assert d["dav_url"] and d["dav_url"].endswith("/LAP/"), d
    http_url = d["dav_url"].replace("dav://", "http://")
    async with aiohttp.ClientSession() as http:
        r = await http.request("PROPFIND", http_url + "Home/", headers={"Depth": "1"})
        assert r.status == 207 and "thesis.odt" in await r.text()
        r = await http.get(http_url + "Home/thesis.odt")
        assert await r.text() == "from the laptop"
        r = await http.put(http_url + "Home/from-pc.txt", data=b"pc")
        assert r.status == 201 and (lap_share / "from-pc.txt").read_bytes() == b"pc"
    print("ok  the phone and the PC browse the laptop")

    # --- the Files sidebar on the PC: nothing unasked, the laptop on request, renames followed,
    #     and a bookmark the user made by hand is never Tether's to move or remove
    bm = PC.paths.bookmarks_file
    root_url = PC.dav_root()

    def tether_lines():
        return [l for l in bm.read_text().splitlines() if l.startswith(root_url)] if bm.exists() else []

    assert tether_lines() == [], tether_lines()
    mine = f"{root_url}PH/Internal/ My phone"
    bm.write_text("file:///home/me/Music Music\n" + mine + "\n" + "dav://127.0.0.1:47292/oldtoken/Phone/ Phone (Tether)\n")
    PC.set_sidebar(lap, True)
    lines = bm.read_text().splitlines()
    assert lines == ["file:///home/me/Music Music", mine, f"{root_url}LAP/ LAP"], lines
    LAP.cfg["name"] = "ThinkPad"
    PC.sessions[lap].channel.close()
    await until(lambda: not PC.is_connected(lap) and not LAP.is_connected(PC.identity.id), what="laptop drop")
    lo, hi = (PC, LAP) if PC.identity.id < lap else (LAP, PC)
    await lo._try_connect(hi.identity.id, "127.0.0.1", 49391 if hi is LAP else 49291)
    await until(lambda: f"{root_url}ThinkPad/ ThinkPad" in tether_lines(), what="sidebar follows a rename")
    assert mine in bm.read_text().splitlines() and f"{root_url}LAP/ LAP" not in tether_lines()
    async with aiohttp.ClientSession() as http:
        r = await http.request("PROPFIND", f"{root_url}ThinkPad/".replace("dav://", "http://"), headers={"Depth": "1"})
        assert r.status == 207 and "Home" in await r.text()
    assert [d["sidebar"] for d in PC.status()["devices"] if d["id"] == lap] == [True]
    PC.set_sidebar(lap, False)
    assert bm.read_text().splitlines() == ["file:///home/me/Music Music", mine], bm.read_text()

    # --- Files scripts: one “Send to …” per paired device, following renames; others left alone
    sd = PC.paths.scripts_dir
    (sd / "My own script").write_text("#!/bin/sh\necho hi\n")
    (sd / "Send to Phone").write_text('#!/bin/sh\nexec "$HOME/.local/bin/tether" send --notify "$@"\n')
    PC.sync_desktop()
    assert sorted(p.name for p in sd.iterdir()) == ["My own script", "Send to PH", "Send to ThinkPad"], list(sd.iterdir())
    assert f"--device {lap}" in (sd / "Send to ThinkPad").read_text()
    assert os.access(sd / "Send to PH", os.X_OK)
    print("ok  Files sidebar opt-in and Files scripts per device; the user's own entries untouched")

    # --- the PC goes to sleep: the others hear about it at once, and it comes back on waking
    await PC.before_sleep()
    await until(lambda: not LAP.is_connected(PC.identity.id) and not PH.is_connected(PC.identity.id), what="sleep")
    assert [d["asleep"] for d in LAP.status()["devices"] if d["id"] == PC.identity.id] == [True]
    await PC.after_wake()
    if PC.identity.id > lap:
        await LAP._try_connect(PC.identity.id, "127.0.0.1", 49291)
    if PC.identity.id > ph:
        await PH._try_connect(PC.identity.id, "127.0.0.1", 49291)
    await until(lambda: PC.is_connected(lap) and PC.is_connected(ph), what="awake again")
    assert [d["asleep"] for d in LAP.status()["devices"] if d["id"] == PC.identity.id] == [False]
    print("ok  a sleeping computer is shown asleep, then reconnects on waking")

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
