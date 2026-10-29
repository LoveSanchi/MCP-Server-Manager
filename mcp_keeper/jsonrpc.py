"""MCP stdio framing: one JSON-RPC message per line."""

from __future__ import annotations

import json
from typing import Any

SHIM_ID_PREFIX = "mcpk-"  # ids the shim itself sends to the server; never seen by the client


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any] | None:
    """A message, or None for a blank or non-JSON line (servers sometimes print junk)."""
    line = line.strip()
    if not line:
        return None
    try:
        message = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return message if isinstance(message, dict) else None


def is_request(m: dict) -> bool:
    return "method" in m and "id" in m


def is_notification(m: dict) -> bool:
    return "method" in m and "id" not in m


def is_response(m: dict) -> bool:
    return "method" not in m and "id" in m and ("result" in m or "error" in m)


def error(request_id: Any, message: str, code: int = -32000) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def result(request_id: Any, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def notification(method: str, params: dict | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def request(request_id: Any, method: str, params: dict | None = None) -> dict[str, Any]:
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message
