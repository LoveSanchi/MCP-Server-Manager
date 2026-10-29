"""All supported MCP clients."""

from __future__ import annotations

from mcp_keeper.clients.base import Adapter, ServerEntry
from mcp_keeper.clients.codex import CodexAdapter
from mcp_keeper.clients.json_clients import json_adapters


def all_adapters() -> list[Adapter]:
    return [*json_adapters(), CodexAdapter()]


def inventory(adapters: list[Adapter] | None = None) -> tuple[list[ServerEntry], list[str]]:
    """Every configured server across installed clients, plus per-client read problems."""
    entries, problems = [], []
    for adapter in adapters if adapters is not None else all_adapters():
        if not adapter.present():
            continue
        try:
            entries.extend(adapter.entries())
            problems.extend(getattr(adapter, "skipped", []))
        except Exception as exc:  # noqa: BLE001 - one unreadable config must not hide the others
            problems.append(f"{adapter.label}: cannot read config: {exc}")
    return entries, problems


def set_wrapped_everywhere(wrapped: bool, client: str | None = None, names: set[str] | None = None,
                           adapters: list[Adapter] | None = None) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Wrap or unwrap across clients, one client at a time: a failure in one config never
    stops the others. Returns (changed per client, error per client)."""
    changed, failed = {}, {}
    for adapter in adapters if adapters is not None else all_adapters():
        if client and adapter.key != client or not adapter.present():
            continue
        try:
            done = adapter.set_wrapped(names, wrapped)
        except Exception as exc:  # noqa: BLE001 - reported per client
            failed[adapter.label] = str(exc)
            continue
        if done:
            changed[adapter.label] = done
    return changed, failed


__all__ = ["Adapter", "ServerEntry", "all_adapters", "inventory", "set_wrapped_everywhere"]
