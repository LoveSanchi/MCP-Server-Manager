"""Synchronous client for the service's control socket (CLI and indicator)."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from mcp_keeper import paths

DEFAULT_TIMEOUT = 60.0


class ServiceUnavailable(RuntimeError):
    pass


class ServiceError(RuntimeError):
    pass


def request(cmd: str, timeout: float = DEFAULT_TIMEOUT, sock_path: Path | None = None, **args: Any) -> Any:
    sock_path = sock_path or paths.ctl_socket()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(sock_path))
            sock.sendall((json.dumps({"cmd": cmd, **args}) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(1 << 16)
                if not chunk:
                    break
                data += chunk
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise ServiceUnavailable("mcp-keeper service is not running — start it: systemctl --user start mcp-keeper") from exc
    except socket.timeout as exc:
        raise ServiceError(f"no reply from mcp-keeper within {int(timeout)}s") from exc
    except OSError as exc:
        raise ServiceUnavailable(f"cannot reach mcp-keeper at {sock_path}: {exc.strerror or exc}") from exc
    try:
        reply = json.loads(data)
    except ValueError as exc:
        raise ServiceError("malformed reply from mcp-keeper") from exc
    if not reply.get("ok"):
        raise ServiceError(reply.get("error") or "request failed")
    return reply.get("data")
