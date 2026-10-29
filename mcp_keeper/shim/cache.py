"""Last known handshake and lists of a server, so a client starting while the server
is down still gets a valid handshake and sees the server's tools."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from mcp_keeper import paths

LIST_METHODS = ("tools/list", "prompts/list", "resources/list", "resources/templates/list")


class Cache:
    def __init__(self, sid: str, path: Path | None = None):
        self._path = path or paths.cache_file(sid)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str) -> Any:
        return self._data.get(key)

    def put(self, key: str, value: Any) -> None:
        if self._data.get(key) == value:
            return
        self._data = {**self._data, key: value}
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".cache.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self._path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)  # a cache that cannot be written is only a cache
