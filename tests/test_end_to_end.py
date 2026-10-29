"""The whole chain with real processes and sockets: service ⇄ shim process ⇄ fake server,
driven by a fake MCP client over the shim's stdin/stdout."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp_keeper import daemon, paths
from mcp_keeper.clients.base import ServerEntry
from mcp_keeper.registry import Registry
from mcp_keeper.service import Service

FAKE = str(Path(__file__).parent / "fake_server.py")
ROOT = str(Path(__file__).resolve().parents[1])
SID = "claude-code:user:fake"


async def start_service(entries):
    registry = Registry()
    hub = daemon.Hub(registry)
    service = Service(registry, hub.send_restart, load_inventory=lambda: (entries, []))
    service.reload_inventory()
    hub_srv = await daemon._unix_server(hub.serve, paths.hub_socket())
    ctl_srv = await daemon._unix_server(lambda r, w: daemon._serve_ctl(service, r, w), paths.ctl_socket())
    return registry, (hub_srv, ctl_srv)


async def ctl(cmd, **args):
    reader, writer = await asyncio.open_unix_connection(str(paths.ctl_socket()))
    writer.write((json.dumps({"cmd": cmd, **args}) + "\n").encode())
    reply = json.loads(await reader.readline())
    writer.close()
    assert reply["ok"], reply["error"]
    return reply["data"]


class Client:
    def __init__(self, proc):
        self.proc, self.ids = proc, 0

    async def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method, params=None):
        self.ids += 1
        await self.send({"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params or {}})
        while True:
            msg = json.loads(await asyncio.wait_for(self.proc.stdout.readline(), 10))
            if msg.get("id") == self.ids:
                return msg

    async def notification(self, timeout=10):
        while True:
            msg = json.loads(await asyncio.wait_for(self.proc.stdout.readline(), timeout))
            if "method" in msg and "id" not in msg:
                return msg


async def wait_for(predicate, timeout=10):
    for _ in range(int(timeout / 0.05)):
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


def test_service_shim_server_chain(tmp_path):
    async def scenario():
        entry = ServerEntry("claude-code", "Claude Code", "user", "fake", "stdio",
                            (sys.executable, FAKE), wrapped=True)
        registry, servers = await start_service([entry])
        env = {**os.environ, "PYTHONPATH": ROOT}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mcp_keeper.shim.main", "--id", SID, "--", sys.executable, FAKE,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env)
        client = Client(proc)
        init = await client.request("initialize", {"protocolVersion": "2025-06-18",
                                                   "clientInfo": {"name": "e2e-client", "version": "1"}})
        assert init["result"]["serverInfo"]["name"] == "fake"
        await client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        async def up_row():
            data = await ctl("status")
            row = next(r for r in data["rows"] if r["id"] == SID)
            return row if row["state"] == "up" and row["instances"] == 1 else None
        row = await wait_for(up_row)
        assert row["clients"] == ["e2e-client"] and row["server"] == "fake 1.0"

        result = await client.request("tools/call", {"name": "echo", "arguments": {"text": "hi"}})
        assert result["result"]["content"][0]["text"] == "hi"

        restarted = await ctl("restart", id=SID)                 # the top bar's Restart
        assert restarted["instances"] == 1
        assert (await client.notification())["method"] == "notifications/tools/list_changed"
        again = await client.request("tools/call", {"name": "echo", "arguments": {"text": "again"}})
        assert again["result"]["content"][0]["text"] == "again"

        events = await ctl("events", id=SID)
        texts = " | ".join(e["text"] for e in events)
        assert "restart requested" in texts and "tools/call echo → ok" in texts

        tested = await ctl("test", id=SID)                       # a standalone run of the same server
        assert tested["ok"] and tested["tools"] == 2

        proc.stdin.close()                                       # the client quits
        await asyncio.wait_for(proc.wait(), 10)

        async def gone():
            data = await ctl("status")
            row = next(r for r in data["rows"] if r["id"] == SID)
            return row if row["state"] == "idle" else None
        await wait_for(gone)
        for srv in servers:
            srv.close()
    asyncio.run(asyncio.wait_for(scenario(), 60))


def test_shim_works_without_the_service(tmp_path):
    async def scenario():
        env = {**os.environ, "PYTHONPATH": ROOT}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mcp_keeper.shim.main", "--id", SID, "--", sys.executable, FAKE,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env)
        client = Client(proc)
        init = await client.request("initialize", {"protocolVersion": "2025-06-18"})
        assert init["result"]["serverInfo"]["name"] == "fake"
        proc.stdin.close()
        assert await asyncio.wait_for(proc.wait(), 10) == 0
    asyncio.run(asyncio.wait_for(scenario(), 30))
