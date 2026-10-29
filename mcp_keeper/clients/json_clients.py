"""Clients whose MCP list lives in a JSON file: Claude Code, Claude Desktop, Cursor,
Windsurf, Antigravity, Gemini CLI and VS Code."""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterator

from mcp_keeper import backup, fsutil
from mcp_keeper.clients.base import Adapter, ServerEntry, unwrap_command, wrap_command

Section = tuple[str, dict]  # (scope, the dict of servers inside the document)


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


class JsonAdapter(Adapter):
    def __init__(self, key: str, label: str, relpath: str, servers_key: str = "mcpServers"):
        self.key, self.label = key, label
        self.skipped: list[str] = []
        self._relpath = relpath
        self._servers_key = servers_key

    @property
    def path(self) -> Path:
        return _home() / self._relpath

    def config_files(self) -> list[Path]:
        return [self.path]

    def sections(self, doc: dict) -> Iterator[Section]:
        servers = doc.get(self._servers_key)
        if isinstance(servers, dict):
            yield "user", servers

    # ── read ──────────────────────────────────────────────────────────────────
    def _load(self) -> tuple[dict, str, str, bool]:
        """(document, sha of the bytes read, raw text, whether it had JSONC comments)."""
        data, sha = fsutil.read(self.path)
        raw = data.decode("utf-8")
        if not raw.strip():
            return {}, sha, raw, False
        try:
            doc, jsonc = json.loads(raw), False
        except ValueError:
            doc, jsonc = json.loads(_strip_jsonc(raw)), True  # VS Code allows comments
        if not isinstance(doc, dict):
            raise ValueError(f"{self.path}: expected a JSON object")
        return doc, sha, raw, jsonc

    def entries(self) -> list[ServerEntry]:
        self.skipped = []
        if not self.path.exists():
            return []
        doc = self._load()[0]
        out = []
        for scope, servers in self.sections(doc):
            for name, spec in servers.items():
                entry = self._entry(scope, name, spec)
                if entry is None:
                    self.skipped.append(f"{self.label}: skipped malformed server entry '{name}'")
                else:
                    out.append(entry)
        return out

    def _entry(self, scope: str, name: str, spec: Any) -> ServerEntry | None:
        if not isinstance(spec, dict):
            return None
        command, args = spec.get("command", ""), spec.get("args") or []
        if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            return None
        url = spec.get("url") or spec.get("serverUrl") or spec.get("httpUrl") or ""
        kind = spec.get("type") or ("stdio" if command else "http")
        kind = "http" if kind in ("streamable-http", "streamableHttp") else str(kind)
        real = unwrap_command(command, list(args)) if command else None
        if real:
            command, args = real
        env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
        return ServerEntry(self.key, self.label, scope, str(name), "stdio" if command else kind,
                           tuple([command, *args]) if command else (), str(url), bool(real),
                           str(self.path), {str(k): str(v) for k, v in env.items()})

    # ── write ─────────────────────────────────────────────────────────────────
    def set_wrapped(self, names: set[str] | None, wrapped: bool) -> list[str]:
        if not self.path.exists():
            return []
        return fsutil.with_retries(lambda: self._set_wrapped_once(names, wrapped))

    def _set_wrapped_once(self, names: set[str] | None, wrapped: bool) -> list[str]:
        doc, sha, raw, jsonc = self._load()
        updated = copy.deepcopy(doc)
        changed = []
        for scope, servers in self.sections(updated):
            for name, spec in list(servers.items()):
                if names is not None and name not in names:
                    continue
                new_spec = self._rewrite(scope, name, spec, wrapped)
                if new_spec is not None:
                    servers[name] = new_spec
                    changed.append(name if scope == "user" else f"{name} ({scope})")
        if not changed:
            return []
        if jsonc:
            raise ValueError(f"{self.path} contains comments; mcp-keeper will not rewrite it (edit it by hand)")
        if fsutil.sha256(self.path.resolve().read_bytes()) != sha:
            raise fsutil.ConcurrentEdit(str(self.path))
        backup.save({self.path: raw.encode("utf-8")}, f"{'wrap' if wrapped else 'unwrap'} {self.label}: {', '.join(changed)}")
        text = json.dumps(updated, indent=_indent(raw), ensure_ascii=False)
        fsutil.write_text_atomic(self.path, text + ("\n" if raw.endswith("\n") else ""),
                                 mode=self.path.resolve().stat().st_mode & 0o777, expect_sha=sha)
        return changed

    def _rewrite(self, scope: str, name: str, spec: Any, wrapped: bool) -> dict | None:
        entry = self._entry(scope, name, spec)
        if entry is None or entry.transport != "stdio":
            return None  # remote servers are watched, never wrapped; malformed ones are left alone
        command, args = spec["command"], list(spec.get("args") or [])
        real = unwrap_command(command, args)
        if wrapped and real is None:
            new_command, new_args = wrap_command(entry.sid, command, args)
            return {**spec, "command": new_command, "args": new_args}
        if not wrapped and real is not None:
            return {**spec, "command": real[0], "args": real[1]}
        return None


def _strip_jsonc(raw: str) -> str:
    """Remove // and /* */ comments (outside strings) and trailing commas."""
    out, i, in_string = [], 0, False
    while i < len(raw):
        ch = raw[i]
        if in_string:
            out.append(ch)
            if ch == "\\":
                out.append(raw[i + 1:i + 2])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif raw.startswith("//", i):
            i = raw.find("\n", i)
            i = len(raw) if i < 0 else i
            continue
        elif raw.startswith("/*", i):
            end = raw.find("*/", i + 2)
            i = len(raw) if end < 0 else end + 2
            continue
        else:
            out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _indent(raw: str) -> int | None:
    lines = raw.splitlines()
    if len(lines) < 2:
        return None if raw.strip() else 2
    second = lines[1]
    return len(second) - len(second.lstrip(" ")) or 2


class ClaudeCodeAdapter(JsonAdapter):
    """~/.claude.json: user scope at the top, local (per-project) scope under projects.
    Project-scope .mcp.json files are shared through git and are never rewritten."""

    def __init__(self) -> None:
        super().__init__("claude-code", "Claude Code", ".claude.json")

    def sections(self, doc: dict) -> Iterator[Section]:
        yield from super().sections(doc)
        for project, settings in (doc.get("projects") or {}).items():
            servers = settings.get("mcpServers") if isinstance(settings, dict) else None
            if isinstance(servers, dict) and servers:
                yield f"local:{project}", servers


def json_adapters() -> list[JsonAdapter]:
    return [
        ClaudeCodeAdapter(),
        JsonAdapter("claude-desktop", "Claude Desktop", ".config/Claude/claude_desktop_config.json"),
        JsonAdapter("cursor", "Cursor", ".cursor/mcp.json"),
        JsonAdapter("windsurf", "Windsurf", ".codeium/windsurf/mcp_config.json"),
        JsonAdapter("antigravity", "Antigravity", ".gemini/antigravity/mcp_config.json"),
        JsonAdapter("gemini", "Gemini CLI", ".gemini/settings.json"),
        JsonAdapter("vscode", "VS Code", ".config/Code/User/mcp.json", servers_key="servers"),
    ]


Factory = Callable[[], list[Adapter]]
