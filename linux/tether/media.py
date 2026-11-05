"""The computer's output volume (PipeWire via wpctl)."""

import asyncio
import re
import shutil


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
    def __init__(self):
        self.has_wpctl = bool(shutil.which("wpctl"))

    async def get_volume(self) -> float | None:
        """The default output's volume (0–1), or None without wpctl."""
        if not self.has_wpctl:
            return None
        code, out = await _run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
        v = parse_volume(out) if code == 0 else None
        return v["level"] if v else None

    async def set_volume(self, level: float) -> None:
        """Sets the default output's volume (0–1), unmuting it."""
        level = max(0.0, min(1.0, float(level)))
        await _run("wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", f"{level:.3f}")
        await _run("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0")
