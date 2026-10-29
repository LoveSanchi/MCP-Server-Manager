"""Codex: [mcp_servers.NAME] tables in ~/.codex/config.toml (edited with tomlkit, so
comments and formatting survive)."""

from __future__ import annotations

import os
from pathlib import Path

import tomlkit

from mcp_keeper import backup, fsutil
from mcp_keeper.clients.base import Adapter, ServerEntry, unwrap_command, wrap_command


class CodexAdapter(Adapter):
    key, label = "codex", "Codex"

    def __init__(self) -> None:
        self.skipped: list[str] = []

    @property
    def path(self) -> Path:
        return Path(os.environ.get("CODEX_HOME") or Path(os.environ.get("HOME") or Path.home()) / ".codex") / "config.toml"

    def config_files(self) -> list[Path]:
        return [self.path]

    def _load(self):
        data, sha = fsutil.read(self.path)
        return tomlkit.parse(data.decode("utf-8")), sha, data

    def entries(self) -> list[ServerEntry]:
        if not self.path.exists():
            return []
        self.skipped = []
        doc = self._load()[0]
        out = []
        for name, spec in (doc.get("mcp_servers") or {}).items():
            if not isinstance(spec, dict) or not isinstance(spec.get("args", []), list):
                self.skipped.append(f"{self.label}: skipped malformed server entry '{name}'")
                continue
            command, args = str(spec.get("command", "")), [str(a) for a in spec.get("args", [])]
            real = unwrap_command(command, args) if command else None
            if real:
                command, args = real
            url = str(spec.get("url", ""))
            out.append(ServerEntry(self.key, self.label, "user", name, "stdio" if command else "http",
                                   tuple([command, *args]) if command else (), url, bool(real),
                                   str(self.path), {k: str(v) for k, v in (spec.get("env") or {}).items()}))
        return out

    def set_wrapped(self, names: set[str] | None, wrapped: bool) -> list[str]:
        if not self.path.exists():
            return []
        return fsutil.with_retries(lambda: self._set_wrapped_once(names, wrapped))

    def _set_wrapped_once(self, names: set[str] | None, wrapped: bool) -> list[str]:
        doc, sha, data = self._load()
        changed = []
        for name, spec in (doc.get("mcp_servers") or {}).items():
            if names is not None and name not in names or not isinstance(spec, dict) or "command" not in spec \
                    or not isinstance(spec.get("args", []), list):
                continue
            command, args = str(spec["command"]), [str(a) for a in spec.get("args", [])]
            real = unwrap_command(command, args)
            if wrapped and real is None:
                sid = f"{self.key}:user:{name}"
                new_command, new_args = wrap_command(sid, command, args)
            elif not wrapped and real is not None:
                new_command, new_args = real
            else:
                continue
            spec["command"] = new_command
            spec["args"] = new_args
            changed.append(name)
        if changed:
            if fsutil.sha256(self.path.resolve().read_bytes()) != sha:
                raise fsutil.ConcurrentEdit(str(self.path))
            backup.save({self.path: data}, f"{'wrap' if wrapped else 'unwrap'} {self.label}: {', '.join(changed)}")
            fsutil.write_text_atomic(self.path, tomlkit.dumps(doc), mode=self.path.resolve().stat().st_mode & 0o777,
                                     expect_sha=sha)
        return changed
