"""The mcp-keeper service: collects shim reports, serves the CLI/indicator, and keeps
the inventory and remote checks fresh. Runs as a systemd --user unit."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import signal
import sys
from pathlib import Path

from mcp_keeper import paths
from mcp_keeper.registry import Registry
from mcp_keeper.service import Service

INVENTORY_SECONDS = 30
REMOTE_SECONDS = 300
MAX_LINE = 4 * 1024 * 1024
EXIT_CONFIG = 78
log = logging.getLogger("mcp-keeper")


class Hub:
    """Accepts shim connections; each one reports state/events and can be told to restart."""

    def __init__(self, registry: Registry):
        self._reg = registry
        self._writers: dict[int, asyncio.StreamWriter] = {}
        self._ids = itertools.count(1)

    def send_restart(self, conn: int, reason: str) -> bool:
        writer = self._writers.get(conn)
        if writer is None:
            return False
        writer.write((json.dumps({"type": "restart", "reason": reason}) + "\n").encode())
        return True

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = next(self._ids)
        self._writers = {**self._writers, conn: writer}
        try:
            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(message, dict):
                    try:
                        self._on_message(conn, message)
                    except Exception:  # noqa: BLE001 - one bad report must not drop the connection
                        log.exception("bad report from shim connection %d", conn)
        except (ConnectionError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            self._writers = {k: v for k, v in self._writers.items() if k != conn}
            self._reg.instance_gone(conn)
            writer.close()

    def _on_message(self, conn: int, message: dict) -> None:
        kind = message.pop("type", "")
        if kind in ("hello", "state"):
            self._reg.instance_state(conn, message)
        elif kind == "event":
            self._reg.add_event(str(message.get("id", "?")), str(message.get("level", "info")),
                                str(message.get("text", "")), message.get("ts"))


async def _serve_ctl(service: Service, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), 30)
        try:
            reply = await service.handle(json.loads(line))
        except ValueError:
            reply = {"ok": False, "data": None, "error": "request is not valid JSON"}
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()
    except (asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()


async def _unix_server(handler, path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        try:
            _, w = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 1)
            w.close()
            raise OSError(f"another mcp-keeper is already listening on {path}")
        except (ConnectionError, FileNotFoundError, asyncio.TimeoutError):
            path.unlink(missing_ok=True)
    old = os.umask(0o177)  # owner-only: these sockets can restart servers and edit configs
    try:
        return await asyncio.start_unix_server(handler, str(path), limit=MAX_LINE)
    finally:
        os.umask(old)


async def _every(seconds: float, work) -> None:
    while True:
        await asyncio.sleep(seconds)
        try:
            await work()
        except Exception:  # noqa: BLE001 - a failed refresh is retried next time
            log.exception("periodic task failed")


async def run() -> int:
    registry = Registry()
    hub = Hub(registry)
    service = Service(registry, hub.send_restart)
    service.reload_inventory()
    try:
        hub_server = await _unix_server(hub.serve, paths.hub_socket())
        ctl_server = await _unix_server(lambda r, w: _serve_ctl(service, r, w), paths.ctl_socket())
    except OSError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    log.info("watching %d configured MCP server(s)", len(registry.entries()))

    async def reload() -> None:
        await asyncio.to_thread(service.reload_inventory)  # a big ~/.claude.json must not stall the loop

    tasks = [asyncio.create_task(service.check_remotes()),
             asyncio.create_task(_every(INVENTORY_SECONDS, reload)),
             asyncio.create_task(_every(REMOTE_SECONDS, service.check_remotes))]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    loop.add_signal_handler(signal.SIGHUP, service.reload_inventory)
    await stop.wait()
    for task in tasks:
        task.cancel()
    for server, path in ((hub_server, paths.hub_socket()), (ctl_server, paths.ctl_socket())):
        server.close()
        path.unlink(missing_ok=True)
    log.info("stopped")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
