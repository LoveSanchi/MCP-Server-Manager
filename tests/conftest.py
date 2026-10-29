import pytest


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path, monkeypatch):
    """Never touch the real ~/.config, ~/.cache, runtime dir or MCP client configs."""
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"),
                     ("XDG_DATA_HOME", "data"), ("XDG_RUNTIME_DIR", "run")):
        monkeypatch.setenv(var, str(tmp_path / sub))
    (tmp_path / "run").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    for var in ("MCP_KEEPER_HUB", "MCP_KEEPER_CTL", "MCP_KEEPER_SHIM"):
        monkeypatch.delenv(var, raising=False)
