"""Render README screenshots from the real GTK widgets, fed with made-up demo servers.

    python3 docs/screenshots/generate.py      (needs a desktop session; writes PNGs here)

Nothing talks to a real service. The tray dropdown itself is drawn by GNOME Shell, so
menu.png re-draws the indicator's real menu labels (from indicator/model.py) in a
GNOME-like popup.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from mcp_keeper.cli import format_detail  # noqa: E402
from mcp_keeper.indicator import model, windows  # noqa: E402

NOW = time.time()


def row(client, name, state, detail, transport="stdio", program="node", url="", instances=1, **kw):
    return {"id": f"{client.lower().replace(' ', '-')}:user:{name}", "client_label": client, "name": name,
            "state": state, "detail": detail, "transport": transport, "program": program, "url": url,
            "scope": "user", "instances": instances, "clients": ["claude-code"] if instances else [],
            "requests": kw.get("requests", 0), "errors": kw.get("errors", 0), "restarts": kw.get("restarts", 0),
            "since": kw.get("since", NOW - 3600), "test": kw.get("test")}


ROWS = [
    row("Claude Code", "orders-db", "down", "exited with code 1: Error: connect ECONNREFUSED 127.0.0.1:3306; "
        "retrying in 10s", requests=41, errors=3, restarts=4, since=NOW - 25),
    row("Claude Code", "github", "up", "ready", program="github-mcp-server", requests=112, since=NOW - 7200),
    row("Claude Code", "filesystem", "up", "ready", program="npx", requests=58, since=NOW - 7200),
    row("Claude Code", "playwright", "up", "ready", program="npx", requests=9, since=NOW - 1800),
    row("Claude Code", "notion", "up", "connected in Claude Code", transport="http", program="",
        url="https://mcp.notion.com", instances=0),
    row("Codex", "docs-search", "idle", "no client is running it right now", program="uvx", instances=0),
    row("Codex", "sentry", "idle", "no client is running it right now", program="uvx", instances=0),
]
STATUS = {"summary": {"total": 7, "up": 4, "down": 1, "idle": 2, "unmanaged": 0, "reachable": 0, "problems": []},
          "rows": ROWS}
EVENTS = [
    (NOW - 300, "info", "github", "ready: github-mcp-server 0.6.0"),
    (NOW - 240, "info", "github", "tools/call search_issues → ok in 412 ms"),
    (NOW - 200, "info", "filesystem", "tools/call read_file → ok in 3 ms"),
    (NOW - 90, "stderr", "orders-db", "Error: connect ECONNREFUSED 127.0.0.1:3306"),
    (NOW - 90, "error", "orders-db", "server down: exited with code 1: Error: connect ECONNREFUSED 127.0.0.1:3306"),
    (NOW - 89, "error", "orders-db", "tools/call execute_sql failed in 2 ms: orders-db is down: … retry shortly."),
    (NOW - 60, "warn", "orders-db", "starting again (last failure: exited with code 1: Error: connect ECONNREFUSED)"),
    (NOW - 30, "info", "playwright", "tools/call browser_navigate → ok in 1240 ms"),
    (NOW - 25, "error", "orders-db", "server down: exited with code 1: Error: connect ECONNREFUSED 127.0.0.1:3306"),
]
DETAIL = {"id": "claude-code:user:orders-db", "client_label": "Claude Code", "scope": "user", "name": "orders-db",
          "config_path": "~/.claude.json", "transport": "stdio",
          "command_line": "/usr/bin/node /opt/mcp/sql-server --transport stdio --dsn mysql://reader:***@127.0.0.1:3306/orders",
          "wrapped": True, "env_names": ["DB_TIMEOUT", "LOG_LEVEL"], "full_url": ""}
BACKUPS = [
    {"stamp": "20261029-131033-979884", "reason": "wrap Claude Code: orders-db, github, filesystem, playwright",
     "files": ["~/.claude.json"]},
    {"stamp": "20261029-131033-423611", "reason": "wrap Codex: docs-search, sentry", "files": ["~/.codex/config.toml"]},
]

CSS = b"""
.popup { background: #2d2d2d; border-radius: 12px; padding: 8px 0; }
.popup label { color: #f2f2f2; padding: 6px 18px; font-size: 11pt; }
.popup .info label { color: #9a9a9a; }
.popup .hover { background: #424242; border-radius: 6px; margin: 0 6px; }
.popup separator { background: #4a4a4a; min-height: 1px; margin: 4px 0; }
.canvas { background: #1a1a1a; padding: 16px; }
.frame { background-color: @theme_bg_color; padding: 14px; }
"""


def spin(ms: int) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


def snapshot(widget: Gtk.Widget, name: str, framed: bool = True, show_all: bool = True) -> None:
    off = Gtk.OffscreenWindow()
    if framed:  # offscreen windows skip the theme background: paint it ourselves
        frame = Gtk.EventBox()
        frame.get_style_context().add_class("frame")
        frame.add(widget)
        widget = frame
    off.add(widget)
    if show_all:
        off.show_all()
    else:
        widget.show()
        off.show()
    spin(500)
    off.get_surface().write_to_png(str(OUT / name))
    print("wrote", OUT / name)
    off.destroy()


def take(window: Gtk.Window, name: str, width: int, height: int) -> None:
    body = window.get_child()
    window.remove(body)
    window.destroy()
    body.set_size_request(width, height)
    snapshot(body, name, show_all=False)


def _popup(items: list[tuple[str, str]], width: int) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.get_style_context().add_class("popup")
    box.set_size_request(width, -1)
    for kind, text in items:
        if kind == "sep":
            box.pack_start(Gtk.Separator(), False, False, 0)
            continue
        line = Gtk.Box()
        line.get_style_context().add_class(kind)
        line.pack_start(Gtk.Label(label=text, xalign=0), True, True, 0)
        if kind in ("hover", "sub"):
            line.pack_end(Gtk.Label(label="›"), False, False, 0)
        box.pack_start(line, False, False, 0)
    return box


def menu_png() -> None:
    groups = model.groups(ROWS)
    top = [("info", model.overview(STATUS).headline), ("sep", "")]
    top += [("hover" if label == "Claude Code" else "sub", model.group_title(label, rows)) for label, rows in groups]
    top += [("sep", ""), ("item", "Restart all red"), ("item", "Refresh"), ("item", "Live log (all servers)…"),
            ("sep", ""), ("item", "Wrap all servers…"), ("item", "Unwrap all servers…"), ("item", "Config backups…"),
            ("sep", ""), ("item", "Hide indicator")]
    claude = dict(groups)["Claude Code"]
    servers = [("hover" if r["name"] == "orders-db" else "sub", model.row_title(r)) for r in claude]
    down = next(r for r in ROWS if r["name"] == "orders-db")
    actions = [("info", line) for line in model.row_details(down, NOW)] + [("sep", "")]
    actions += [("item", t) for t in ("Restart", "Test now", "Live log…", "Show config…", "Copy id")]

    canvas = Gtk.Box(spacing=8)
    canvas.get_style_context().add_class("canvas")
    for items, width, offset in ((top, 290, 0), (servers, 250, 44), (actions, 470, 44)):
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        spacer = Gtk.Box()
        spacer.set_size_request(-1, offset)
        column.pack_start(spacer, False, False, 0)
        column.pack_start(_popup(items, width), False, False, 0)
        canvas.pack_start(column, False, False, 0)
    snapshot(canvas, "menu.png", framed=False)


def fake_events(*_args, **kwargs):
    if kwargs.get("since", 0):
        return []
    return [{"seq": i + 1, "ts": ts, "level": level, "id": f"claude-code:user:{name}", "text": text}
            for i, (ts, level, name, text) in enumerate(EVENTS)]


def main() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    menu_png()

    log = windows.LiveLog(fake_events)
    spin(1500)
    take(log, "live-log.png", 980, 330)

    take(windows.ConfigWindow(DETAIL, format_detail(DETAIL), lambda p: None), "config.png", 900, 230)
    take(windows.BackupsWindow(BACKUPS, lambda s: None), "backups.png", 760, 260)


if __name__ == "__main__":
    main()
