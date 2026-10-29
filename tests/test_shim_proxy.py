import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from mcp_keeper.shim.cache import Cache
from mcp_keeper.shim.journal import Journal
from mcp_keeper.shim.proxy import DOWN, UP, Shim, Timings

FAKE = str(Path(__file__).parent / "fake_server.py")
FAST = Timings(client_init_wait=1.0, handshake_timeout=1.0, ping_interval=0.2, ping_timeout=0.2,
               ping_misses=2, backoff=(0.1, 0.2, 0.3), stable_after=60)


class Harness:
    """Plays the MCP client: feeds lines to the shim and collects what it writes back."""

    def __init__(self, tmp_path, env=None, timings=FAST):
        self.out: asyncio.Queue = asyncio.Queue()
        self.journal = Journal("test:user:fake", log_path=tmp_path / "fake.log")
        self.cache = Cache("test:user:fake", path=tmp_path / "cache.json")
        self.shim = Shim("test:user:fake", [sys.executable, FAKE], {**env_base(), **(env or {})},
                         lambda data: self.out.put_nowait(json.loads(data)), cache=self.cache,
                         journal=self.journal, timings=timings)
        self._ids = 0

    async def send(self, message):
        await self.shim.on_client_line((json.dumps(message) + "\n").encode())

    async def request(self, method, params=None):
        self._ids += 1
        await self.send({"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params or {}})
        return self._ids

    async def reply_to(self, rid, timeout=3.0):
        while True:
            message = await asyncio.wait_for(self.out.get(), timeout)
            if message.get("id") == rid and "method" not in message:
                return message

    async def next_message(self, timeout=3.0):
        return await asyncio.wait_for(self.out.get(), timeout)

    async def initialize(self):
        rid = await self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "pytest", "version": "1"}})
        result = await self.reply_to(rid)
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    async def call(self, tool, **args):
        rid = await self.request("tools/call", {"name": tool, "arguments": args})
        return await self.reply_to(rid)

    async def wait_state(self, state, timeout=5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while self.shim.snapshot()["state"] != state:
            assert asyncio.get_running_loop().time() < deadline, self.shim.snapshot()
            await asyncio.sleep(0.02)


def env_base():
    return {"PATH": os.environ["PATH"]}


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def test_passthrough_and_announced_list_changed(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.shim.start()
        init = await h.initialize()
        assert init["result"]["serverInfo"]["name"] == "fake"
        assert init["result"]["capabilities"]["tools"]["listChanged"] is True  # promised so restarts re-fetch
        tools = await h.reply_to(await h.request("tools/list"))
        assert [t["name"] for t in tools["result"]["tools"]] == ["echo", "crash"]
        assert (await h.call("echo", text="hi"))["result"]["content"][0]["text"] == "hi"
        for _ in range(50):
            if h.cache.get("tools/list"):
                break
            await asyncio.sleep(0.05)
        assert h.cache.get("tools/list")["tools"][0]["name"] == "echo"
        snap = h.shim.snapshot()
        assert snap["state"] == UP and snap["client"] == "pytest" and snap["server"] == "fake 1.0"
        await h.shim.close()
    run(scenario())


def test_crash_mid_call_fails_that_call_then_heals(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.shim.start()
        await h.initialize()
        crashed = await h.call("crash")
        assert "fake stopped" in crashed["error"]["message"] and "panic: boom" in crashed["error"]["message"]
        changed = await h.next_message()
        assert changed == {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
        assert (await h.call("echo", text="back"))["result"]["content"][0]["text"] == "back"
        assert h.shim.restarts == 1
        await h.shim.close()
    run(scenario())


def test_server_down_at_start_then_recovers_by_itself(tmp_path):
    """The orders-db case: the server exits at start because its tunnel is down."""
    async def scenario():
        fail = tmp_path / "tunnel-down"
        fail.touch()
        h = Harness(tmp_path, env={"FAKE_FAIL_FILE": str(fail)})
        h.cache.put("tools/list", {"tools": [{"name": "echo"}]})
        await h.shim.start()
        await h.wait_state(DOWN)
        init = await h.initialize()  # still a valid handshake, from the cache / minimal answer
        assert "mcp-keeper" in init["result"]["serverInfo"]["version"]
        listed = await h.reply_to(await h.request("tools/list"))
        assert listed["result"]["tools"] == [{"name": "echo"}]       # last known tools
        failed = await h.call("echo", text="x")
        assert "cannot connect to database" in failed["error"]["message"]
        assert "retry shortly" in failed["error"]["message"]
        pong = await h.reply_to(await h.request("ping"))
        assert pong["result"] == {}                                    # never looks dead to the client
        fail.unlink()                                                  # the tunnel is back
        changed = await h.next_message(timeout=5)
        assert changed["method"] == "notifications/tools/list_changed"
        assert (await h.call("echo", text="up"))["result"]["content"][0]["text"] == "up"
        await h.shim.close()
    run(scenario())


def test_cached_handshake_used_when_available(tmp_path):
    async def scenario():
        fail = tmp_path / "down"
        fail.touch()
        h = Harness(tmp_path, env={"FAKE_FAIL_FILE": str(fail)})
        h.cache.put("initialize", {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "fake", "version": "cached"}})
        await h.shim.start()
        init = await h.initialize()
        assert init["result"]["serverInfo"]["version"] == "cached"
        assert init["result"]["capabilities"]["tools"]["listChanged"] is True
        await h.shim.close()
    run(scenario())


def test_user_restart_fails_pending_and_announces(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.shim.start()
        await h.initialize()
        await h.shim.restart("clicked Restart")
        assert (await h.next_message())["method"] == "notifications/tools/list_changed"
        assert (await h.call("echo", text="ok"))["result"]["content"][0]["text"] == "ok"
        assert h.shim.restarts == 1
        assert "restarting: clicked Restart" in (tmp_path / "fake.log").read_text()
        await h.shim.close()
    run(scenario())


def test_server_to_client_requests_pass_through(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.shim.start()
        await h.initialize()
        rid = await h.request("tools/call", {"name": "ask"})
        ask = await h.next_message()
        assert ask["method"] == "roots/list" and ask["id"] == 99
        await h.send({"jsonrpc": "2.0", "id": 99, "result": {"roots": []}})
        answer = await h.reply_to(rid)
        assert json.loads(answer["result"]["content"][0]["text"]) == {"roots": []}
        await h.shim.close()
    run(scenario())


def test_error_responses_are_counted_and_logged(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.shim.start()
        await h.initialize()
        assert (await h.call("fail"))["error"]["message"] == "db said no"
        assert h.shim.errors == 1
        assert "tools/call fail failed" in (tmp_path / "fake.log").read_text()
        await h.shim.close()
    run(scenario())


def test_secrets_on_stderr_are_masked(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.shim.start()
        await h.initialize()
        await h.call("leak")
        await asyncio.sleep(0.1)
        log = (tmp_path / "fake.log").read_text()
        assert "hunter2" not in log and "mysql://reader:***@db" in log
        await h.shim.close()
    run(scenario())


def test_server_that_never_handshakes_backs_off(tmp_path):
    async def scenario():
        h = Harness(tmp_path, env={"FAKE_NO_INIT": "1"})
        await h.shim.start()
        rid = await h.request("initialize", {"protocolVersion": "2025-06-18"})
        await h.reply_to(rid)  # answered (minimal) after client_init_wait
        await h.wait_state(DOWN)
        assert "no reply to initialize" in h.shim.snapshot()["detail"]
        await h.shim.close()
    run(scenario())


def test_unanswered_pings_restart_the_server(tmp_path):
    async def scenario():
        h = Harness(tmp_path, env={"FAKE_NO_PING": "1"})
        await h.shim.start()
        await h.initialize()
        await asyncio.sleep(1.2)
        assert h.shim.restarts >= 1
        assert "no reply to 2 pings" in (tmp_path / "fake.log").read_text()
        await h.shim.close()
    run(scenario())


def test_unstartable_command(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        h.shim._argv = ["/nonexistent/server"]
        await h.shim.start()
        await h.wait_state(DOWN)
        assert "cannot start" in h.shim.snapshot()["detail"]
        await h.shim.close()
    run(scenario())


@pytest.mark.parametrize("line, reply", [
    (b"\n", None),
    (b"not json\n", "parse error"),
    (b"[1,2]\n", "JSON-RPC batches are not supported"),
    (b'{"jsonrpc":"2.0","id":[1],"method":"tools/list"}\n', "invalid request id"),
    (b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":"x"}\n', "params must be an object"),
    (b'{"jsonrpc":"2.0","id":4,"method":5}\n', "method must be a string"),
])
def test_malformed_client_messages_get_errors_never_kill_the_pump(tmp_path, line, reply):
    async def scenario():
        h = Harness(tmp_path)
        await h.shim.start()
        await h.shim.on_client_line(line)
        if reply is None:
            assert h.out.empty()
        else:
            assert (await h.next_message())["error"]["message"] == reply
        await h.initialize()  # still fully working afterwards
        assert (await h.call("echo", text="ok"))["result"]["content"][0]["text"] == "ok"
        await h.shim.close()
    run(scenario())


def test_crash_during_first_handshake_then_recovers(tmp_path):
    """C1: the replacement process must get its own handshake."""
    async def scenario():
        h = Harness(tmp_path, env={"FAKE_DIE_ON_FIRST_INIT": str(tmp_path / "died-once")})
        await h.shim.start()
        await h.initialize()                       # first child dies during this handshake
        await h.wait_state(UP, timeout=5)          # the respawned child is handshaken
        assert (await h.call("echo", text="healed"))["result"]["content"][0]["text"] == "healed"
        await h.shim.close()
    run(scenario())


def test_bad_server_messages_do_not_stop_the_pump(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.shim.start()
        await h.initialize()
        assert (await h.call("bad_error"))["error"] == "not an object"   # forwarded as-is
        assert h.shim.errors == 1
        assert (await h.call("echo", text="still"))["result"]["content"][0]["text"] == "still"
        await h.shim.close()
    run(scenario())


def test_crash_detected_even_when_a_helper_holds_the_pipes(tmp_path):
    """H2: the server dies but a helper it started keeps stdout/stderr open."""
    async def scenario():
        h = Harness(tmp_path)
        await h.shim.start()
        await h.initialize()
        started = asyncio.get_running_loop().time()
        failed = await h.call("orphan")
        assert "fake stopped" in failed["error"]["message"]
        assert asyncio.get_running_loop().time() - started < 3
        await h.shim.close()
    run(scenario())


def test_concurrent_restarts_leave_one_server(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.shim.start()
        await h.initialize()
        await asyncio.gather(h.shim.restart("a"), h.shim.restart("b"))
        await h.wait_state(UP)
        assert (await h.call("echo", text="one"))["result"]["content"][0]["text"] == "one"
        children = await asyncio.create_subprocess_exec("pgrep", "-f", "fake_server.py", "-P", str(os.getpid()),
                                                        stdout=asyncio.subprocess.PIPE)
        out, _ = await children.communicate()
        assert len(out.split()) == 1
        await h.shim.close()
    run(scenario())


def test_server_messages_before_the_handshake_answer_are_held(tmp_path):
    async def scenario():
        h = Harness(tmp_path, env={"FAKE_EARLY_NOTE": "1"})
        await h.shim.start()
        await asyncio.sleep(0.3)
        assert h.out.empty()                       # nothing reaches the client before its initialize answer
        rid = await h.request("initialize", {"protocolVersion": "2025-06-18"})
        first = await h.next_message()
        assert first["id"] == rid
        assert (await h.next_message())["method"] == "notifications/message"
        await h.shim.close()
    run(scenario())


def test_paginated_lists_are_cached_whole(tmp_path):
    async def scenario():
        h = Harness(tmp_path, env={"FAKE_PAGES": "1"})
        await h.shim.start()
        await h.initialize()
        for _ in range(50):
            if h.cache.get("tools/list"):
                break
            await asyncio.sleep(0.05)
        assert [t["name"] for t in h.cache.get("tools/list")["tools"]] == ["echo", "crash"]
        await h.shim.close()
    run(scenario())


def test_missing_handshake_fields_are_completed(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.shim.start()
        rid = await h.request("initialize", None)
        await h.send({"jsonrpc": "2.0", "id": rid, "method": "initialize"})
        await h.reply_to(rid)
        params = h.shim._init_params
        assert params["capabilities"] == {} and params["clientInfo"]["name"] == "unknown-client"
        await h.shim.close()
    run(scenario())


def test_best_reason_prefers_error_lines():
    from mcp_keeper.reasons import best_reason as _best_reason
    assert _best_reason(["Starting", "Error: connect ECONNREFUSED 127.0.0.1:3399", "  code: 1", "}"]) \
        == "Error: connect ECONNREFUSED 127.0.0.1:3399"
    assert _best_reason(["server listening", "}"]) == "server listening"
    assert _best_reason(["}", " "]) == "no output"


def test_exit_during_handshake_reports_the_real_reason(tmp_path):
    async def scenario():
        fail = tmp_path / "down"
        fail.touch()
        h = Harness(tmp_path, env={"FAKE_FAIL_FILE": str(fail)})
        await h.shim.start()
        await h.initialize()
        await asyncio.sleep(0.5)
        detail = h.shim.snapshot()["detail"]
        assert "cannot connect to database" in detail and "no reply to initialize" not in detail
        await h.shim.close()
    run(scenario())


def test_journal_rate_limits_stderr_and_logs_privately(tmp_path):
    import stat
    sent = []
    journal = Journal("a:b:c", log_path=tmp_path / "x.log")
    journal.attach(type("Hub", (), {"send": lambda self, m: sent.append(m)})())
    for i in range(50):
        journal.stderr(f"line {i}")
    journal.event("info", "done")
    assert sum(1 for m in sent if m["level"] == "stderr") <= 10
    assert (tmp_path / "x.log").read_text().count("[stderr]") == 50
    assert stat.S_IMODE((tmp_path / "x.log").stat().st_mode) == 0o600
    journal.close()


def test_safe_names_never_collide():
    from mcp_keeper import paths
    assert paths.safe_name("c:local:/p/x y:n") != paths.safe_name("c:local:/p/x_y:n")
