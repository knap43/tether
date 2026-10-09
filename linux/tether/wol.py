"""Wake-on-LAN: which network cards can wake this computer, and switching it on.

The phone keeps the MAC and broadcast address of each physical interface so it
can send a magic packet while this computer sleeps or is powered off.
"""

import fcntl
import ipaddress
import os
import re
import shutil
import socket
import struct
import subprocess

SYS = "/sys/class/net"
SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
VIRTUAL = re.compile(r"^(lo|docker|br-|veth|virbr|vnet|tun|tap|wg|tailscale|zt|vmnet|vboxnet)")


def _ioctl_ip(ifname: str, req: int) -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            packed = fcntl.ioctl(s.fileno(), req, struct.pack("256s", ifname.encode()[:15]))
        except OSError:
            return None
    return socket.inet_ntoa(packed[20:24])


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", mac))
    if len(raw) != 6:
        raise ValueError(f"bad MAC address {mac!r}")
    return b"\xff" * 6 + raw * 16


def nm_status(ifname: str, wireless: bool) -> tuple[str | None, bool | None]:
    """(NetworkManager connection, whether it has magic-packet wake enabled)."""
    if not shutil.which("nmcli"):
        return None, None
    try:
        conn = subprocess.run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", ifname],
                              capture_output=True, text=True, timeout=5).stdout.strip()
        if not conn:
            return None, None
        key = "802-11-wireless.wake-on-wlan" if wireless else "802-3-ethernet.wake-on-lan"
        val = subprocess.run(["nmcli", "-g", key, "connection", "show", conn],
                             capture_output=True, text=True, timeout=5).stdout.strip().lower()
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    return conn, ("magic" in val) if val else None


def ethtool_status(ifname: str) -> bool | None:
    if not shutil.which("ethtool"):
        return None
    try:
        out = subprocess.run(["ethtool", ifname], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"^\s*Wake-on:\s*(\S+)", out, re.M)
    return ("g" in m.group(1)) if m else None


def interfaces(with_status: bool = True) -> list[dict]:
    """Physical interfaces with an IPv4 address: name, MAC, broadcast, wired, WoL state."""
    out = []
    try:
        names = sorted(os.listdir(SYS))
    except OSError:
        return out
    for name in names:
        if VIRTUAL.match(name) or not os.path.exists(f"{SYS}/{name}/device"):
            continue
        if _read(f"{SYS}/{name}/type") != "1":  # ARPHRD_ETHER (Wi-Fi reports this too)
            continue
        mac = _read(f"{SYS}/{name}/address")
        ip, mask = _ioctl_ip(name, SIOCGIFADDR), _ioctl_ip(name, SIOCGIFNETMASK)
        if not mac or mac == "00:00:00:00:00:00" or not ip or not mask:
            continue
        net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
        wireless = os.path.isdir(f"{SYS}/{name}/wireless")
        entry = {"ifname": name, "mac": mac, "ip": ip, "broadcast": str(net.broadcast_address),
                 "wired": not wireless}
        if with_status:
            conn, enabled = nm_status(name, wireless)
            if enabled is None and not wireless:
                enabled = ethtool_status(name)
            entry.update(connection=conn, enabled=enabled)
        out.append(entry)
    out.sort(key=lambda e: not e["wired"])  # wired first: Wi-Fi wake is rarely supported
    return out


def enable(ifname: str) -> str:
    """Turns on magic-packet wake for the interface's NetworkManager connection."""
    wireless = os.path.isdir(f"{SYS}/{ifname}/wireless")
    conn, _ = nm_status(ifname, wireless)
    if not conn:
        raise RuntimeError(f"{ifname} isn't managed by NetworkManager; use: sudo ethtool -s {ifname} wol g")
    key = "802-11-wireless.wake-on-wlan" if wireless else "802-3-ethernet.wake-on-lan"
    r = subprocess.run(["nmcli", "connection", "modify", conn, key, "magic"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "nmcli failed")
    subprocess.run(["nmcli", "device", "reapply", ifname], capture_output=True)
    return conn
