"""Media players (MPRIS via playerctl) and system volume (PipeWire via wpctl)."""

import asyncio
import logging
import re
import shutil
import time

log = logging.getLogger("tether.media")

SEP = "\x1f"
FORMAT = SEP.join([
    "{{playerInstance}}", "{{playerName}}", "{{status}}", "{{xesam:title}}", "{{xesam:artist}}",
    "{{xesam:album}}", "{{mpris:length}}", "{{position}}",
])
ACTIONS = {"play_pause": "play-pause", "play": "play", "pause": "pause", "next": "next", "previous": "previous",
           "stop": "stop"}


def _int(s: str) -> int | None:
    try:
        return int(s)
    except ValueError:
        return None


def parse_players(out: str, now_ms: int | None = None) -> list[dict]:
    """Parses `playerctl -a metadata --format FORMAT` output."""
    now_ms = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    players = []
    for line in out.splitlines():
        f = line.split(SEP)
        if len(f) != 8 or not f[0]:
            continue
        length, pos = _int(f[6]), _int(f[7])
        players.append({
            "name": f[0],
            "identity": f[1].replace("_", " ").title() if f[1] else f[0],
            "status": f[2] or "Stopped",
            "title": f[3],
            "artist": f[4],
            "album": f[5],
            "length": length // 1000 if length else None,  # µs → ms
            "position": pos // 1000 if pos is not None else None,
            "at": now_ms,
        })
    players.sort(key=lambda p: (p["status"] != "Playing", p["status"] != "Paused"))
    return players


def parse_volume(out: str) -> dict | None:
    """Parses `wpctl get-volume` output, e.g. 'Volume: 0.45 [MUTED]'."""
    m = re.search(r"Volume:\s*([0-9.]+)", out)
    if not m:
        return None
    return {"level": float(m.group(1)), "muted": "MUTED" in out}


async def _run(*args: str, timeout: float = 5) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, ""
    return proc.returncode, out.decode("utf-8", "replace")


class Media:
    def __init__(self, on_change):
        self.on_change = on_change  # callable(state)
        self.available = bool(shutil.which("playerctl"))
        self.has_wpctl = bool(shutil.which("wpctl"))
        self.last: dict | None = None

    async def state(self) -> dict:
        players: list = []
        if self.available:
            code, out = await _run("playerctl", "-a", "metadata", "--format", FORMAT)
            if code == 0:
                players = parse_players(out)
        volume = None
        if self.has_wpctl:
            code, out = await _run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
            if code == 0:
                volume = parse_volume(out)
        return {"players": players, "volume": volume, "available": self.available}

    async def command(self, player: str | None, action: str, value=None) -> dict:
        if action == "volume":
            if not self.has_wpctl:
                raise ValueError("volume control needs wpctl (PipeWire)")
            level = max(0.0, min(1.0, float(value)))
            await _run("wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", f"{level:.3f}")
        elif action == "mute":
            await _run("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle")
        else:
            if not self.available:
                raise ValueError("media control needs playerctl")
            target = ["-p", player] if player else []
            if action == "seek":
                secs = float(value)
                arg = f"{abs(secs):g}{'+' if secs >= 0 else '-'}"
                await _run("playerctl", *target, "position", arg)
            elif action == "position":
                await _run("playerctl", *target, "position", f"{max(0.0, float(value)) / 1000:g}")
            elif action in ACTIONS:
                await _run("playerctl", *target, ACTIONS[action])
            else:
                raise ValueError(f"unknown action {action}")
        await asyncio.sleep(0.15)  # let the player settle before reading back
        return await self.refresh()

    async def refresh(self) -> dict:
        st = await self.state()
        if self._key(st) != self._key(self.last):
            self.last = st
            self.on_change(st)
        self.last = st
        return st

    @staticmethod
    def _key(st):
        if st is None:
            return None
        # Position moves constantly; only meaningful changes are pushed.
        return ([(p["name"], p["status"], p["title"], p["artist"], p["length"]) for p in st["players"]],
                st["volume"])

    async def watch(self) -> None:
        """Pushes state changes; playerctl --follow tells us when something changes."""
        if not self.available:
            return
        while True:
            proc = await asyncio.create_subprocess_exec(
                "playerctl", "-a", "--follow", "metadata", "--format", "{{playerInstance}}{{status}}{{xesam:title}}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL)
            try:
                while await proc.stdout.readline():
                    await asyncio.sleep(0.2)
                    await self.refresh()
            finally:
                if proc.returncode is None:
                    proc.kill()
            await asyncio.sleep(3)
