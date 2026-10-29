import asyncio
import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

from mcp_keeper import probes, redact
from mcp_keeper.clients.base import ServerEntry
from mcp_keeper.registry import Registry
from mcp_keeper.service import Service

FAKE = str(Path(__file__).parent / "fake_server.py")


def entry(name, transport="stdio", wrapped=True, url="", command=(sys.executable, FAKE), env=None):
    return ServerEntry("claude-code", "Claude Code", "user", name, transport,
                       command if transport == "stdio" else (), url, wrapped, "/x/.claude.json", env or {})


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


# ── registry ──────────────────────────────────────────────────────────────────
def test_rows_merge_inventory_instances_and_remote():
    reg = Registry()
    reg.set_inventory([entry("db"), entry("idle"), entry("raw", wrapped=False),
                       entry("jira", transport="http", url="https://mcp.example.com/v1")], ["Cursor: broken"])
    reg.instance_state(1, {"id": "claude-code:user:db", "state": "up", "detail": "ready", "client": "claude-code",
                           "tools": 3, "restarts": 1, "requests": 5, "errors": 0, "since": 1})
    reg.instance_state(2, {"id": "claude-code:user:db", "state": "down", "detail": "exited", "client": "codex",
                           "tools": 3, "restarts": 2, "requests": 1, "errors": 1, "since": 2})
    reg.set_remote("claude-code:user:jira", "reachable", "login handled by the client")
    rows = {r["name"]: r for r in reg.rows()}
    assert rows["db"]["state"] == "down" and rows["db"]["instances"] == 2      # worst instance wins
    assert rows["db"]["restarts"] == 3 and rows["db"]["clients"] == ["claude-code", "codex"]
    assert rows["idle"]["state"] == "idle" and rows["raw"]["state"] == "unmanaged"
    assert rows["jira"]["state"] == "reachable"
    summary = reg.summary()
    assert (summary["total"], summary["up"], summary["down"], summary["idle"]) == (4, 0, 1, 1)
    assert summary["problems"] == ["Cursor: broken"]
    reg.instance_gone(2)
    assert {r["name"]: r for r in reg.rows()}["db"]["state"] == "up"
    assert reg.summary()["up"] == 1


def test_events_are_redacted_filtered_and_paged():
    reg = Registry()
    reg.add_event("a", "info", "connecting to mysql://u:secret@h/db")
    reg.add_event("b", "error", "boom")
    all_events = reg.events()
    assert "secret" not in all_events[0]["text"]
    assert [e["id"] for e in reg.events(sid="b")] == ["b"]
    assert reg.events(since=all_events[-1]["seq"]) == []


def test_orphan_instance_is_still_shown():
    reg = Registry()
    reg.instance_state(1, {"id": "cursor:user:gone", "state": "up", "detail": "ready"})
    (row,) = reg.rows()
    assert row["name"] == "gone" and row["client"] == "cursor" and row["state"] == "up"


# ── service ───────────────────────────────────────────────────────────────────
class FakeAdapter:
    key, label = "claude-code", "Claude Code"

    def __init__(self):
        self.calls = []

    def present(self):
        return True

    def set_wrapped(self, names, wrapped):
        self.calls.append((names, wrapped))
        return ["db"]


def make_service(reg, sent=None, adapter=None):
    sent = sent if sent is not None else []

    async def remote(e, path=""):
        return ("up", "answered (HTTP 200)")

    return Service(reg, lambda conn, why: sent.append(conn) or True,
                   load_inventory=lambda: ([entry("db"), entry("jira", "http", url="https://x")], []),
                   adapters=lambda: [adapter or FakeAdapter()], check_remote=remote)


def test_service_status_restart_and_errors():
    async def scenario():
        reg, sent = Registry(), []
        svc = make_service(reg, sent)
        svc.reload_inventory()
        reg.instance_state(7, {"id": "claude-code:user:db", "state": "down", "detail": "x"})
        assert (await svc.handle({"cmd": "restart", "id": "claude-code:user:db"}))["data"]["instances"] == 1
        assert sent == [7]
        red = await svc.handle({"cmd": "restart", "all_red": True})
        assert red["data"]["servers"] == ["claude-code:user:db"]
        assert "no client is running" in (await svc.handle({"cmd": "restart", "id": "claude-code:user:jira"}))["error"]
        assert (await svc.handle({"cmd": "nope"}))["error"] == "unknown command 'nope'"
        assert "'id' is required" in (await svc.handle({"cmd": "test"}))["error"]
        assert (await svc.handle([1]))["ok"] is False
        status = (await svc.handle({"cmd": "status"}))["data"]
        assert status["summary"]["total"] == 2
    run(scenario())


def test_service_refresh_checks_remotes_and_test_remote():
    async def scenario():
        reg = Registry()
        svc = make_service(reg)
        data = (await svc.handle({"cmd": "refresh"}))["data"]
        jira = next(r for r in data["rows"] if r["name"] == "jira")
        assert jira["state"] == "up"
        tested = (await svc.handle({"cmd": "test", "id": "claude-code:user:jira"}))["data"]
        assert tested["ok"] is True
        assert "no configured server" in (await svc.handle({"cmd": "test", "id": "x:y:z"}))["error"]
    run(scenario())


def test_service_wrap_unwrap_validates_names():
    async def scenario():
        adapter = FakeAdapter()
        svc = make_service(Registry(), adapter=adapter)
        out = (await svc.handle({"cmd": "wrap", "names": ["db"]}))["data"]
        assert out["changed"] == {"Claude Code": ["db"]} and out["failed"] == {}
        assert adapter.calls == [({"db"}, True)]
        await svc.handle({"cmd": "unwrap", "client": "claude-code"})
        assert adapter.calls[-1] == (None, False)
        assert "must be a list" in (await svc.handle({"cmd": "wrap", "names": "db"}))["error"]
        assert "'stamp' is required" in (await svc.handle({"cmd": "restore"}))["error"]
        assert "send force=true" in (await svc.handle({"cmd": "restore", "stamp": "x"}))["error"]
        assert "no backup named" in (await svc.handle({"cmd": "restore", "stamp": "x", "force": True}))["error"]
        assert (await svc.handle({"cmd": "backups"}))["data"] == []
        assert "must be numbers" in (await svc.handle({"cmd": "events", "since": "x"}))["error"]
    run(scenario())


# ── probes ────────────────────────────────────────────────────────────────────
def test_probe_stdio_ok_failing_and_missing(tmp_path):
    ok = run(probes.test_stdio(entry("fake")))
    assert ok["ok"] and ok["tools"] == 2 and ok["server"] == "fake 1.0"
    fail = tmp_path / "down"
    fail.touch()
    bad = run(probes.test_stdio(entry("fake", env={"FAKE_FAIL_FILE": str(fail)})))
    assert not bad["ok"] and "cannot connect to database" in bad["detail"]
    hang = run(probes.test_stdio(entry("fake", env={"FAKE_NO_INIT": "1"}), timeout=1))
    assert not hang["ok"] and "within 1s" in hang["detail"]
    missing = run(probes.test_stdio(entry("x", command=("/nonexistent/bin",))))
    assert "cannot start" in missing["detail"]


@pytest.fixture
def http_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            code = 401 if self.path == "/auth" else 404 if self.path == "/missing" else 200
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_remote_check_states(http_server):
    def check(path, transport="http"):
        return run(probes.check_remote(entry("r", transport, url=http_server + path)))
    assert check("/mcp") == ("up", "answered (HTTP 200)")
    assert check("/auth")[0] == "reachable" and "only the client holds that login" in check("/auth")[1]
    assert check("/missing") == ("reachable", "reachable but answered HTTP 404")
    assert check("/sse", "sse")[0] == "up"
    state, detail = run(probes.check_remote(entry("r", "http", url="http://127.0.0.1:1/mcp")))
    assert state == "down" and "unreachable" in detail


# ── redaction ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, leaked", [
    ("mysql://reader:hunter2@db:3306/x", "hunter2"),
    ("PASSWORD=hunter2 next", "hunter2"),
    ('api_key: "abc123def"', "abc123def"),
    ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
    ("token glpat-ABCDEFGHIJKLMNOPQRST used", "glpat-ABCDEFGHIJKLMNOPQRST"),
])
def test_redact(raw, leaked):
    assert leaked not in redact.text(raw)


def test_redact_keeps_ordinary_text():
    assert redact.text("server ready on port 8080") == "server ready on port 8080"


def test_reports_are_validated():
    reg = Registry()
    reg.instance_state(1, {"id": ["x"]})
    reg.instance_state(2, "nope")
    reg.instance_state(3, {"id": "a:b:c", "state": "up", "tools": "lots", "since": "x", "detail": "pw=hunter2",
                           "path": "/opt/bin:/usr/bin"})
    (row,) = reg.rows()
    assert row["tools"] == 0 and "hunter2" not in row["detail"]
    assert reg.client_path("a:b:c") == "/opt/bin:/usr/bin" and reg.client_path("other") == "/opt/bin:/usr/bin"


def test_probe_uses_the_clients_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "my-server"
    script.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE}\n")
    script.chmod(0o755)
    e = entry("p", command=("my-server",))
    assert "cannot start" in run(probes.test_stdio(e))["detail"]
    assert run(probes.test_stdio(e, path=f"{bin_dir}:/usr/bin:/bin"))["ok"]


@pytest.mark.parametrize("raw, leaked", [
    ('{"api_key":"abc123secret"}', "abc123secret"),
    ("--password hunter2", "hunter2"),
    ("postgres://u:p@ss@db/x", "ss@db"[:2] + "@"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk", "eyJzdWIi"),
    ("key AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
])
def test_redact_more_shapes(raw, leaked):
    assert leaked not in redact.text(raw)


@pytest.mark.parametrize("output, expected", [
    ("atlassian:\n  Scope: User config\n  Status: ✔ Connected\n  Type: http", ("up", "connected in Claude Code")),
    ("  Status: ! Needs authentication", ("down", "Claude Code needs you to log in to it (run /mcp in Claude Code)")),
    ("  Status: ✘ Failed to connect", ("down", "Claude Code reports: Failed to connect")),
    ("no status line here", None),
])
def test_claude_status_parsing(output, expected):
    assert probes._claude_status(output) == expected


def test_remote_check_asks_claude_code_first(tmp_path, http_server):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\necho \"$3:\"\necho '  Status: ✔ Connected'\n")
    fake.chmod(0o755)
    claude_entry = entry("jira", "http", url=http_server + "/auth")
    assert run(probes.check_remote(claude_entry, path=f"{bin_dir}:/usr/bin:/bin")) == ("up", "connected in Claude Code")
    codex_entry = ServerEntry("codex", "Codex", "user", "jira", "http", (), http_server + "/auth")
    assert run(probes.check_remote(codex_entry, path=f"{bin_dir}:/usr/bin:/bin"))[0] == "reachable"
    no_claude = run(probes.check_remote(claude_entry, path="/nonexistent"))
    assert no_claude[0] == "reachable"  # falls back to the HTTP check


def test_reachable_remote_test_passes():
    async def scenario():
        async def remote(e, path=""):
            return ("reachable", "server is up and asks for a login (HTTP 403); only the client holds that login")
        svc = Service(Registry(), lambda c, w: True, load_inventory=lambda: ([entry("jira", "http", url="https://x")], []),
                      check_remote=remote)
        svc.reload_inventory()
        result = (await svc.handle({"cmd": "test", "id": "claude-code:user:jira"}))["data"]
        assert result["ok"] is True
    run(scenario())


def test_show_masks_secrets():
    async def scenario():
        db = ServerEntry("claude-code", "Claude Code", "user", "db", "stdio",
                         ("/usr/bin/node", "/opt/orders-db", "--dsn", "mysql://ro:hunter2@127.0.0.1:3306/orders"),
                         "", True, "/home/u/.claude.json", {"API_TOKEN": "s3cret", "HOST": "h"})
        web = ServerEntry("claude-code", "Claude Code", "user", "web", "http", (),
                          "https://u:pw@mcp.example.com/v1/mcp?key=abc", False, "/home/u/.claude.json")
        svc = Service(Registry(), lambda c, w: True, load_inventory=lambda: ([db, web], []))
        svc.reload_inventory()
        d = (await svc.handle({"cmd": "show", "id": "claude-code:user:db"}))["data"]
        dumped = json.dumps(d)
        assert "hunter2" not in dumped and "s3cret" not in dumped
        assert d["command_line"] == "/usr/bin/node /opt/orders-db --dsn mysql://ro:***@127.0.0.1:3306/orders"
        assert d["env_names"] == ["API_TOKEN", "HOST"] and d["launched_as"][1:3] == ["--id", "claude-code:user:db"]
        w = (await svc.handle({"cmd": "show", "id": "claude-code:user:web"}))["data"]
        assert w["full_url"] == "https://u:***@mcp.example.com/v1/mcp?…"
        assert "no configured server" in (await svc.handle({"cmd": "show", "id": "x:y:z"}))["error"]
    run(scenario())
