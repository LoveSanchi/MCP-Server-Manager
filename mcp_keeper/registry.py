"""The service's picture of the world: configured servers (from every client's config)
merged with live reports from running shims, plus a shared event stream."""

from __future__ import annotations

import collections
import itertools
import time
from typing import Any

from mcp_keeper import redact
from mcp_keeper.clients.base import ServerEntry

# Row states, from best to worst. Only "up" is green: a server is green only when a
# running instance has completed its handshake and answers.
UP, REACHABLE, STARTING, DOWN, IDLE, UNMANAGED = "up", "reachable", "starting", "down", "idle", "unmanaged"
_SEVERITY = {"up": 0, "starting": 1, "restarting": 2, "down": 3, "stopped": 0}
MAX_EVENTS = 5000


class Registry:
    def __init__(self) -> None:
        self._entries: dict[str, ServerEntry] = {}
        self._problems: list[str] = []
        self._instances: dict[int, dict[str, Any]] = {}       # connection id → latest snapshot
        self._remote: dict[str, tuple[str, str, float]] = {}  # sid → (state, detail, checked_at)
        self._tests: dict[str, dict[str, Any]] = {}
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAX_EVENTS)
        self._seq = itertools.count(1)
        self._paths: dict[str, str] = {}

    # ── inputs ────────────────────────────────────────────────────────────────
    def set_inventory(self, entries: list[ServerEntry], problems: list[str]) -> None:
        self._entries = {e.sid: e for e in entries}
        self._problems = list(problems)

    def entry(self, sid: str) -> ServerEntry | None:
        return self._entries.get(sid)

    def entries(self) -> list[ServerEntry]:
        return list(self._entries.values())

    def instance_state(self, conn: int, raw: dict[str, Any]) -> None:
        snapshot = _clean_snapshot(raw)
        if snapshot is None:
            return
        if snapshot.get("path"):
            self._paths = {**self._paths, snapshot["id"]: snapshot["path"]}
        before = self._instances.get(conn, {})
        self._instances = {**self._instances, conn: dict(snapshot)}
        if (before.get("state"), before.get("detail")) != (snapshot.get("state"), snapshot.get("detail")):
            level = "info" if snapshot.get("state") in ("up", "stopped") else "warn"
            self.add_event(snapshot.get("id", "?"), level, f"{snapshot.get('state')}: {snapshot.get('detail', '')}")

    def instance_gone(self, conn: int) -> None:
        gone = self._instances.get(conn)
        self._instances = {k: v for k, v in self._instances.items() if k != conn}
        if gone:
            self.add_event(gone.get("id", "?"), "info", "client closed this connection")

    def set_remote(self, sid: str, state: str, detail: str) -> None:
        before = self._remote.get(sid)
        detail = redact.text(str(detail))[:300]
        self._remote = {**self._remote, sid: (str(state), detail, time.time())}
        if before is None or before[:2] != (state, detail):
            self.add_event(sid, "info" if state == UP else "warn", f"remote check: {detail}")

    def set_test(self, sid: str, result: dict[str, Any]) -> None:
        result = {**result, "detail": redact.text(str(result.get("detail", "")))[:300]}
        self._tests = {**self._tests, sid: {**result, "at": time.time()}}
        self.add_event(sid, "info" if result.get("ok") else "error", f"test: {result.get('detail', '')}")

    def add_event(self, sid: str, level: str, text: str, ts: float | None = None) -> None:
        self._events.append({"seq": next(self._seq), "ts": ts or time.time(), "id": sid,
                             "level": level, "text": redact.text(text)[:500]})

    # ── views ─────────────────────────────────────────────────────────────────
    def events(self, since: int = 0, sid: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        out = [e for e in self._events if e["seq"] > since and (sid is None or e["id"] == sid)]
        return out[-limit:]

    def client_path(self, sid: str) -> str:
        """PATH a client last ran this server with (any server's, if this one never ran)."""
        return self._paths.get(sid) or next(iter(self._paths.values()), "")

    def connections_for(self, sid: str) -> list[int]:
        return [conn for conn, snap in self._instances.items() if snap.get("id") == sid]

    def rows(self) -> list[dict[str, Any]]:
        by_sid: dict[str, list[dict]] = collections.defaultdict(list)
        for snap in self._instances.values():
            by_sid[snap.get("id", "?")].append(snap)
        sids = list(self._entries) + [s for s in by_sid if s not in self._entries]
        return [self._row(sid, by_sid.get(sid, [])) for sid in sids]

    def _row(self, sid: str, live: list[dict]) -> dict[str, Any]:
        entry = self._entries.get(sid)
        base = entry.public() if entry else _orphan(sid)
        row = {**base, "instances": len(live), "test": self._tests.get(sid)}
        if live:
            worst = max(live, key=lambda s: _SEVERITY.get(s.get("state", "down"), 3))
            state = worst.get("state", "down")
            state = UP if state in ("up", "stopped") else STARTING if state == "starting" else DOWN
            return {**row, "state": state, "detail": worst.get("detail", ""),
                    "since": worst.get("since", 0), "server": worst.get("server", ""),
                    "tools": max(s.get("tools", 0) for s in live),
                    "restarts": sum(s.get("restarts", 0) for s in live),
                    "requests": sum(s.get("requests", 0) for s in live),
                    "errors": sum(s.get("errors", 0) for s in live),
                    "clients": sorted({s.get("client", "") for s in live if s.get("client")})}
        if base["transport"] != "stdio":
            state, detail, at = self._remote.get(sid, (REACHABLE, "not checked yet", 0))
            return {**row, "state": state, "detail": detail, "since": at}
        if not base["wrapped"]:
            return {**row, "state": UNMANAGED, "detail": "not wrapped: mcp-keeper cannot see or restart it"}
        return {**row, "state": IDLE, "detail": "no client is running it right now"}

    def summary(self) -> dict[str, Any]:
        rows = self.rows()
        count = collections.Counter(r["state"] for r in rows)
        return {"total": len(rows), "up": count[UP], "down": count[DOWN] + count[STARTING],
                "idle": count[IDLE], "unmanaged": count[UNMANAGED], "reachable": count[REACHABLE],
                "problems": list(self._problems)}


_INT_FIELDS = ("tools", "restarts", "requests", "errors")
_STR_FIELDS = ("state", "detail", "client", "server", "path")


def _clean_snapshot(raw: Any) -> dict[str, Any] | None:
    """Reports come from processes over a socket: keep only well-typed fields."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        return None
    clean: dict[str, Any] = {"id": raw["id"][:300]}
    for key in _STR_FIELDS:
        clean[key] = str(raw.get(key, ""))[:2000]
    for key in _INT_FIELDS:
        value = raw.get(key, 0)
        clean[key] = value if isinstance(value, int) and not isinstance(value, bool) else 0
    since = raw.get("since", 0)
    clean["since"] = since if isinstance(since, (int, float)) else 0
    clean["detail"] = redact.text(clean["detail"])
    return clean


def _orphan(sid: str) -> dict[str, Any]:
    """A running shim whose config entry is gone (e.g. edited away while a client still runs it)."""
    client = sid.split(":", 1)[0]
    return {"id": sid, "client": client, "client_label": client, "scope": "", "name": sid.rsplit(":", 1)[-1],
            "transport": "stdio", "wrapped": True, "config_path": "", "program": "", "url": ""}
