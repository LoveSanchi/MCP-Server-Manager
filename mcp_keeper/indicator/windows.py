"""Windows opened from the top-bar menu: live event log, backups, alerts."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

Fetch = Callable[..., Any]
LEVEL_TAGS = {"error": "#e01b24", "warn": "#e5a50a", "stderr": "#9a9996", "info": None}


class LiveLog(Gtk.Window):
    """Follows the service's event stream (every server, or one) as it happens."""

    POLL_MS = 1000

    def __init__(self, fetch: Fetch, sid: str | None = None, title: str = "MCP live log"):
        super().__init__(title=title)
        self.set_default_size(980, 520)
        self._fetch, self._sid, self._since, self._paused = fetch, sid, 0, False
        self._busy = False
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.add(box)
        bar = Gtk.Box(spacing=6)
        self._status = Gtk.Label(xalign=0)
        bar.pack_start(self._status, True, True, 0)
        self._errors_only = Gtk.CheckButton(label="Errors only")
        self._errors_only.connect("toggled", lambda *_: self._reload())
        bar.pack_end(self._errors_only, False, False, 0)
        pause = Gtk.ToggleButton(label="Pause")
        pause.connect("toggled", lambda b: setattr(self, "_paused", b.get_active()))
        bar.pack_end(pause, False, False, 0)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda *_: self._view.get_buffer().set_text(""))
        bar.pack_end(clear, False, False, 0)
        box.pack_start(bar, False, False, 0)

        self._view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        buffer = self._view.get_buffer()
        for level, colour in LEVEL_TAGS.items():
            buffer.create_tag(level, foreground=colour) if colour else buffer.create_tag(level)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(self._view)
        box.pack_start(scroller, True, True, 0)
        self._timer = GLib.timeout_add(self.POLL_MS, self._tick)
        self.connect("destroy", lambda *_: GLib.source_remove(self._timer))
        self.show_all()
        self._tick()

    def _reload(self) -> None:
        self._view.get_buffer().set_text("")
        self._since = 0
        self._tick()

    def _tick(self) -> bool:
        if self._paused or self._busy:
            return True
        self._busy = True
        since = self._since

        def work() -> None:
            try:
                events, error = self._fetch("events", since=since, id=self._sid, limit=1000 if since == 0 else 5000), None
            except Exception as exc:  # noqa: BLE001 - shown in the window, never fatal
                events, error = [], str(exc)
            GLib.idle_add(self._append, events, error)

        threading.Thread(target=work, daemon=True).start()
        return True

    def _append(self, events: list[dict], error: str | None) -> bool:
        self._busy = False
        if error:
            self._status.set_text(f"⚠ {error}")
            return False
        buffer = self._view.get_buffer()
        errors_only = self._errors_only.get_active()
        for e in events:
            self._since = max(self._since, e["seq"])
            if errors_only and e["level"] not in ("error", "warn"):
                continue
            stamp = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
            name = e["id"].rsplit(":", 1)[-1] if self._sid is None else ""
            line = f"{stamp}  {e['level']:<6} {name + '  ' if name else ''}{e['text']}\n"
            buffer.insert_with_tags_by_name(buffer.get_end_iter(), line, e["level"] if e["level"] in LEVEL_TAGS else "info")
        if events:
            self._view.scroll_to_iter(buffer.get_end_iter(), 0.0, False, 0.0, 1.0)
        self._status.set_text(f"following {'all servers' if self._sid is None else self._sid}"
                              f"{' (paused)' if self._paused else ''}")
        return False


class ConfigWindow(Gtk.Window):
    """A server's definition: command, URL, env names (values hidden), config file."""

    def __init__(self, detail: dict, text: str, open_file: Callable[[str], None]):
        super().__init__(title=f"Config — {detail['name']}")
        self.set_default_size(820, 300)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self.add(box)
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        view.set_wrap_mode(Gtk.WrapMode.CHAR)
        view.get_buffer().set_text(text)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(view)
        box.pack_start(scroller, True, True, 0)
        note = Gtk.Label(xalign=0, wrap=True)
        note.set_text("Passwords and environment values are hidden here. The config file has the real values.")
        note.get_style_context().add_class("dim-label")
        box.pack_start(note, False, False, 0)
        bar = Gtk.Box(spacing=6)
        if detail.get("config_path"):
            button = Gtk.Button(label="Open config file")
            button.connect("clicked", lambda *_: open_file(detail["config_path"]))
            bar.pack_end(button, False, False, 0)
        copy = Gtk.Button(label="Copy id")
        copy.connect("clicked", lambda *_: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(detail["id"], -1))
        bar.pack_end(copy, False, False, 0)
        box.pack_start(bar, False, False, 0)
        self.show_all()


class BackupsWindow(Gtk.Window):
    def __init__(self, backups: list[dict], restore: Callable[[str], None]):
        super().__init__(title="MCP config backups")
        self.set_default_size(720, 360)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self.add(box)
        note = Gtk.Label(xalign=0, wrap=True)
        note.set_markup("Every config file is copied here before mcp-keeper changes it. Restoring puts the "
                        "files back exactly as they were (the current versions are backed up first). "
                        "<b>Restart the affected clients afterwards.</b>")
        box.pack_start(note, False, False, 0)
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        for item in backups or [{"stamp": "", "reason": "No backups yet", "files": []}]:
            row = Gtk.Box(spacing=12, margin=6)
            text = Gtk.Label(xalign=0, wrap=True)
            text.set_text(f"{item['stamp']}  {item['reason']}\n" + "\n".join(f"   {f}" for f in item["files"]))
            row.pack_start(text, True, True, 0)
            if item["stamp"]:
                button = Gtk.Button(label="Restore")
                button.connect("clicked", lambda _b, s=item["stamp"]: restore(s))
                row.pack_end(button, False, False, 0)
            listbox.add(row)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(listbox)
        box.pack_start(scroller, True, True, 0)
        self.show_all()


def confirm(title: str, body: str) -> bool:
    dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL, text=title)
    dialog.format_secondary_text(body)
    dialog.set_keep_above(True)
    ok = dialog.run() == Gtk.ResponseType.OK
    dialog.destroy()
    return ok


def message(title: str, body: str, error: bool = False) -> None:
    dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO,
                               buttons=Gtk.ButtonsType.CLOSE, text=title)
    dialog.format_secondary_text(body)
    dialog.set_keep_above(True)
    dialog.run()
    dialog.destroy()
