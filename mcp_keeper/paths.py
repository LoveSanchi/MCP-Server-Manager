"""Filesystem locations (XDG) and server identifiers."""

import hashlib
import os
import re
from pathlib import Path

APP = "mcp-keeper"
DEFAULT_SHIM = "/usr/bin/mcp-keeper-shim"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "").strip()
    return Path(value) if value else Path.home() / fallback


def runtime_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    return Path(runtime) / APP if runtime else state_dir() / "run"


def hub_socket() -> Path:
    """Where stand-ins (shims) report to the service."""
    return Path(os.environ.get("MCP_KEEPER_HUB", "") or runtime_dir() / "hub.sock")


def ctl_socket() -> Path:
    """Where the CLI and the indicator talk to the service."""
    return Path(os.environ.get("MCP_KEEPER_CTL", "") or runtime_dir() / "ctl.sock")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / APP


def backup_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP / "backups"


def shim_path() -> str:
    return os.environ.get("MCP_KEEPER_SHIM", "") or DEFAULT_SHIM


def server_id(client: str, scope: str, name: str) -> str:
    return f"{client}:{scope}:{name}"


def safe_name(sid: str) -> str:
    """Readable and collision-free: 'local:/p/x y' and 'local:/p/x_y' get different names."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "_", sid)[-80:]
    return f"{readable}-{hashlib.sha256(sid.encode()).hexdigest()[:8]}"


def log_file(sid: str) -> Path:
    return state_dir() / "logs" / f"{safe_name(sid)}.log"


def cache_file(sid: str) -> Path:
    return cache_dir() / f"{safe_name(sid)}.json"
