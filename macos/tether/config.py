import json
import os
import secrets
import socket
import tempfile
from pathlib import Path


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


SUPPORT = "~/Library/Application Support/Tether"


class Paths:
    """Everything lives under ~/Library/Application Support/Tether; the app finds the socket there too."""

    def __init__(self, base: str | None = None):
        b = Path(base or os.environ.get("TETHER_SUPPORT") or SUPPORT).expanduser()
        self.config_dir, self.data_dir, self.runtime_dir = b, b / "data", b / "run"
        for d in (self.config_dir, self.data_dir, self.runtime_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config_file = self.config_dir / "config.json"
        self.identity_file = self.data_dir / "identity.pem"
        self.peers_file = self.data_dir / "peers.json"
        self.sync_dir = self.data_dir / "sync"
        self.socket = self.runtime_dir / "ctl.sock"
        self.mount_dir = self.runtime_dir / "Phone"


DEFAULTS = {
    "name": None,
    "port": 47291,
    "discovery_port": 47290,
    "dav_port": 47292,
    "dav_token": None,
    "clipboard": True,
        "download_dir": "~/Downloads/Tether",
    "shares": {"Home": "~"},
    "sync": [],
    "static_peers": [],
    "scan_interval": 10,
    "commands": [],
    "remote_shell": False,
    "command_timeout": 120,
    "notifications": True,
    "notif_muted": [],
    "media": False,  # no media control on macOS yet
}


def computer_name() -> str:
    """The name from System Settings → General → About, not the .local host name."""
    try:
        import subprocess

        out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname().removesuffix(".local")


class Config:
    def __init__(self, paths: Paths):
        self.paths = paths
        stored = read_json(paths.config_file, {})
        self.data = {**DEFAULTS, **stored}
        if not self.data.get("name"):
            self.data["name"] = computer_name()
        if not self.data.get("dav_token"):
            self.data["dav_token"] = secrets.token_hex(16)
        if self.data != stored:
            self.save()

    def save(self) -> None:
        atomic_write_json(self.paths.config_file, self.data)

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save()

    @property
    def download_dir(self) -> Path:
        return Path(self.data["download_dir"]).expanduser()

    def sync_folders(self) -> dict[str, Path]:
        return {f["id"]: Path(f["path"]).expanduser() for f in self.data["sync"]}


class PeerStore:
    """Paired devices: id -> {name, key (base64 spki), type, addr}."""

    def __init__(self, path: Path):
        self.path = path
        self.peers: dict[str, dict] = read_json(path, {})

    def save(self) -> None:
        atomic_write_json(self.path, self.peers)

    def __contains__(self, peer_id):
        return peer_id in self.peers

    def get(self, peer_id):
        return self.peers.get(peer_id)

    def add(self, peer_id, name, key, type_, addr=None):
        self.peers[peer_id] = {"name": name, "key": key, "type": type_, "addr": addr}
        self.save()

    def remove(self, peer_id) -> bool:
        if self.peers.pop(peer_id, None) is None:
            return False
        self.save()
        return True

    def set_addr(self, peer_id, addr, name=None):
        p = self.peers.get(peer_id)
        if p is None:
            return
        changed = p.get("addr") != list(addr) or (name and p.get("name") != name)
        if changed:
            p["addr"] = list(addr)
            if name:
                p["name"] = name
            self.save()
