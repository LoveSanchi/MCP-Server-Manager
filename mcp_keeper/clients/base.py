"""What every client adapter shares: the server entry model and wrap/unwrap of a command."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from mcp_keeper import paths

SHIM_NAME = "mcp-keeper-shim"


@dataclass(frozen=True)
class ServerEntry:
    client: str            # adapter key, e.g. "claude-code"
    client_label: str      # e.g. "Claude Code"
    scope: str             # "user", or "local:/path/to/project"
    name: str
    transport: str         # "stdio" | "http" | "sse"
    command: tuple[str, ...] = ()        # the real server command (already unwrapped)
    url: str = ""
    wrapped: bool = False
    config_path: str = ""
    env: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)  # may hold secrets

    @property
    def sid(self) -> str:
        return paths.server_id(self.client, self.scope, self.name)

    def public(self) -> dict[str, Any]:
        """Safe to show or send: no env, no command line (both can hold credentials)."""
        return {"id": self.sid, "client": self.client, "client_label": self.client_label,
                "scope": self.scope, "name": self.name, "transport": self.transport,
                "wrapped": self.wrapped, "config_path": self.config_path,
                "program": os.path.basename(self.command[0]) if self.command else "",
                "url": _host_only(self.url)}


    def detail(self) -> dict[str, Any]:
        """The full definition for the "Show config" view, with secrets masked: passwords in
        the command line and URL are redacted and env values are never shown, only names."""
        from mcp_keeper import redact
        return {**self.public(),
                "command": [redact.text(part) for part in self.command],
                "command_line": redact.text(" ".join(_quote(p) for p in self.command)),
                "launched_as": ([paths.shim_path(), "--id", self.sid, "--", "<command above>"] if self.wrapped else []),
                "env_names": sorted(self.env),
                "full_url": redact.text(_without_query(self.url))}


def _quote(part: str) -> str:
    return part if part and all(c.isalnum() or c in "/._-=:@,+%" for c in part) else repr(part)


def _without_query(url: str) -> str:
    return url.split("?", 1)[0] + ("?…" if "?" in url else "")


def _host_only(url: str) -> str:
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    host = re.split(r"[/?#]", rest, maxsplit=1)[0].rsplit("@", 1)[-1]
    return f"{scheme}://{host}"


def is_shim(command: str) -> bool:
    return os.path.basename(command) == SHIM_NAME


def unwrap_command(command: str, args: list[str]) -> tuple[str, list[str]] | None:
    """(real command, real args) if this command line is our shim, else None."""
    if not is_shim(command) or "--" not in args:
        return None
    rest = args[args.index("--") + 1:]
    return (rest[0], rest[1:]) if rest else None


def wrap_command(sid: str, command: str, args: list[str]) -> tuple[str, list[str]]:
    return paths.shim_path(), ["--id", sid, "--", command, *args]


class Adapter:
    """One MCP client's config. Subclasses implement load/save of their own format."""

    key = ""
    label = ""

    def config_files(self) -> list[Path]:
        raise NotImplementedError

    def entries(self) -> list[ServerEntry]:
        raise NotImplementedError

    def set_wrapped(self, names: set[str] | None, wrapped: bool) -> list[str]:
        """Wrap (or unwrap) the named stdio servers (all when None); returns the names changed."""
        raise NotImplementedError

    def present(self) -> bool:
        return any(p.exists() for p in self.config_files())
