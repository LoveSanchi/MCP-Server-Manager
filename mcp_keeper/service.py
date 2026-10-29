"""Commands the CLI and the indicator send to the service. Every reply uses the same
envelope: {"ok": bool, "data": ..., "error": str|null}."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from mcp_keeper import backup, fsutil, probes
from mcp_keeper.clients import all_adapters, inventory, set_wrapped_everywhere
from mcp_keeper.registry import DOWN, STARTING, Registry

log = logging.getLogger("mcp-keeper")

SendRestart = Callable[[int, str], bool]


class RequestError(ValueError):
    pass


class Service:
    def __init__(self, registry: Registry, send_restart: SendRestart,
                 load_inventory=inventory, adapters=all_adapters,
                 test_stdio=probes.test_stdio, check_remote=probes.check_remote):
        self._reg = registry
        self._send_restart = send_restart
        self._load_inventory = load_inventory
        self._adapters = adapters
        self._test_stdio = test_stdio
        self._check_remote = check_remote
        self._handlers: dict[str, Callable[[dict], Awaitable[Any]]] = {
            "status": self._status, "events": self._events, "restart": self._restart,
            "test": self._test, "refresh": self._refresh, "wrap": self._wrap, "unwrap": self._unwrap,
            "backups": self._backups, "restore": self._restore, "show": self._show,
        }

    async def handle(self, message: Any) -> dict[str, Any]:
        if not isinstance(message, dict) or not isinstance(message.get("cmd"), str):
            return _fail("request must be an object with a 'cmd' string")
        handler = self._handlers.get(message["cmd"])
        if handler is None:
            return _fail(f"unknown command '{message['cmd']}'")
        try:
            return {"ok": True, "data": await handler(message), "error": None}
        except (RequestError, FileNotFoundError, ValueError) as exc:
            return _fail(str(exc))
        except fsutil.ConcurrentEdit as exc:
            return _fail(f"{exc} — try again in a moment")
        except Exception:  # noqa: BLE001 - one bad request must never stop the service
            log.exception("request %r failed", message.get("cmd"))
            return _fail("internal error — see: journalctl --user -u mcp-keeper")

    # ── periodic work (also run on demand) ────────────────────────────────────
    def reload_inventory(self) -> None:
        entries, problems = self._load_inventory()
        self._reg.set_inventory(entries, problems)

    async def check_remotes(self) -> None:
        remote = [e for e in self._reg.entries() if e.transport != "stdio" and e.url]
        results = await asyncio.gather(*(self._check_remote(e, self._reg.client_path(e.sid)) for e in remote),
                                       return_exceptions=True)
        for entry, result in zip(remote, results):
            state, detail = result if isinstance(result, tuple) else ("down", f"check failed: {result}")
            self._reg.set_remote(entry.sid, state, detail)

    # ── handlers ──────────────────────────────────────────────────────────────
    async def _status(self, _msg: dict) -> dict:
        return {"summary": self._reg.summary(), "rows": self._reg.rows()}

    async def _events(self, msg: dict) -> list[dict]:
        since = msg.get("since", 0)
        limit = msg.get("limit", 500)
        if not isinstance(since, int) or not isinstance(limit, int):
            raise RequestError("'since' and 'limit' must be numbers")
        return self._reg.events(since, msg.get("id") or None, max(1, min(limit, 5000)))

    async def _restart(self, msg: dict) -> dict:
        if msg.get("all_red"):
            targets = [r["id"] for r in self._reg.rows() if r["state"] in (DOWN, STARTING) and r["instances"]]
        else:
            targets = [_sid(msg)]
        sent = 0
        for sid in targets:
            conns = self._reg.connections_for(sid)
            if not conns and not msg.get("all_red"):
                raise RequestError(f"no client is running {sid} right now — use Test instead")
            for conn in conns:
                sent += self._send_restart(conn, "restart requested from mcp-keeper")
            self._reg.add_event(sid, "warn", f"restart requested ({len(conns)} running instance(s))")
        return {"servers": targets, "instances": sent}

    async def _test(self, msg: dict) -> dict:
        sid = _sid(msg)
        entry = self._reg.entry(sid)
        if entry is None:
            raise RequestError(f"no configured server {sid}")
        if entry.transport == "stdio":
            result = await self._test_stdio(entry, path=self._reg.client_path(sid))
            if self._reg.connections_for(sid):
                result = {**result, "detail": f"{result['detail']} (note: a client is running it too)"}
        else:
            state, detail = await self._check_remote(entry, self._reg.client_path(sid))
            self._reg.set_remote(sid, state, detail)
            # "reachable" is a pass: the server is up; only the client can hold its login.
            result = {"ok": state in ("up", "reachable"), "detail": detail}
        self._reg.set_test(sid, result)
        return result

    async def _show(self, msg: dict) -> dict:
        sid = _sid(msg)
        entry = self._reg.entry(sid)
        if entry is None:
            raise RequestError(f"no configured server {sid}")
        return entry.detail()

    async def _refresh(self, _msg: dict) -> dict:
        self.reload_inventory()
        await self.check_remotes()
        return await self._status({})

    async def _wrap(self, msg: dict) -> dict:
        return self._set_wrapped(msg, True)

    async def _unwrap(self, msg: dict) -> dict:
        return self._set_wrapped(msg, False)

    def _set_wrapped(self, msg: dict, wrapped: bool) -> dict:
        client, names = msg.get("client"), msg.get("names")
        if names is not None and not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
            raise RequestError("'names' must be a list of server names")
        changed, failed = set_wrapped_everywhere(wrapped, client, set(names) if names else None, self._adapters())
        for label, done in changed.items():
            log.info("%s %s: %s", "wrapped" if wrapped else "unwrapped", label, ", ".join(done))
        self.reload_inventory()
        return {"changed": changed, "failed": failed,
                "note": "restart those clients (or reconnect their MCP servers) for this to take effect"}

    async def _backups(self, _msg: dict) -> list[dict]:
        return [{"stamp": stamp, "reason": reason, "files": [b.original for b in files]}
                for stamp, reason, files in backup.list_backups()]

    async def _restore(self, msg: dict) -> dict:
        stamp = msg.get("stamp")
        if not isinstance(stamp, str) or not stamp:
            raise RequestError("'stamp' is required")
        if msg.get("force") is not True:
            raise RequestError("a restore replaces whole files (losing what the client wrote since); "
                               "send force=true, or use unwrap to undo only mcp-keeper's change")
        restored = backup.restore(stamp, msg.get("file"))
        self.reload_inventory()
        return {"restored": restored}


def _sid(msg: dict) -> str:
    sid = msg.get("id")
    if not isinstance(sid, str) or not sid:
        raise RequestError("'id' is required")
    return sid


def _fail(error: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": error}
