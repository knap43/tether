import asyncio, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from tether.desknotify import DesktopNotifier
async def main():
    got = []
    d = DesktopNotifier(lambda n, a: got.append(("action", n, a)), lambda n, r: got.append(("closed", n, r)))
    watcher = asyncio.ensure_future(d.watch()); await asyncio.sleep(0.5)
    nid = await d.notify("Signal · Pixel 8", "Ana \"quoted\"", "see you\nat 8 — ok", "/tmp/x.png", [("reply", "Reply")])
    print("NID", nid)
    for _ in range(30):
        if len(got) >= 2: break
        await asyncio.sleep(0.1)
    print("SIGNALS", got)
    await d.close(nid); await asyncio.sleep(0.3)
    print("EMPTY-ACTIONS", await d.notify("A", "t", "b"))
    watcher.cancel()
asyncio.run(main())
