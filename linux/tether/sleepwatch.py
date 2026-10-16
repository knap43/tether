"""Notices the computer going to sleep and waking, through logind (driven with gdbus).

A delay inhibitor (systemd-inhibit --mode=delay) holds sleep back for the moment it
takes to tell connected devices; logind caps the delay (InhibitDelayMaxSec, 5 s).
"""

import asyncio
import logging
import shutil

log = logging.getLogger("tether.sleep")

MONITOR = ["gdbus", "monitor", "--system", "--dest", "org.freedesktop.login1",
           "--object-path", "/org/freedesktop/login1"]
INHIBIT = ["systemd-inhibit", "--what=sleep", "--mode=delay", "--who=Tether",
           "--why=Telling your devices this computer is going to sleep", "sleep", "infinity"]


def parse_prepare(line: str):
    """True (going to sleep) or False (woke up) for a PrepareForSleep line, else None."""
    if ".PrepareForSleep (" not in line:
        return None
    arg = line.rsplit("(", 1)[1].strip().lower()
    return True if arg.startswith("true") else False if arg.startswith("false") else None


class SleepWatch:
    def __init__(self, on_sleep, on_wake):
        self.on_sleep, self.on_wake = on_sleep, on_wake
        self.available = bool(shutil.which("gdbus"))
        self.inhibitor = None

    async def _hold(self) -> None:
        if self.inhibitor is None and shutil.which("systemd-inhibit"):
            try:
                self.inhibitor = await asyncio.create_subprocess_exec(
                    *INHIBIT, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
            except OSError as e:
                log.info("no sleep inhibitor: %s", e)

    def _release(self) -> None:
        if self.inhibitor is not None and self.inhibitor.returncode is None:
            self.inhibitor.terminate()
        self.inhibitor = None

    async def watch(self) -> None:
        if not self.available:
            return
        while True:
            await self._hold()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *MONITOR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    stdin=asyncio.subprocess.DEVNULL)
            except OSError:
                self._release()
                return
            try:
                while line := await proc.stdout.readline():
                    state = parse_prepare(line.decode(errors="replace"))
                    if state is True:
                        try:
                            await asyncio.wait_for(self.on_sleep(), 3)
                        except Exception as e:  # never hold sleep up
                            log.warning("before sleep: %r", e)
                        self._release()
                    elif state is False:
                        await self._hold()
                        await self.on_wake()
            finally:
                if proc.returncode is None:
                    proc.kill()
                self._release()
            await asyncio.sleep(5)
