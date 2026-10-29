import json
import os
import stat
from pathlib import Path

import pytest

from mcp_keeper import backup, fsutil
from mcp_keeper.clients import inventory
from mcp_keeper.clients.base import unwrap_command, wrap_command
from mcp_keeper.clients.codex import CodexAdapter
from mcp_keeper.clients.json_clients import ClaudeCodeAdapter, JsonAdapter, json_adapters

CLAUDE = {
    "numStartups": 7,
    "mcpServers": {
        "db": {"type": "stdio", "command": "/usr/bin/node", "args": ["/opt/orders-db", "--dsn", "mysql://u:pw@h/db"],
               "env": {"TOKEN": "s3cret"}},
        "jira": {"type": "http", "url": "https://user:tok@mcp.example.com/v1/mcp"},
    },
    "projects": {
        "/work/app": {"allowedTools": [], "mcpServers": {"local-one": {"command": "uvx", "args": ["thing"]}}},
        "/work/empty": {"mcpServers": {}},
    },
}

CODEX = """# my codex settings
model = "gpt-5"

[mcp_servers.notes]
command = "/opt/venv/bin/python"  # keep this comment
args = ["-m", "mcp_server"]

[mcp_servers.notes.env]
API_KEY = "abc"

[mcp_servers.remote]
url = "https://mcp.example.com/mcp"
"""


@pytest.fixture
def home(tmp_path):
    h = Path(os.environ["HOME"])
    (h / ".claude.json").write_text(json.dumps(CLAUDE, indent=2) + "\n")
    os.chmod(h / ".claude.json", 0o600)
    (h / ".codex").mkdir()
    (h / ".codex" / "config.toml").write_text(CODEX)
    return h


def test_inventory_reads_all_scopes_without_exposing_secrets(home):
    entries, problems = inventory()
    assert problems == []
    by_id = {e.sid: e for e in entries}
    assert set(by_id) == {"claude-code:user:db", "claude-code:user:jira", "claude-code:local:/work/app:local-one",
                          "codex:user:notes", "codex:user:remote"}
    db = by_id["claude-code:user:db"]
    assert db.transport == "stdio" and db.command[0] == "/usr/bin/node" and db.env == {"TOKEN": "s3cret"}
    public = json.dumps([e.public() for e in entries])
    assert "s3cret" not in public and "pw@" not in public and "tok@" not in public and "mysql://" not in public
    assert by_id["claude-code:user:jira"].public()["url"] == "https://mcp.example.com"


def test_claude_wrap_then_unwrap_restores_the_exact_file(home, monkeypatch):
    monkeypatch.setenv("MCP_KEEPER_SHIM", "/usr/bin/mcp-keeper-shim")
    path = home / ".claude.json"
    original = path.read_text()
    changed = ClaudeCodeAdapter().set_wrapped(None, True)
    assert sorted(changed) == ["db", "local-one (local:/work/app)"]  # never the http server
    doc = json.loads(path.read_text())
    db = doc["mcpServers"]["db"]
    assert db["command"] == "/usr/bin/mcp-keeper-shim"
    assert db["args"] == ["--id", "claude-code:user:db", "--", "/usr/bin/node", "/opt/orders-db", "--dsn", "mysql://u:pw@h/db"]
    assert db["env"] == {"TOKEN": "s3cret"} and doc["numStartups"] == 7
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    entries = {e.name: e for e in ClaudeCodeAdapter().entries()}
    assert entries["db"].wrapped and entries["db"].command[0] == "/usr/bin/node"  # still sees the real server
    assert ClaudeCodeAdapter().set_wrapped(None, True) == []  # idempotent
    assert sorted(ClaudeCodeAdapter().set_wrapped(None, False)) == ["db", "local-one (local:/work/app)"]
    assert path.read_text() == original


def test_wrap_selected_names_only(home):
    assert ClaudeCodeAdapter().set_wrapped({"db"}, True) == ["db"]
    entries = {e.name: e.wrapped for e in ClaudeCodeAdapter().entries()}
    assert entries == {"db": True, "jira": False, "local-one": False}


def test_every_change_is_backed_up_first(home):
    path = home / ".claude.json"
    original = path.read_bytes()
    ClaudeCodeAdapter().set_wrapped(None, True)
    (stamp, reason, files), = backup.list_backups()
    assert "wrap Claude Code" in reason and files[0].original == str(path)
    assert files[0].copy.read_bytes() == original
    assert stat.S_IMODE(files[0].copy.stat().st_mode) == 0o600
    assert stat.S_IMODE(files[0].copy.parent.stat().st_mode) == 0o700


def test_restore_puts_the_file_back_and_backs_up_current(home):
    path = home / ".claude.json"
    original = path.read_bytes()
    ClaudeCodeAdapter().set_wrapped(None, True)
    stamp = backup.list_backups()[0][0]
    assert backup.restore(stamp) == [str(path)]
    assert path.read_bytes() == original
    assert len(backup.list_backups()) == 2  # the wrapped version was saved before restoring
    with pytest.raises(FileNotFoundError):
        backup.restore("nope")


def test_restore_refuses_a_damaged_copy(home):
    ClaudeCodeAdapter().set_wrapped(None, True)
    stamp, _, files = backup.list_backups()[0]
    files[0].copy.write_text("tampered")
    with pytest.raises(ValueError, match="checksum"):
        backup.restore(stamp)


def test_a_client_saving_once_meanwhile_is_retried(home, monkeypatch):
    path = home / ".claude.json"
    real = fsutil.write_text_atomic
    calls = []

    def client_saves_once(p, text, mode=0o600, expect_sha=None):
        if p == path and not calls:
            calls.append(1)
            doc = json.loads(path.read_text())
            path.write_text(json.dumps({**doc, "numStartups": 8}, indent=2) + "\n")  # Claude Code saved
        return real(p, text, mode, expect_sha)

    monkeypatch.setattr(fsutil, "write_text_atomic", client_saves_once)
    assert "db" in ClaudeCodeAdapter().set_wrapped(None, True)
    doc = json.loads(path.read_text())
    assert doc["numStartups"] == 8                          # the client's write survived
    assert doc["mcpServers"]["db"]["command"].endswith("mcp-keeper-shim")


def test_a_client_that_keeps_saving_fails_cleanly(home, monkeypatch):
    path = home / ".claude.json"
    real = fsutil.write_text_atomic
    monkeypatch.setattr(fsutil.time, "sleep", lambda s: None)

    def always_saving(p, text, mode=0o600, expect_sha=None):
        if p == path:
            path.write_text(path.read_text() + " ")
        return real(p, text, mode, expect_sha)

    monkeypatch.setattr(fsutil, "write_text_atomic", always_saving)
    with pytest.raises(fsutil.ConcurrentEdit, match="kept rewriting"):
        ClaudeCodeAdapter().set_wrapped(None, True)
    assert "mcp-keeper-shim" not in path.read_text()


def test_symlinked_config_is_written_through(home, tmp_path):
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir()
    (home / ".claude.json").rename(real)
    (home / ".claude.json").symlink_to(real)
    ClaudeCodeAdapter().set_wrapped({"db"}, True)
    assert (home / ".claude.json").is_symlink()
    assert "mcp-keeper-shim" in real.read_text()


def test_jsonc_is_read_but_never_rewritten(home):
    (home / ".config/Code/User").mkdir(parents=True)
    path = home / ".config/Code/User/mcp.json"
    path.write_text('{\n  // my servers\n  "servers": {"git": {"command": "uvx", "args": ["mcp-git",],},},\n}\n')
    vscode = next(a for a in json_adapters() if a.key == "vscode")
    assert [e.name for e in vscode.entries()] == ["git"]
    with pytest.raises(ValueError, match="contains comments"):
        vscode.set_wrapped(None, True)
    assert "// my servers" in path.read_text()


def test_malformed_entries_are_skipped_and_reported(home):
    path = home / ".claude.json"
    doc = json.loads(path.read_text())
    doc["mcpServers"]["bad1"] = {"command": 5}
    doc["mcpServers"]["bad2"] = {"command": "x", "args": "not-a-list"}
    doc["mcpServers"]["bad3"] = "nope"
    path.write_text(json.dumps(doc))
    entries, problems = inventory()
    assert "bad1" not in {e.name for e in entries} and "db" in {e.name for e in entries}
    assert sum("skipped malformed server entry" in p for p in problems) == 3
    assert ClaudeCodeAdapter().set_wrapped(None, True) == ["db", "local-one (local:/work/app)"]
    assert json.loads(path.read_text())["mcpServers"]["bad2"] == {"command": "x", "args": "not-a-list"}


def test_one_broken_client_does_not_stop_the_others(home):
    from mcp_keeper.clients import set_wrapped_everywhere
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text("{broken")
    changed, failed = set_wrapped_everywhere(True)
    assert "Cursor" in failed and set(changed) == {"Claude Code", "Codex"}


def test_codex_wrap_keeps_comments_and_unwraps_exactly(home, monkeypatch):
    monkeypatch.setenv("MCP_KEEPER_SHIM", "/usr/bin/mcp-keeper-shim")
    path = home / ".codex" / "config.toml"
    assert CodexAdapter().set_wrapped(None, True) == ["notes"]
    text = path.read_text()
    assert "# keep this comment" in text and "# my codex settings" in text
    assert 'args = ["--id", "codex:user:notes", "--", "/opt/venv/bin/python", "-m", "mcp_server"]' in text
    notes = {e.name: e for e in CodexAdapter().entries()}["notes"]
    assert notes.wrapped and notes.command == ("/opt/venv/bin/python", "-m", "mcp_server")
    assert notes.env == {"API_KEY": "abc"}
    assert CodexAdapter().set_wrapped(None, False) == ["notes"]
    assert path.read_text() == CODEX


def test_missing_and_broken_configs(home):
    assert JsonAdapter("cursor", "Cursor", ".cursor/mcp.json").entries() == []
    assert JsonAdapter("cursor", "Cursor", ".cursor/mcp.json").set_wrapped(None, True) == []
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text("{broken")
    entries, problems = inventory()
    assert any("Cursor: cannot read config" in p for p in problems)
    assert any(e.client == "claude-code" for e in entries)  # others still listed


def test_vscode_uses_servers_key(home):
    (home / ".config" / "Code" / "User").mkdir(parents=True)
    (home / ".config/Code/User/mcp.json").write_text(json.dumps(
        {"servers": {"git": {"type": "stdio", "command": "uvx", "args": ["mcp-git"]}}}))
    vscode = next(a for a in json_adapters() if a.key == "vscode")
    assert vscode.set_wrapped(None, True) == ["git"]
    assert vscode.entries()[0].wrapped


def test_wrap_helpers():
    command, args = wrap_command("a:b:c", "node", ["x"])
    assert unwrap_command(command, args) == ("node", ["x"])
    assert unwrap_command("node", ["--", "x"]) is None
    assert unwrap_command("/usr/bin/mcp-keeper-shim", ["--id", "x"]) is None
