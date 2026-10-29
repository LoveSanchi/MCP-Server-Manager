"""Where a shim records what happens: a per-server log file (always) and the service (when
connected). Every text here has already been through redact.text().

Writes are buffered (one open handle, flushed in batches) and stderr forwarded to the
service is rate-limited, so a chatty server cannot slow down MCP traffic or flood the
shared event stream."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import IO, Any

from mcp_keeper import paths

MAX_LOG_BYTES = 1024 * 1024
STDERR_EVENTS_PER_SECOND = 10


class Journal:
    def __init__(self, sid: str, log_path: Path | None = None):
        self._sid = sid
        self._path = log_path or paths.log_file(sid)
        self._hub = None
        self._fh: IO[str] | None = None
        self._written = 0
        self._window, self._sent, self._suppressed = 0.0, 0, 0

    def attach(self, hub) -> None:
        self._hub = hub

    def event(self, level: str, text: str) -> None:
        self._write(f"[{level}] {text}", flush=True)
        self._send({"type": "event", "level": level, "text": text})

    def stderr(self, text: str) -> None:
        self._write(f"[stderr] {text}", flush=False)
        now = time.monotonic()
        if now - self._window >= 1.0:
            if self._suppressed:
                self._send({"type": "event", "level": "stderr",
                            "text": f"({self._suppressed} more stderr lines in the log file)"})
            self._window, self._sent, self._suppressed = now, 0, 0
        if self._sent < STDERR_EVENTS_PER_SECOND:
            self._sent += 1
            self._send({"type": "event", "level": "stderr", "text": text})
        else:
            self._suppressed += 1

    def state(self, snapshot: dict[str, Any]) -> None:
        self._send({"type": "state", **snapshot})

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def _send(self, message: dict[str, Any]) -> None:
        if self._hub is not None:
            self._hub.send({"id": self._sid, "ts": time.time(), **message})

    def _open(self) -> IO[str]:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._path.exists() and self._path.stat().st_size > MAX_LOG_BYTES:
            self._path.replace(self._path.with_suffix(".log.1"))
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")

    def _write(self, line: str, flush: bool) -> None:
        try:
            if self._fh is None or self._written > MAX_LOG_BYTES:
                self.close()
                self._fh, self._written = self._open(), 0
            entry = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n"
            self._fh.write(entry)
            self._written += len(entry)
            if flush:
                self._fh.flush()
        except OSError:
            pass  # logging must never break the MCP connection
