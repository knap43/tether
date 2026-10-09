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


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or fallback).expanduser()


class Paths:
    def __init__(self, base: str | None = None):
        if base:
            b = Path(base).expanduser()
            self.config_dir, self.data_dir, self.runtime_dir = b / "config", b / "data", b / "run"
        else:
            self.config_dir = _xdg("XDG_CONFIG_HOME", "~/.config") / "tether"
            self.data_dir = _xdg("XDG_DATA_HOME", "~/.local/share") / "tether"
            rt = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/tether-{os.getuid()}"
            self.runtime_dir = Path(rt) / "tether"
        for d in (self.config_dir, self.data_dir, self.runtime_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config_file = self.config_dir / "config.json"
        self.identity_file = self.data_dir / "identity.pem"
        self.peers_file = self.data_dir / "peers.json"
        self.sync_dir = self.data_dir / "sync"
        self.socket = self.runtime_dir / "ctl.sock"


DEFAULTS = {
    "name": socket.gethostname(),
    "port": 47291,
    "discovery_port": 47290,
    "dav_port": 47292,
    "dav_token": None,
    "clipboard": True,
    "clipboard_backend": "auto",
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
}


class Config:
    def __init__(self, paths: Paths):
        self.paths = paths
        stored = read_json(paths.config_file, {})
        self.data = {**DEFAULTS, **stored}
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
