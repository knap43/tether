import asyncio, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from tether.media import Media
async def main():
    pushes = []
    m = Media(lambda st: pushes.append(st))
    watcher = asyncio.ensure_future(m.watch())
    st = await m.state(); p = st["players"][0]
    print("STATE", p["name"], p["status"], p["title"], p["artist"], p["length"], p["position"])
    st = await m.command(p["name"], "play_pause"); print("AFTER PP", st["players"][0]["status"])
    st = await m.command(None, "next"); print("AFTER NEXT", st["players"][0]["title"])
    await m.command(p["name"], "seek", 10); await m.command(p["name"], "position", 5000)
    await asyncio.sleep(1)
    print("PUSHES", len(pushes) > 0)
    watcher.cancel()
asyncio.run(main())
