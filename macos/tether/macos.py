"""Small macOS helpers: AppleScript, notifications without the app, Finder mounts."""

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger("tether.macos")


def as_string(s: str) -> str:
    """An AppleScript string literal."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def osascript(script: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)


def notify(title: str, body: str, subtitle: str = "") -> None:
    """A plain banner, used when the Tether app isn't running (it has no buttons and can't be closed later)."""
    if not shutil.which("osascript"):
        return
    script = f"display notification {as_string(body)} with title {as_string(title)}"
    if subtitle:
        script += f" subtitle {as_string(subtitle)}"
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def pick_files(prompt: str = "Send to phone") -> list[str]:
    script = (
        f"set picked to choose file with prompt {as_string(prompt)} with multiple selections allowed\n"
        "set out to \"\"\n"
        "repeat with f in picked\n"
        "  set out to out & POSIX path of f & linefeed\n"
        "end repeat\n"
        "return out"
    )
    r = osascript(script)
    return [line for line in r.stdout.splitlines() if line] if r.returncode == 0 else []


def open_path(target: str) -> None:
    subprocess.Popen(["open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def mounted(mountpoint: Path) -> bool:
    return os.path.ismount(mountpoint)


def mount(url: str, mountpoint: Path, volume: str = "Phone") -> Path:
    """Mounts the WebDAV bridge in Finder (it shows under Locations). Idempotent."""
    if mounted(mountpoint):
        return mountpoint
    mountpoint.mkdir(parents=True, exist_ok=True)
    if not shutil.which("mount_webdav") and not os.path.exists("/sbin/mount_webdav"):
        raise RuntimeError("mount_webdav is missing")
    cmd = shutil.which("mount_webdav") or "/sbin/mount_webdav"
    r = subprocess.run([cmd, "-S", "-v", volume, url, str(mountpoint)], capture_output=True, text=True, timeout=30)
    if r.returncode != 0 and not mounted(mountpoint):
        raise RuntimeError(r.stderr.strip() or f"mount_webdav exited with {r.returncode}")
    for _ in range(20):  # mount_webdav returns before Finder sees the volume
        if mounted(mountpoint):
            break
        time.sleep(0.1)
    return mountpoint


def unmount(mountpoint: Path) -> None:
    if mounted(mountpoint):
        subprocess.run(["umount", str(mountpoint)], capture_output=True, timeout=30)
