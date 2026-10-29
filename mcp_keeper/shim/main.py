"""mcp-keeper-shim --id CLIENT:SCOPE:NAME [--cwd DIR] -- COMMAND [ARGS...]

What an MCP client starts instead of the real server (mcp-keeper wraps the client's
config). Speaks MCP on stdin/stdout to the client and runs COMMAND behind it."""

from __future__ import annotations

import asyncio
import os
import signal
import sys

from mcp_keeper.shim.hub import HubLink
from mcp_keeper.shim.journal import Journal
from mcp_keeper.shim.proxy import Shim

USAGE = "usage: mcp-keeper-shim --id CLIENT:SCOPE:NAME [--cwd DIR] -- COMMAND [ARGS...]"


def parse(argv: list[str]) -> tuple[str, str | None, list[str]]:
    if "--" not in argv:
        raise SystemExit(USAGE)
    split = argv.index("--")
    options, command = argv[:split], argv[split + 1:]
    opts = dict(zip(options[::2], options[1::2]))
    if not command or "--id" not in opts or len(options) % 2:
        raise SystemExit(USAGE)
    return opts["--id"], opts.get("--cwd"), command


async def run(sid: str, cwd: str | None, command: list[str]) -> int:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout.buffer)
    writer = asyncio.StreamWriter(transport, protocol, None, loop)

    journal = Journal(sid)

    async def drain_client() -> None:
        try:
            await asyncio.wait_for(writer.drain(), 30)
        except (ConnectionError, RuntimeError, asyncio.TimeoutError):
            stop.set()  # the client stopped reading: nothing left to serve

    stop = asyncio.Event()
    shim = Shim(sid, command, os.environ, writer.write, journal=journal, cwd=cwd, drain_client=drain_client)

    async def on_command(cmd: dict) -> None:
        if cmd.get("type") == "restart":
            await shim.restart(str(cmd.get("reason") or "requested from mcp-keeper"))

    hub = HubLink(shim.snapshot, on_command)
    journal.attach(hub)
    hub.start()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)

    async def pump_client() -> None:
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:  # a message beyond the size limit: reject it, keep the session
                    journal.event("error", "dropped a client message larger than 64 MiB")
                    continue
                if not line:
                    break
                await shim.on_client_line(line)
        finally:
            stop.set()  # the client went away (or the pump failed): so do we

    await shim.start()
    pump = asyncio.create_task(pump_client())
    await stop.wait()
    pump.cancel()
    await shim.close()
    try:
        await asyncio.wait_for(writer.drain(), 5)  # deliver the last responses before exiting
    except (ConnectionError, RuntimeError, asyncio.TimeoutError):
        pass
    await hub.stop()
    journal.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    sid, cwd, command = parse(sys.argv[1:] if argv is None else argv)
    return asyncio.run(run(sid, cwd, command))


if __name__ == "__main__":
    sys.exit(main())
