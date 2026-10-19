"""Configuration, read from a TOML file.

Lookup order for the file: explicit path, $MULTIDROP_CONFIG,
$XDG_CONFIG_HOME/multidrop/config.toml. A missing file means defaults.

Example::

    alias = "Laptop"
    download_dir = "~/Downloads/multidrop"
    # Protocol order for devices that speak several (first wins).
    prefer = ["quickshare", "localsend"]

    [localsend]
    port = 53317
    https = true
    pin = "123456"

    [quickshare]
    port = 53318

    [daemon]
    web_port = 53319     # web UI, always bound to 127.0.0.1
    accept = "ask"       # or "all"
    history = true       # keep the history in ~/.local/state/multidrop/history.jsonl
"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


WINDOWS = os.name == "nt"


def runtime_dir() -> Path:
    """Per-user directory for the daemon socket and the web UI URL (mode 0700).
    $XDG_RUNTIME_DIR wins everywhere, like the other XDG paths."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if WINDOWS and not base:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "multidrop" / "run"
    base = base or f"/tmp/multidrop-{os.getuid()}"
    return Path(base) / "multidrop"


def default_config_path() -> Path:
    env = os.environ.get("MULTIDROP_CONFIG")
    if env:
        return Path(env)
    return _xdg("XDG_CONFIG_HOME", ".config") / "multidrop" / "config.toml"


# Quick Share first: Android and Samsung phones have it built in, LocalSend
# needs an app.
DEFAULT_PREFER = ("quickshare", "localsend", "airdrop")


@dataclass
class LocalSendConfig:
    enabled: bool = True
    port: int = 53317
    https: bool = True
    bind: str = "0.0.0.0"
    multicast_group: str = "224.0.0.167"
    multicast_port: int = 53317
    # IPv4 address of the interface used for multicast. 0.0.0.0 lets the
    # kernel pick (normally the default-route interface).
    multicast_interface: str = "0.0.0.0"
    pin: str | None = None
    # Browser download ("multidrop share"): plain HTTP, only while sharing.
    share_port: int = 53320
    share_bind: str = "0.0.0.0"


@dataclass
class QuickShareConfig:
    enabled: bool = True
    # Fixed so a firewall rule is possible. If it is taken, a random free
    # port is used instead (logged); 0 = always random.
    port: int = 53318   # = quickshare.frames.DEFAULT_PORT
    bind: str = "0.0.0.0"
    # IPv4 addresses to announce over mDNS. Empty = the default-route address.
    mdns_addresses: list[str] = field(default_factory=list)
    mdns: bool = True
    # BLE "wake up" advertisement so Android devices publish themselves.
    ble: bool = True


@dataclass
class AirDropConfig:
    # Off by default: needs the AWDL radio (OWL, root, see docs/INSTALL-ROOT.md).
    enabled: bool = False
    interface: str = "awdl0"
    port: int = 8771
    # Empty = the IPv6 link-local address of `interface` (never all interfaces).
    bind: str = ""
    # Shown on iPhones; a Mac model gets the laptop icon.
    model: str = "MacBookPro18,3"
    mdns: bool = True
    # mDNS instance name. Empty = random, kept in state_dir. "mac" = the
    # interface MAC as hex, which omdrop-owl's announcement helper uses (then
    # set mdns = false and let the helper announce).
    service_id: str = ""
    # Addresses for mDNS; empty = the bind address.
    mdns_addresses: list[str] = field(default_factory=list)
    # While visible, repeat the mDNS announcement: `announce_burst` copies
    # every `announce_interval` seconds (0 = off), see airdrop/mdns.py.
    announce_interval: float = 3.0
    announce_burst: int = 3
    # Sending: wake iPhones with Apple's BLE AirDrop advertisement first
    # (BlueZ over D-Bus as a normal user, like Quick Share's FE2C wake-up).
    ble: bool = True
    ble_shape: str = "os26"        # or "published" (older iOS), see airdrop/ble.py
    ble_connectable: bool = False  # Apple's own adverts are connectable


@dataclass
class DaemonConfig:
    # The web UI only ever listens on 127.0.0.1; if the port is taken a
    # random one is used (see `multidrop ui`).
    web_port: int = 53319
    accept: str = "ask"          # "ask" (notification / web UI) or "all"
    ask_timeout: float = 300.0   # unanswered offers are rejected after this
    notifications: bool = True
    # Unix socket for the local API. Empty = $XDG_RUNTIME_DIR/multidrop/multidropd.sock
    socket: str = ""
    # Keep the event history (web UI "Verlauf") across restarts, mode 0600.
    history: bool = True
    # Empty = $XDG_STATE_HOME/multidrop/history.jsonl (~/.local/state/...)
    history_file: str = ""

    def history_path(self) -> Path:
        return Path(self.history_file).expanduser() if self.history_file \
            else _xdg("XDG_STATE_HOME", ".local/state") / "multidrop" / "history.jsonl"


@dataclass
class Config:
    alias: str = field(default_factory=socket.gethostname)
    download_dir: Path = field(
        default_factory=lambda: Path.home() / "Downloads" / "multidrop")
    state_dir: Path = field(
        default_factory=lambda: _xdg("XDG_DATA_HOME", ".local/share") / "multidrop")
    localsend: LocalSendConfig = field(default_factory=LocalSendConfig)
    quickshare: QuickShareConfig = field(default_factory=QuickShareConfig)
    prefer: list[str] = field(default_factory=lambda: list(DEFAULT_PREFER))
    airdrop: AirDropConfig = field(default_factory=AirDropConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or default_config_path()
        data = {}
        if path.exists():
            with open(path, "rb") as f:
                data = tomllib.load(f)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        cfg = cls()
        ls_data = data.get("localsend", {})
        known_ls = {f.name for f in fields(LocalSendConfig)}
        cfg.localsend = LocalSendConfig(
            **{k: v for k, v in ls_data.items() if k in known_ls})
        qs_data = data.get("quickshare", {})
        known_qs = {f.name for f in fields(QuickShareConfig)}
        cfg.quickshare = QuickShareConfig(
            **{k: v for k, v in qs_data.items() if k in known_qs})
        ad_data = data.get("airdrop", {})
        known_ad = {f.name for f in fields(AirDropConfig)}
        cfg.airdrop = AirDropConfig(**{k: v for k, v in ad_data.items() if k in known_ad})
        d_data = data.get("daemon", {})
        known_d = {f.name for f in fields(DaemonConfig)}
        cfg.daemon = DaemonConfig(**{k: v for k, v in d_data.items() if k in known_d})
        if cfg.daemon.accept not in ("ask", "all"):
            raise ValueError(f"daemon.accept must be 'ask' or 'all', not {cfg.daemon.accept!r}")
        if "alias" in data:
            cfg.alias = str(data["alias"])
        if "download_dir" in data:
            cfg.download_dir = Path(data["download_dir"]).expanduser()
        if "prefer" in data:
            cfg.prefer = [str(p) for p in data["prefer"]]
        if "state_dir" in data:
            cfg.state_dir = Path(data["state_dir"]).expanduser()
        return cfg
