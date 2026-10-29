import pytest

from mcp_keeper.indicator import model


def summary(**kw):
    base = {"total": 4, "up": 2, "down": 0, "idle": 1, "unmanaged": 0, "reachable": 1, "problems": []}
    return {"summary": {**base, **kw}, "rows": []}


@pytest.mark.parametrize("status, icon, label, headline", [
    (None, model.ICON_OFF, "", "mcp-keeper service is not running"),
    (summary(), model.ICON_OK, "2/4", "2/4 up · 1 idle · 1 remote"),
    (summary(down=1, up=1), model.ICON_WARN, "1/4", "1/4 up · 1 down · 1 idle · 1 remote"),
    (summary(down=2, up=0), model.ICON_ERROR, "0/4", "0/4 up · 2 down · 1 idle · 1 remote"),
    (summary(up=0, idle=3, reachable=0, unmanaged=1), model.ICON_OFF, "0/4", "0/4 up · 3 idle · 1 not wrapped"),
])
def test_overview(status, icon, label, headline):
    assert model.overview(status) == model.Overview(icon, label, headline)


ROW = {"id": "claude-code:user:orders-db", "client_label": "Claude Code", "name": "orders-db", "state": "up",
       "detail": "ready", "transport": "stdio", "program": "node", "url": "", "scope": "user",
       "instances": 2, "clients": ["claude-code", "codex"], "requests": 9, "errors": 1, "restarts": 3,
       "since": 1000, "test": None}


def test_groups_and_titles():
    rows = [ROW, {**ROW, "id": "b", "name": "aaa", "state": "down"},
            {**ROW, "id": "c", "client_label": "Codex", "name": "x", "state": "idle", "instances": 0}]
    (claude, claude_rows), (codex, _) = model.groups(rows)
    assert (claude, codex) == ("Claude Code", "Codex")
    assert [r["name"] for r in claude_rows] == ["aaa", "orders-db"]  # worst first
    assert model.group_title("Claude Code", claude_rows) == "🔴  Claude Code   1/2"
    assert model.row_title(ROW) == "🟢  orders-db  ×2"
    assert model.row_title({**ROW, "instances": 1}) == "🟢  orders-db"


def test_row_details():
    lines = model.row_details({**ROW, "test": {"at": 1030, "detail": "ok: handshake + 2 tools"}}, now=1060)
    assert lines == ["Up — ready", "stdio · node", "running in claude-code, codex (2 instances)",
                     "requests 9 · errors 1 · restarts 3", "up for 60s", "last test 30s ago: ok: handshake + 2 tools"]
    idle = model.row_details({**ROW, "state": "idle", "instances": 0, "scope": "local:/p"}, now=1060)
    assert idle == ["Idle — ready", "stdio · node · local:/p"]
    remote = model.row_details({**ROW, "transport": "http", "program": "", "url": "https://h", "instances": 0,
                                "state": "reachable", "since": 0}, now=2)
    assert remote[1] == "http · https://h"


def test_structure_ignores_changing_text_and_actions():
    status = {"summary": {"down": 0}, "rows": [ROW]}
    later = {"summary": {"down": 0}, "rows": [{**ROW, "detail": "x", "requests": 50, "since": 5}]}
    assert model.structure(status, False) == model.structure(later, False)
    assert model.structure(status, False) != model.structure({"summary": {"down": 1}, "rows": [{**ROW, "state": "down"}]}, False)
    assert model.structure(None, True) == (None, True)
    assert model.can_restart(ROW) and not model.can_restart({**ROW, "instances": 0})
    assert model.can_test(ROW) and model.can_test({**ROW, "transport": "http"})


def test_ages():
    assert model._ago(0, 100) == "1m"
    assert model._ago(0, 30) == "30s" and model._ago(0, 600) == "10m" and model._ago(0, 7200) == "2h"
