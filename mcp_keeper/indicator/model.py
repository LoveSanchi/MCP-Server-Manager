"""What the top-bar indicator shows — pure functions over the service's status."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

ICON_OK, ICON_WARN, ICON_ERROR, ICON_OFF = "mcp-keeper-ok", "mcp-keeper-warn", "mcp-keeper-error", "mcp-keeper-off"
DOTS = {"up": "🟢", "reachable": "🔵", "starting": "🟡", "down": "🔴", "idle": "⚪", "unmanaged": "⚫"}
RANK = {"down": 0, "starting": 1, "up": 2, "reachable": 3, "idle": 4, "unmanaged": 5}


@dataclass(frozen=True)
class Overview:
    icon: str
    label: str
    headline: str


def overview(status: dict | None) -> Overview:
    """Icon, the meter next to it (green/total) and the menu's headline."""
    if status is None:
        return Overview(ICON_OFF, "", "mcp-keeper service is not running")
    s = status["summary"]
    parts = [f"{s['up']}/{s['total']} up"]
    for key, word in (("down", "down"), ("idle", "idle"), ("reachable", "remote"), ("unmanaged", "not wrapped")):
        if s.get(key):
            parts.append(f"{s[key]} {word}")
    headline = " · ".join(parts)
    meter = f"{s['up']}/{s['total']}"
    if s["down"]:
        return Overview(ICON_ERROR if s["up"] == 0 else ICON_WARN, meter, headline)
    return Overview(ICON_OK if s["up"] else ICON_OFF, meter, headline)


def groups(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Rows per client, clients alphabetically, worst servers first inside each."""
    by_client: dict[str, list[dict]] = {}
    for row in rows:
        by_client.setdefault(row["client_label"], []).append(row)
    return [(label, sorted(items, key=lambda r: (RANK.get(r["state"], 9), r["name"])))
            for label, items in sorted(by_client.items())]


def group_title(label: str, rows: list[dict]) -> str:
    worst = min(rows, key=lambda r: RANK.get(r["state"], 9))["state"]
    up = sum(1 for r in rows if r["state"] == "up")
    return f"{DOTS.get(worst, '⚫')}  {label}   {up}/{len(rows)}"


def row_title(row: dict) -> str:
    many = f"  ×{row['instances']}" if row.get("instances", 0) > 1 else ""
    return f"{DOTS.get(row['state'], '⚫')}  {row['name']}{many}"


def _ago(ts: float, now: float) -> str:
    secs = int(now - ts)
    if secs < 90:
        return f"{secs}s"
    return f"{secs // 60}m" if secs < 5400 else f"{secs // 3600}h"


def row_details(row: dict, now: float | None = None) -> list[str]:
    now = now if now is not None else time.time()
    lines = [f"{row['state'].capitalize()} — {row.get('detail') or 'no detail'}"]
    where = row.get("program") or row.get("url") or ""
    lines.append(f"{row['transport']}{' · ' + where if where else ''}" + ("" if row["scope"] == "user" else f" · {row['scope']}"))
    if row.get("instances"):
        clients = ", ".join(row.get("clients") or []) or "a client"
        lines.append(f"running in {clients} ({row['instances']} instance{'s' if row['instances'] > 1 else ''})")
        lines.append(f"requests {row.get('requests', 0)} · errors {row.get('errors', 0)} · restarts {row.get('restarts', 0)}")
    if row.get("since") and row["state"] in ("up", "down", "starting"):
        lines.append(f"{row['state']} for {_ago(row['since'], now)}")
    test = row.get("test")
    if test:
        lines.append(f"last test {_ago(test['at'], now)} ago: {test.get('detail', '')}")
    return [line[:140] for line in lines]


def structure(status: dict | None, busy: bool) -> Any:
    """Changes only when the menu needs different items; all text is updated in place."""
    if status is None:
        return (None, busy)
    rows = status["rows"]
    return (tuple((r["id"], r["client_label"], bool(r.get("instances")), len(row_details(r)),
                   r["state"] == "down") for r in rows), status["summary"]["down"] > 0, busy)


def can_restart(row: dict) -> bool:
    return bool(row.get("instances"))


def can_test(row: dict) -> bool:
    return row["transport"] != "stdio" or row["state"] in ("idle", "unmanaged", "down", "up", "starting")
