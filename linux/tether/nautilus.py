"""Files (Nautilus) scripts: right-click → Scripts → Send to <device>, one per paired device."""

import os
import re
from pathlib import Path

MARK = "# tether-managed"
TEMPLATE = """#!/bin/sh
{mark}: sends the selected files to {name}. Tether rewrites this file; edits are lost.
t="$HOME/.local/bin/tether"; [ -x "$t" ] || t=tether
exec "$t" send --notify --device {id} "$@"
"""
# What older versions installed as the one “Send to Phone” script.
LEGACY = "Send to Phone"


def ours(p: Path) -> bool:
    try:
        text = p.read_text(errors="replace")
    except OSError:
        return False
    if MARK in text:
        return True
    return p.name == LEGACY and 'tether" send --notify "$@"' in text


def script_name(name: str) -> str:
    return "Send to " + (re.sub(r"[/\x00-\x1f]", "_", name).strip() or "device")


def sync(folder: Path, devices: list[tuple[str, str]]) -> bool:
    """Makes Tether's scripts in `folder` exactly one per (id, name); leaves other scripts alone."""
    names = [n for _, n in devices]
    wanted = {}
    for pid, name in devices:
        shown = name if names.count(name) == 1 else f"{name} ({pid[:6]})"
        wanted[script_name(shown)] = TEMPLATE.format(mark=MARK, name=shown, id=pid)
    changed = False
    if folder.is_dir():
        for p in folder.iterdir():
            if p.is_file() and p.name not in wanted and ours(p):
                p.unlink()
                changed = True
    for fname, body in wanted.items():
        p = folder / fname
        if p.exists() and not ours(p):
            continue  # the user's own script of that name wins
        try:
            current = p.read_text() if p.exists() else None
        except OSError:
            current = None
        if current != body:
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / f".{fname}.tether-tmp"
            tmp.write_text(body)
            os.chmod(tmp, 0o755)
            tmp.replace(p)
            changed = True
    return changed
