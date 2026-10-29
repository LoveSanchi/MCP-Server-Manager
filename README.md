# MCP Server Manager (mcp-keeper)

See and heal every MCP server of every AI client on your machine, from the GNOME
top bar.

Claude Code, Codex, Cursor, Claude Desktop, Windsurf, Antigravity, Gemini CLI and
VS Code each start their own MCP servers. When one fails, for example a database
server whose tunnel was down when the client started, you only find out inside
that client, and usually have to restart or reconnect it by hand. MCP Keeper puts
all of them in one place:

- **A meter in the top bar.** It shows how many servers are green out of the
  total, and the icon turns amber or red as soon as one goes down.
- **One menu per client.** Every server is listed with its state, the reason it
  failed, which clients are running it, and counts of requests, errors and
  restarts.
- **Self-healing.** A server that crashes or cannot start is brought back
  automatically, with a growing delay between attempts. **Restart** and
  **Restart all red** do it on demand. In both cases the client stays
  connected.
- **Live logs.** You can follow every server's events as they happen (starts,
  failures, tool calls with their duration, its error output), or one
  server's events only.
- **Test now.** This starts any configured server on its own and checks its
  MCP handshake, even if no client is running it.
- **Remote servers** (HTTP/SSE) are checked for reachability.

## How it works

For every local (stdio) server, MCP Keeper changes the client's config so that
the client starts `mcp-keeper-shim`, a small stand-in, with the original
command passed through:

```
client ⇄ mcp-keeper-shim ⇄ the real server
              │
              └─ reports to mcp-keeper.service ⇄ top-bar indicator / mcp-keeper CLI
```

The client only ever talks to the stand-in, so its connection survives
whatever happens to the server behind it.

- **When the server restarts,** the stand-in repeats the MCP handshake with it,
  using the client's original parameters. It then sends
  `notifications/tools/list_changed`, so the client re-fetches the tools.
- **While the server is down,** list requests are answered from the last known
  lists, and tool calls fail immediately with the reason, for example
  `orders-db is down: ECONNREFUSED; retrying in 5s`, instead of hanging.
- **If the server can't start at all,** the client still gets a valid
  handshake straight away and sees the server's last known tools. Once the
  server recovers, the tools come back on their own.
- **If the service isn't running,** the stand-in keeps working as a plain
  proxy.

Secrets are masked everywhere. A server's command line and environment (which
often hold a DSN or a token) are never logged, and its error output is redacted
before anything is written or shown.

## Install (Ubuntu 22.04+ / GNOME)

Download `mcp-keeper_<version>_all.deb` from the [Releases](../../releases) page, or build it
yourself (see *Build and develop* below). Then:

```bash
sudo apt install ./mcp-keeper_*_all.deb
systemctl --user daemon-reload && systemctl --user start mcp-keeper
mcp-keeper-indicator &
mcp-keeper wrap --dry-run      # see what would change
mcp-keeper wrap                # every config is backed up first
```

Then restart your AI clients, or reconnect their MCP servers (`/mcp` in Claude
Code), so they start the servers through MCP Keeper.

**Backups and undo:**

- Before any change, every config file is copied to
  `~/.local/share/mcp-keeper/backups/<timestamp>/` (folder 0700, files 0600,
  since configs hold secrets).
- `mcp-keeper unwrap` puts the original commands back exactly.
- `mcp-keeper restore <timestamp>` puts back whole files.
- `apt remove` unwraps the removing user's configs automatically.

Project-level `.mcp.json` files are shared through git and are never rewritten.

## Use

| From the top bar | From a terminal |
|---|---|
| meter + menu per client | `mcp-keeper status` |
| server → Restart / Restart all red | `mcp-keeper restart <id>` / `--all-red` |
| server → Test now | `mcp-keeper test <id>` |
| Live log… | `mcp-keeper logs [<id>] -f` |
| Wrap / Unwrap all servers… | `mcp-keeper wrap` / `unwrap` (`--client`, names, `--dry-run`) |
| Config backups… → Restore | `mcp-keeper backups`, `mcp-keeper restore <stamp>` |

| Dot | Meaning |
|---|---|
| 🟢 | running in a client, handshake done, answering |
| 🟡 | starting |
| 🔴 | down: crashed, cannot start, or stopped answering pings (the reason is shown) |
| ⚪ | configured, but no client is running it right now (use **Test now**) |
| 🔵 | remote server reachable; its login is handled by the client |
| ⚫ | not wrapped: MCP Keeper can't see it |

A server id looks like `claude-code:user:orders-db` or `codex:user:notes`.

## Supported clients

| Client | Config |
|---|---|
| Claude Code | `~/.claude.json` (user and per-project local scope) |
| Codex | `~/.codex/config.toml` (comments preserved) |
| Claude Desktop | `~/.config/Claude/claude_desktop_config.json` |
| Cursor | `~/.cursor/mcp.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| Antigravity | `~/.gemini/antigravity/mcp_config.json` |
| Gemini CLI | `~/.gemini/settings.json` |
| VS Code | `~/.config/Code/User/mcp.json` |

## Build and develop

```bash
packaging/build-deb.sh                       # → dist/mcp-keeper_<version>_all.deb
python3 -m venv --system-site-packages .venv && .venv/bin/pip install pytest pytest-cov tomlkit
.venv/bin/python -m pytest --cov
```

## License

[MIT](LICENSE)
