"""Top-bar indicator: every MCP server of every client, a green/total meter, one-click
restart, tests, live logs, and config backups."""

from __future__ import annotations

import os

os.environ.setdefault("GDK_BACKEND", "x11")  # GNOME draws the menu: clipboard writes need XWayland

import fcntl  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from mcp_keeper import client, paths  # noqa: E402
from mcp_keeper.cli import format_detail  # noqa: E402
from mcp_keeper.indicator import model, windows  # noqa: E402

ICON_DIR = Path(__file__).resolve().parent / "icons"
POLL_SECONDS = 3
RESTART_NOTE = "Restart those clients (or reconnect their MCP servers, e.g. /mcp in Claude Code) to apply."


class Indicator:
    def __init__(self) -> None:
        self._ind = AppIndicator.Indicator.new("mcp-keeper", model.ICON_OFF,
                                               AppIndicator.IndicatorCategory.SYSTEM_SERVICES)
        self._ind.set_icon_theme_path(str(ICON_DIR))
        self._ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._ind.set_title("MCP servers")
        self._status: dict | None = None
        self._busy = ""
        self._structure: Any = object()
        self._polling = False
        self._header: Gtk.MenuItem | None = None
        self._titles: dict[str, Gtk.MenuItem] = {}
        self._group_titles: dict[str, Gtk.MenuItem] = {}
        self._details: dict[str, list[Gtk.MenuItem]] = {}
        self._render()
        self._poll()
        GLib.timeout_add_seconds(POLL_SECONDS, self._poll)

    # ── polling ───────────────────────────────────────────────────────────────
    def _poll(self) -> bool:
        if not self._polling:
            self._polling = True
            threading.Thread(target=self._fetch, daemon=True).start()
        return True

    def _fetch(self) -> None:
        try:
            status = client.request("status", timeout=3)
        except (client.ServiceUnavailable, client.ServiceError):
            status = None
        GLib.idle_add(self._apply, status)

    def _apply(self, status: dict | None) -> bool:
        self._polling = False
        self._status = status
        self._render()
        return False

    def _render(self) -> None:
        view = model.overview(self._status)
        self._ind.set_icon_full(view.icon, view.headline)
        self._ind.set_label(view.label, "00/00")
        structure = model.structure(self._status, bool(self._busy))
        if structure != self._structure:
            self._structure = structure
            self._ind.set_menu(self._build_menu())
        self._update_texts(view.headline)

    def _update_texts(self, headline: str) -> None:
        if self._header:
            self._header.set_label(f"⏳ {self._busy}…" if self._busy else headline)
        if not self._status:
            return
        for label, rows in model.groups(self._status["rows"]):
            if label in self._group_titles:
                self._group_titles[label].set_label(model.group_title(label, rows))
            for row in rows:
                if row["id"] in self._titles:
                    self._titles[row["id"]].set_label(model.row_title(row))
                for item, text in zip(self._details.get(row["id"], []), model.row_details(row)):
                    item.set_label(text)

    # ── background work ───────────────────────────────────────────────────────
    def _run(self, label: str, fn: Callable[[], Any], done: Callable[[Any], None] | None = None) -> None:
        self._busy = label
        self._render()

        def worker() -> None:
            try:
                result, error = fn(), None
            except Exception as exc:  # noqa: BLE001 - reported to the user
                result, error = None, str(exc)
            GLib.idle_add(self._finished, result, error, done)

        threading.Thread(target=worker, daemon=True).start()

    def _finished(self, result: Any, error: str | None, done) -> bool:
        self._busy = ""
        if error:
            windows.message("MCP action failed", error, error=True)
        elif done:
            done(result)
        self._poll()
        return False

    def _call(self, label: str, cmd: str, done=None, **args: Any) -> Callable[..., None]:
        return lambda *_: self._run(label, lambda: client.request(cmd, timeout=90, **args), done)

    # ── menu ──────────────────────────────────────────────────────────────────
    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        self._titles, self._group_titles, self._details = {}, {}, {}
        self._header = _label(menu, "")
        menu.append(Gtk.SeparatorMenuItem())
        idle = not self._busy
        if self._status is None:
            _item(menu, "Start mcp-keeper service", self._start_service)
        else:
            for problem in self._status["summary"].get("problems", []):
                _label(menu, f"⚠ {problem}")
            for label, rows in model.groups(self._status["rows"]):
                group = Gtk.MenuItem(label=model.group_title(label, rows))
                sub = Gtk.Menu()
                for row in rows:
                    item = Gtk.MenuItem(label=model.row_title(row))
                    item.set_submenu(self._server_menu(row))
                    sub.append(item)
                    self._titles[row["id"]] = item
                group.set_submenu(sub)
                menu.append(group)
                self._group_titles[label] = group
            menu.append(Gtk.SeparatorMenuItem())
            if self._status["summary"]["down"]:
                _item(menu, "Restart all red", self._call("Restarting red servers", "restart", all_red=True), idle)
            _item(menu, "Refresh", self._call("Refreshing", "refresh"), idle)
            _item(menu, "Live log (all servers)…", lambda *_: windows.LiveLog(client.request))
            menu.append(Gtk.SeparatorMenuItem())
            _item(menu, "Wrap all servers…", lambda *_: self._wrap(True), idle)
            _item(menu, "Unwrap all servers…", lambda *_: self._wrap(False), idle)
            _item(menu, "Config backups…", self._backups, idle)
        menu.append(Gtk.SeparatorMenuItem())
        _item(menu, "Hide indicator", lambda *_: Gtk.main_quit())
        menu.show_all()
        return menu

    def _server_menu(self, row: dict) -> Gtk.Menu:
        sid, idle = row["id"], not self._busy
        sub = Gtk.Menu()
        self._details[sid] = [_label(sub, line) for line in model.row_details(row)]
        sub.append(Gtk.SeparatorMenuItem())
        if model.can_restart(row):
            _item(sub, "Restart", self._call(f"Restarting {row['name']}", "restart", id=sid), idle)
        if model.can_test(row):
            _item(sub, "Test now", self._call(f"Testing {row['name']}", "test", done=self._tested, id=sid), idle)
        _item(sub, "Live log…", lambda *_: windows.LiveLog(client.request, sid, f"MCP live log — {row['name']}"))
        _item(sub, "Show config…", lambda *_: self._show_config(sid))
        _item(sub, "Copy id", lambda *_: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(sid, -1))
        return sub

    # ── handlers ──────────────────────────────────────────────────────────────
    def _show_config(self, sid: str) -> None:
        def show(detail: dict) -> None:
            windows.ConfigWindow(detail, format_detail(detail), _open_file)

        self._run("Loading config", lambda: client.request("show", id=sid), show)

    def _tested(self, result: dict) -> None:
        windows.message("Test passed" if result.get("ok") else "Test failed", result.get("detail", ""),
                        error=not result.get("ok"))

    def _wrap(self, wrapped: bool) -> None:
        verb = "Wrap" if wrapped else "Unwrap"
        body = ("mcp-keeper will stand in front of every local (stdio) MCP server of every client, so it can "
                "watch and restart them. Every config file is backed up first." if wrapped else
                "Every client config goes back to starting its servers directly.") + f"\n\n{RESTART_NOTE}"
        if windows.confirm(f"{verb} all MCP servers?", body):
            self._run(f"{verb}ping", lambda: client.request("wrap" if wrapped else "unwrap", timeout=60),
                      lambda r: windows.message(f"{verb} done", _changes(r)))

    def _backups(self, *_: Any) -> None:
        def restore(stamp: str) -> None:
            body = ("This puts back the WHOLE file(s) as they were then, losing everything the client wrote "
                    "since (history, trust, newly added servers…). To undo only mcp-keeper's change, use "
                    f"'Unwrap all servers' instead.\n\n{RESTART_NOTE}")
            if windows.confirm(f"Restore backup {stamp}?", body):
                self._run("Restoring", lambda: client.request("restore", stamp=stamp, force=True),
                          lambda r: windows.message("Restored", "\n".join(r["restored"])))

        self._run("Loading backups", lambda: client.request("backups"), lambda b: windows.BackupsWindow(b, restore))

    def _start_service(self, *_: Any) -> None:
        self._run("Starting service", lambda: subprocess.run(
            ["systemctl", "--user", "start", "mcp-keeper.service"], check=True, timeout=30))


def _open_file(path: str) -> None:
    try:
        Gio.AppInfo.launch_default_for_uri(Path(path).resolve().as_uri(), None)
    except GLib.Error as exc:
        windows.message("Could not open the config file", exc.message, error=True)


def _changes(result: dict) -> str:
    changed = result.get("changed") or {}
    if not changed and not result.get("failed"):
        return "Nothing to change."
    lines = [f"{client_label}: {', '.join(names)}" for client_label, names in changed.items()]
    lines += [f"FAILED {client_label}: {error}" for client_label, error in (result.get("failed") or {}).items()]
    return "\n".join(lines) + f"\n\n{result.get('note', '')}"


def _label(menu: Gtk.Menu, text: str) -> Gtk.MenuItem:
    item = Gtk.MenuItem(label=text)
    item.set_sensitive(False)
    menu.append(item)
    return item


def _item(menu: Gtk.Menu, text: str, handler: Callable, sensitive: bool = True) -> None:
    item = Gtk.MenuItem(label=text)
    item.connect("activate", handler)
    item.set_sensitive(sensitive)
    menu.append(item)


def _single_instance() -> object | None:
    lock_path = paths.runtime_dir() / "indicator.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> int:
    lock = _single_instance()
    if lock is None:
        print("mcp-keeper-indicator is already running", file=sys.stderr)
        return 0
    Indicator()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
