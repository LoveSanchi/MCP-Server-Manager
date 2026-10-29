"""The shim's line to the mcp-keeper service. Optional by design: when the service is
not running the shim still proxies, and it keeps trying to reconnect in the background."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp_keeper import paths

RECONNECT_SECONDS = 5.0
log = logging.getLogger("mcp-keeper-shim")

Command = Callable[[dict[str, Any]], Awaitable[None]]


class HubLink:
    def __init__(self, snapshot: Callable[[], dict[str, Any]], on_command: Command, sock: Path | None = None):
        self._snapshot = snapshot
        self._on_command = on_command
        self._sock = sock or paths.hub_socket()
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._writer:
            self._writer.close()

    @property
    def connected(self) -> bool:
        return self._writer is not None

    def send(self, message: dict[str, Any]) -> None:
        """Fire and forget: never let a slow or absent service hold up MCP traffic."""
        if self._writer is None:
            return
        try:
            self._writer.write((json.dumps(message) + "\n").encode())
        except (ConnectionError, RuntimeError):
            self._writer = None

    async def _run(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(str(self._sock))
            except OSError:
                await asyncio.sleep(RECONNECT_SECONDS)
                continue
            self._writer = writer
            self.send({"type": "hello", **self._snapshot()})
            try:
                while line := await reader.readline():
                    try:
                        command = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(command, dict):
                        await self._on_command(command)
            except (ConnectionError, OSError):
                pass
            finally:
                self._writer = None
                writer.close()
            await asyncio.sleep(RECONNECT_SECONDS)
