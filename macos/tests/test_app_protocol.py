"""The control-socket protocol the menu-bar app relies on, against a real daemon.

Run:  python3 tests/test_app_protocol.py
"""

import asyncio
import base64
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tether.config import Paths  # noqa: E402
from tether.control import ControlServer  # noqa: E402
from tether.node import Node  # noqa: E402


class Line:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    async def send(self, obj):
        self.writer.write(json.dumps(obj).encode() + b"\n")
        await self.writer.drain()

    async def next(self, ev=None, timeout=5):
        while True:
            msg = json.loads(await asyncio.wait_for(self.reader.readline(), timeout))
            if ev is None or msg.get("ev") == ev:
                return msg


async def main(root: Path):
    paths = Paths(str(root))
    paths.config_file.write_text(json.dumps({"name": "Mac", "port": 49291, "dav_port": 49292,
                                             "discovery_port": 49290}))
    node = Node(paths)
    await node.start(with_dav=False, with_discovery=False)
    await ControlServer(node).start()

    app = Line(*await asyncio.open_unix_connection(str(paths.socket), limit=32 * 1024 * 1024))
    await app.send({"cmd": "subscribe", "app": True})
    st = await app.next("status")
    assert st["name"] == "Mac" and st["clipboard_backend"] == "app" and not st["media"], st
    assert st["dav_url"].startswith("http://127.0.0.1:49292/") and st["mount_path"].endswith("/Phone"), st
    print("ok  subscribe as the app")

    # clipboard: the daemon asks the app to set it; the app reports local copies
    await node.clip.set("from the phone")
    assert (await app.next("clip_set"))["text"] == "from the phone"
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40
    await node.clip.set_image("image/png", png)
    ev = await app.next("clip_image_set")
    assert ev["mime"] == "image/png" and base64.b64decode(ev["data"]) == png
    seen = []
    node.on_local_clip = lambda text: seen.append(text)
    node.on_local_clip_image = lambda mime, data: seen.append((mime, data))
    await app.send({"cmd": "clip_local", "text": "copied on the Mac"})
    await app.send({"cmd": "clip_local_image", "mime": "image/png", "data": base64.b64encode(png).decode()})
    for _ in range(50):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.05)
    assert seen == ["copied on the Mac", ("image/png", png)], seen
    print("ok  clipboard text and images")

    # notifications: posted by the app, replies and dismissals reported back
    nid = await node.desk.notify("Signal · Pixel", "Ana", "see you at 8", "/tmp/icon.png", [("reply", "Reply")])
    ev = await app.next("desk_notify")
    assert ev["nid"] == nid and ev["actions"] == [{"key": "reply", "label": "Reply"}] and ev["icon"] == "/tmp/icon.png"
    again = await node.desk.notify("Signal · Pixel", "Ana", "on second thoughts, 9", replaces=nid)
    assert again == nid and (await app.next("desk_notify"))["body"] == "on second thoughts, 9"
    await node.desk.close(nid)
    assert (await app.next("desk_close"))["nid"] == nid
    acts, closes = [], []
    node._on_notif_action = lambda n, a, t=None: acts.append((n, a, t))
    node._on_notif_closed = lambda n, r: closes.append((n, r))
    await app.send({"cmd": "desk_action", "nid": nid, "action": "reply", "text": "ok!"})
    await app.send({"cmd": "desk_closed", "nid": nid, "reason": 2})
    for _ in range(50):
        if acts and closes:
            break
        await asyncio.sleep(0.05)
    assert acts == [(nid, "reply", "ok!")] and closes == [(nid, 2)], (acts, closes)
    node.notify("Paired", "Paired with Pixel")
    assert (await app.next("notify"))["title"] == "Paired"
    print("ok  notifications (post, replace, close, reply, dismiss)")

    # commands on their own connection, as the app does
    cli = Line(*await asyncio.open_unix_connection(str(paths.socket)))
    await cli.send({"cmd": "set", "key": "remote_shell", "value": True, "id": 7})
    res = await cli.next()
    assert res == {"ok": True, "rid": 7}, res
    await cli.send({"cmd": "set", "key": "media", "value": True})
    assert not (await cli.next())["ok"], "media can't be switched on"
    await cli.send({"cmd": "command_add", "name": "Lock screen", "command": "pmset displaysleepnow"})
    assert (await cli.next())["ok"]
    await cli.send({"cmd": "status"})
    st = await cli.next()
    assert st["ok"] and st["remote_shell"] and st["commands"][0]["id"] == "lock-screen", st
    print("ok  commands")

    # once the app goes away the daemon stops routing to it
    app.writer.close()
    for _ in range(50):
        if not node.clip.clients and not node.desk.clients:
            break
        await asyncio.sleep(0.05)
    assert not node.clip.clients and not node.desk.clients
    assert node.status()["clipboard_backend"] in ("pbcopy", "none")
    print("ok  app disconnect")
    print("ALL PASSED")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        asyncio.run(asyncio.wait_for(main(Path(d)), 60))
