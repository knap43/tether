"""Wake-on-LAN on macOS: which network ports can wake this Mac, and switching it on.

The phone keeps the MAC and broadcast address of each port so it can send a
magic packet while the Mac sleeps. macOS calls the setting “Wake for network
access” (pmset `womp`); it applies to the whole machine and wakes it from
sleep, not from shut down.
"""

import ipaddress
import re
import subprocess

from .macos import osascript

VIRTUAL = re.compile(r"^(lo|gif|stf|anpi|ap|awdl|llw|utun|bridge|vmenet|vnic|feth|ipsec|ppp)")


def _run(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", mac))
    if len(raw) != 6:
        raise ValueError(f"bad MAC address {mac!r}")
    return b"\xff" * 6 + raw * 16


def parse_hardware_ports(out: str) -> dict[str, str]:
    """`networksetup -listallhardwareports` → {device: port name}."""
    ports, name = {}, None
    for line in out.splitlines():
        if line.startswith("Hardware Port:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and name:
            ports[line.split(":", 1)[1].strip()] = name
            name = None
    return ports


def parse_ifconfig(out: str) -> dict[str, dict]:
    """`ifconfig` → {ifname: {mac, ip, netmask, broadcast, active}} for interfaces with an IPv4 address."""
    ifs: dict[str, dict] = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"^([a-zA-Z0-9]+): flags=", line)
        if m:
            cur = ifs.setdefault(m.group(1), {})
            continue
        if cur is None:
            continue
        line = line.strip()
        if line.startswith("ether "):
            cur["mac"] = line.split()[1]
        elif line.startswith("inet ") and "ip" not in cur:
            f = line.split()
            cur["ip"] = f[1]
            if "netmask" in f:
                mask = f[f.index("netmask") + 1]
                cur["netmask"] = str(ipaddress.IPv4Address(int(mask, 16))) if mask.startswith("0x") else mask
            if "broadcast" in f:
                cur["broadcast"] = f[f.index("broadcast") + 1]
        elif line.startswith("status:"):
            cur["active"] = line.split(":", 1)[1].strip() == "active"
    return {k: v for k, v in ifs.items() if v.get("ip") and v.get("mac")}


def parse_womp(out: str) -> bool | None:
    """`pmset -g` → whether “Wake for network access” is on."""
    m = re.search(r"^\s*womp\s+(\d)", out, re.M)
    return m.group(1) == "1" if m else None


def interfaces(with_status: bool = True) -> list[dict]:
    """Physical ports with an IPv4 address: name, MAC, broadcast, wired, wake state."""
    ports = parse_hardware_ports(_run("networksetup", "-listallhardwareports"))
    womp = parse_womp(_run("pmset", "-g")) if with_status else None
    out = []
    for name, i in parse_ifconfig(_run("ifconfig")).items():
        if VIRTUAL.match(name) or (ports and name not in ports) or i["mac"] == "00:00:00:00:00:00":
            continue
        broadcast = i.get("broadcast")
        if not broadcast and i.get("netmask"):
            broadcast = str(ipaddress.IPv4Network(f"{i['ip']}/{i['netmask']}", strict=False).broadcast_address)
        port = ports.get(name, name)
        wired = port not in ("Wi-Fi", "AirPort")
        entry = {"ifname": name, "port": port, "mac": i["mac"], "ip": i["ip"],
                 "broadcast": broadcast or "255.255.255.255", "wired": wired}
        if with_status:
            entry.update(connection=port, enabled=womp)
        out.append(entry)
    out.sort(key=lambda e: not e["wired"])  # wired first: Wi-Fi wake depends on the Mac
    return out


def broadcast_addresses() -> list[str]:
    """Directed broadcast address of each physical network; macOS sends 255.255.255.255 out of one only."""
    return sorted({i["broadcast"] for i in interfaces(False) if i["broadcast"] != "255.255.255.255"})


def enable(_ifname: str = "") -> str:
    """Turns on “Wake for network access”; macOS asks for an administrator password."""
    r = osascript('do shell script "/usr/bin/pmset -a womp 1" with administrator privileges')
    if r.returncode != 0:
        raise RuntimeError("cancelled" if "-128" in r.stderr else (r.stderr.strip() or "pmset failed"))
    return "Wake for network access"
