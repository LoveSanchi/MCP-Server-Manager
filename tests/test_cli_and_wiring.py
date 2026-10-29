import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from mcp_keeper import cli, client, daemon, paths
from mcp_keeper.shim import main as shim_main
from mcp_keeper.shim.hub import HubLink

FAKE = str(Path(__file__).parent / "fake_server.py")


class Recorder:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        reply = self.replies[cmd]
        if isinstance(reply, Exception):
            raise reply
        return reply


STATUS = {"summary": {"total": 3, "up": 1, "down": 1, "idle": 1, "unmanaged": 0, "reachable": 0,
                      "problems": ["Cursor: cannot read config"]},
          "rows": [{"client_label": "Claude Code", "name": "orders-db", "state": "down", "detail": "tunnel down",
                    "since": 1, "instances": 2},
                   {"client_label": "Claude Code", "name": "gitlab", "state": "up", "detail": "ready",
                    "since": 0, "instances": 1},
                   {"client_label": "Codex", "name": "notes", "state": "idle", "detail": "no client", "instances": 0}]}


def test_status_output_and_exit_code(capsys):
    assert cli.main(["status"], Recorder({"status": STATUS})) == cli.EXIT_PROBLEM
    out = capsys.readouterr().out
    assert out.startswith("1/3 up · 1 down · 1 idle")
    assert "! Cursor: cannot read config" in out and "✕ orders-db" in out and "×2" in out and "\nCodex" in out
    assert cli.main(["status", "--json"], Recorder({"status": STATUS})) == cli.EXIT_PROBLEM
    assert json.loads(capsys.readouterr().out)["summary"]["total"] == 3


def test_restart_test_logs_refresh(capsys):
    rec = Recorder({"restart": {"servers": ["a"], "instances": 2}, "test": {"ok": False, "detail": "boom"},
                    "events": [{"seq": 1, "ts": 0, "level": "info", "id": "c:u:db", "text": "ready"}],
                    "refresh": STATUS})
    assert cli.main(["restart", "c:u:db"], rec) == 0 and rec.calls[-1] == ("restart", {"id": "c:u:db"})
    assert cli.main(["restart", "--all-red"], rec) == 0 and rec.calls[-1] == ("restart", {"all_red": True})
    assert cli.main(["test", "c:u:db"], rec) == cli.EXIT_PROBLEM
    assert cli.main(["logs", "c:u:db"], rec) == 0
    assert cli.main(["refresh"], rec) == 0
    out = capsys.readouterr().out
    assert "2 running instance(s)" in out and "boom" in out and "db" in out and "ready" in out


def test_service_errors_map_to_exit_codes(capsys):
    assert cli.main(["status"], Recorder({"status": client.ServiceUnavailable("down")})) == cli.EXIT_NO_SERVICE
    assert cli.main(["status"], Recorder({"status": client.ServiceError("bad")})) == cli.EXIT_PROBLEM


def test_list_wrap_backups_restore_against_real_files(capsys, monkeypatch):
    home = Path(os.environ["HOME"])
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "db": {"command": "node", "args": ["x"]}, "web": {"type": "http", "url": "https://h/mcp"}}}))
    monkeypatch.setattr(client, "request", lambda *a, **k: (_ for _ in ()).throw(client.ServiceUnavailable("x")))
    assert cli.main(["list"], Recorder({})) == 0
    assert "NOT wrapped" in capsys.readouterr().out
    assert cli.main(["wrap", "--dry-run"], Recorder({})) == 0
    assert "would wrap: Claude Code / db" in capsys.readouterr().out
    assert cli.main(["wrap", "--client", "claude-code"], Recorder({})) == 0
    assert "wrapped: Claude Code / db" in capsys.readouterr().out
    assert cli.main(["wrap"], Recorder({})) == 0
    assert "nothing to change" in capsys.readouterr().out
    assert cli.main(["backups"], Recorder({})) == 0
    stamp = capsys.readouterr().out.split()[0]
    assert cli.main(["restore", stamp], Recorder({})) == cli.EXIT_PROBLEM      # needs --force
    assert "use 'mcp-keeper unwrap'" in capsys.readouterr().err
    assert cli.main(["restore", stamp, "--force"], Recorder({})) == 0
    assert json.loads((home / ".claude.json").read_text())["mcpServers"]["db"]["command"] == "node"
    assert cli.main(["restore", "nope", "--force"], Recorder({})) == cli.EXIT_PROBLEM


def test_client_against_a_live_service_and_failure_modes(tmp_path):
    async def scenario():
        task = asyncio.create_task(daemon.run())
        for _ in range(100):
            if paths.ctl_socket().exists():
                break
            await asyncio.sleep(0.02)
        data = await asyncio.to_thread(client.request, "status")
        assert "summary" in data
        with pytest.raises(client.ServiceError, match="unknown command"):
            await asyncio.to_thread(client.request, "bogus")
        second = await daemon.run()                       # a second service refuses to start
        assert second == daemon.EXIT_CONFIG
        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(task, 10) == 0
        assert not paths.ctl_socket().exists()
    asyncio.run(scenario())
    with pytest.raises(client.ServiceUnavailable):
        client.request("status")


def test_shim_argument_parsing():
    assert shim_main.parse(["--id", "a:b:c", "--", "node", "x"]) == ("a:b:c", None, ["node", "x"])
    assert shim_main.parse(["--id", "a:b:c", "--cwd", "/p", "--", "node"]) == ("a:b:c", "/p", ["node"])
    for bad in (["--id", "a"], ["--", "node"], ["--id", "a", "--"], ["--id", "--", "x"]):
        with pytest.raises(SystemExit):
            shim_main.parse(bad)


def test_hub_link_reports_and_receives_commands(tmp_path):
    async def scenario():
        got, commands = asyncio.Queue(), []

        async def fake_service(reader, writer):
            got.put_nowait(json.loads(await reader.readline()))
            writer.write(b'{"type": "restart", "reason": "test"}\nnot json\n')
            await writer.drain()
            got.put_nowait(json.loads(await reader.readline()))

        server = await asyncio.start_unix_server(fake_service, str(tmp_path / "hub.sock"))

        async def on_command(cmd):
            commands.append(cmd)

        link = HubLink(lambda: {"id": "a:b:c", "state": "up"}, on_command, sock=tmp_path / "hub.sock")
        link.send({"type": "event"})          # not connected yet: silently dropped
        link.start()
        hello = await asyncio.wait_for(got.get(), 5)
        assert hello == {"type": "hello", "id": "a:b:c", "state": "up"}
        for _ in range(100):
            if commands:
                break
            await asyncio.sleep(0.02)
        assert commands == [{"type": "restart", "reason": "test"}]
        assert link.connected
        link.send({"type": "event", "text": "x"})
        assert (await asyncio.wait_for(got.get(), 5))["text"] == "x"
        await link.stop()
        server.close()
    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_shim_main_runs_until_client_eof(tmp_path):
    """The real entry point, in-process, with pipes standing in for the client."""
    async def scenario():
        rfd, wfd = os.pipe()
        out_r, out_w = os.pipe()
        os.write(wfd, b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n')
        stdin, stdout = open(rfd, "rb", buffering=0), open(out_w, "wb", buffering=0)
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin = type("S", (), {"buffer": stdin})()
        sys.stdout = type("S", (), {"buffer": stdout})()
        try:
            task = asyncio.create_task(shim_main.run("t:user:fake", None, [sys.executable, FAKE]))
            reply = await asyncio.wait_for(asyncio.to_thread(os.read, out_r, 65536), 15)
            os.close(wfd)  # the client quits
            code = await asyncio.wait_for(task, 15)
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        assert code == 0 and json.loads(reply.decode().splitlines()[0])["result"]["serverInfo"]["name"] == "fake"
    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_show_output(capsys):
    detail = {"id": "claude-code:user:db", "client_label": "Claude Code", "scope": "user", "config_path": "/h/.claude.json",
              "transport": "stdio", "command_line": "node x --dsn mysql://u:***@h/db", "wrapped": True,
              "env_names": ["TOKEN"], "full_url": ""}
    assert cli.main(["show", "claude-code:user:db"], Recorder({"show": detail})) == 0
    out = capsys.readouterr().out
    assert "mysql://u:***@h/db" in out and "TOKEN   (values hidden)" in out and "mcp-keeper-shim" in out
    remote = {**detail, "transport": "http", "full_url": "https://h/mcp", "env_names": []}
    cli.main(["show", "x"], Recorder({"show": remote}))
    assert "url        https://h/mcp" in capsys.readouterr().out
