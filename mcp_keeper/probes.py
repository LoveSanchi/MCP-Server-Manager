"""On-demand checks run by the service itself.

test_stdio: start a server exactly as its client would, do the MCP handshake and list
            its tools, then stop it. Used for servers no client is running right now.
check_remote: is a remote (HTTP/SSE) MCP endpoint answering? The client owns the login,
            so 401/403 means "reachable", never "up"."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp_keeper import jsonrpc, redact
from mcp_keeper.reasons import best_reason
from mcp_keeper.clients.base import ServerEntry
from mcp_keeper.shim.child import LINE_LIMIT, _signal_group

TEST_TIMEOUT_SECONDS = 25.0
REMOTE_TIMEOUT_SECONDS = 8.0
INIT_PARAMS = {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "mcp-keeper-test", "version": "1"}}


def _cwd(entry: ServerEntry) -> str:
    if entry.scope.startswith("local:") and Path(entry.scope[6:]).is_dir():
        return entry.scope[6:]
    return str(Path.home())


def _env(entry: ServerEntry, path: str) -> dict[str, str]:
    """The service runs with systemd's minimal PATH; servers found via nvm, pipx or
    ~/.local/bin need the PATH the client ran them with (reported by its shim)."""
    home = str(Path.home())
    fallback = os.pathsep.join([f"{home}/.local/bin", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")])
    return {**os.environ, "PATH": path or fallback, **entry.env}


async def test_stdio(entry: ServerEntry, timeout: float = TEST_TIMEOUT_SECONDS, path: str = "") -> dict[str, Any]:
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *entry.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=_env(entry, path), cwd=_cwd(entry),
            start_new_session=True, limit=LINE_LIMIT)
    except OSError as exc:
        return {"ok": False, "detail": f"cannot start: {exc.strerror or exc}"}
    try:
        return await asyncio.wait_for(_talk(proc, started), timeout)
    except asyncio.TimeoutError:
        tail = await _stderr_tail(proc)
        return {"ok": False, "detail": f"no complete handshake within {int(timeout)}s{tail}"}
    finally:
        if proc.returncode is None:
            _signal_group(proc.pid, 9)
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        await proc.wait()
        await asyncio.sleep(0)  # let the pipe transports close inside the loop


async def _talk(proc, started: float) -> dict[str, Any]:
    async def ask(rid: int, method: str, params: dict) -> dict | None:
        proc.stdin.write(jsonrpc.encode(jsonrpc.request(rid, method, params)))
        await proc.stdin.drain()
        while line := await proc.stdout.readline():
            message = jsonrpc.decode(line)
            if message and jsonrpc.is_response(message) and message.get("id") == rid:
                return message
        return None

    init = await ask(1, "initialize", INIT_PARAMS)
    if init is None or "error" in init or not isinstance(init.get("result"), dict):
        error = (init or {}).get("error")
        why = "exited" if init is None else error.get("message", error) if isinstance(error, dict) else error
        return {"ok": False, "detail": f"handshake failed: {redact.text(str(why))}{await _stderr_tail(proc)}"}
    proc.stdin.write(jsonrpc.encode(jsonrpc.notification("notifications/initialized")))
    tools = []
    if "tools" in init["result"].get("capabilities", {}):
        listed = await ask(2, "tools/list", {})
        tools = (listed or {}).get("result", {}).get("tools", [])
    info = init["result"].get("serverInfo") or {}
    ms = int((time.monotonic() - started) * 1000)
    return {"ok": True, "tools": len(tools), "server": f"{info.get('name', '')} {info.get('version', '')}".strip(),
            "detail": f"ok: handshake + {len(tools)} tools in {ms} ms"}


async def _stderr_tail(proc) -> str:
    if proc.returncode is None:
        _signal_group(proc.pid, 9)
    try:
        data = await asyncio.wait_for(proc.stderr.read(), 2)
    except asyncio.TimeoutError:
        return ""
    lines = [ln for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
    return f" — {redact.text(best_reason(lines))[:200]}" if lines else ""


def _post_initialize(url: str, transport: str) -> tuple[str, str]:
    if transport == "sse":
        request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    else:
        body = json.dumps(jsonrpc.request(1, "initialize", INIT_PARAMS)).encode()
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(request, timeout=REMOTE_TIMEOUT_SECONDS) as response:
            return "up", f"answered (HTTP {response.status})"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "reachable", (f"server is up and asks for a login (HTTP {exc.code}); "
                                 f"only the client holds that login")
        return "reachable", f"reachable but answered HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return "down", f"unreachable: {redact.text(str(reason))[:200]}"


CLAUDE_GET_TIMEOUT_SECONDS = 45.0


def _claude_status(output: str) -> tuple[str, str] | None:
    """Parse `claude mcp get NAME`: the client's own verdict, including its OAuth login."""
    for line in output.splitlines():
        line = line.strip()
        if not line.lower().startswith("status:"):
            continue
        status = line.split(":", 1)[1].strip().lstrip("✔✘✗!× ").strip()
        lowered = status.lower()
        if lowered.startswith("connected"):
            return "up", "connected in Claude Code"
        if "auth" in lowered:
            return "down", "Claude Code needs you to log in to it (run /mcp in Claude Code)"
        return "down", f"Claude Code reports: {redact.text(status)[:160]}"
    return None


async def _ask_claude(entry: ServerEntry, path: str) -> tuple[str, str] | None:
    """Only Claude Code holds a remote server's login, so ask it rather than guessing."""
    env = _env(entry, path)
    claude = shutil.which("claude", path=env["PATH"])
    if not claude:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            claude, "mcp", "get", entry.name, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env, cwd=_cwd(entry))
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), CLAUDE_GET_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return _claude_status(out.decode("utf-8", "replace"))


async def check_remote(entry: ServerEntry, path: str = "") -> tuple[str, str]:
    if entry.client == "claude-code":
        verdict = await _ask_claude(entry, path)
        if verdict is not None:
            return verdict
    return await asyncio.to_thread(_post_initialize, entry.url, entry.transport)
