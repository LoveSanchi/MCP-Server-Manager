"""mcp-keeper — see and heal every MCP server of every AI client on this machine."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Callable

from mcp_keeper import __version__, backup, client
from mcp_keeper.clients import inventory, set_wrapped_everywhere
from mcp_keeper.client import ServiceError, ServiceUnavailable

DOTS = {"up": "●", "reachable": "◐", "starting": "◌", "down": "✕", "idle": "○", "unmanaged": "·"}
EXIT_OK, EXIT_PROBLEM, EXIT_NO_SERVICE = 0, 1, 3

Request = Callable[..., Any]


def _age(ts: float) -> str:
    if not ts:
        return ""
    secs = int(time.time() - ts)
    return f"{secs}s" if secs < 120 else f"{secs // 60}m" if secs < 7200 else f"{secs // 3600}h"


def format_status(data: dict) -> str:
    s = data["summary"]
    head = (f"{s['up']}/{s['total']} up · {s['down']} down · {s['idle']} idle"
            + (f" · {s['reachable']} remote" if s["reachable"] else "")
            + (f" · {s['unmanaged']} not wrapped" if s["unmanaged"] else ""))
    lines = [head]
    for problem in s.get("problems", []):
        lines.append(f"! {problem}")
    client_label = None
    for row in sorted(data["rows"], key=lambda r: (r["client_label"], r["name"])):
        if row["client_label"] != client_label:
            client_label = row["client_label"]
            lines.append(f"\n{client_label}")
        count = f" ×{row['instances']}" if row.get("instances", 0) > 1 else ""
        age = f" ({_age(row['since'])})" if row.get("since") and row["state"] in ("up", "down") else ""
        lines.append(f"  {DOTS.get(row['state'], '?')} {row['name']:<22} {row['state']:<9}{count}{age}  {row['detail']}")
    return "\n".join(lines)


def cmd_status(args, request: Request) -> int:
    data = request("status")
    if args.json:
        import json
        print(json.dumps(data, indent=2))
    else:
        print(format_status(data))
    return EXIT_OK if data["summary"]["down"] == 0 else EXIT_PROBLEM


def cmd_list(args, request: Request) -> int:
    entries, problems = inventory()
    for problem in problems:
        print(f"! {problem}", file=sys.stderr)
    for e in entries:
        p = e.public()
        mark = "wrapped" if p["wrapped"] else ("remote" if p["transport"] != "stdio" else "NOT wrapped")
        print(f"{p['client_label']:<14} {p['name']:<22} {p['transport']:<6} {mark:<12} {p['program'] or p['url']}"
              + (f"  [{p['scope']}]" if p["scope"] != "user" else ""))
    return EXIT_OK


def _change(args, wrapped: bool) -> int:
    names = set(args.names) if args.names else None
    if args.dry_run:
        for e in inventory()[0]:
            if e.transport == "stdio" and e.wrapped != wrapped and (names is None or e.name in names) \
                    and (not args.client or e.client == args.client):
                print(f"would {'wrap' if wrapped else 'unwrap'}: {e.client_label} / {e.name}")
        return EXIT_OK
    changed, failed = set_wrapped_everywhere(wrapped, args.client, names)
    total = 0
    for label, done in changed.items():
        for name in done:
            print(f"{'wrapped' if wrapped else 'unwrapped'}: {label} / {name}")
        total += len(done)
    for label, error in failed.items():
        print(f"FAILED {label}: {error}", file=sys.stderr)
    if total:
        print(f"\nBacked up first — see: mcp-keeper backups. Restart those clients (or reconnect their MCP "
              f"servers, e.g. /mcp in Claude Code) for this to take effect.")
        try:
            client.request("refresh", timeout=10)
        except (ServiceUnavailable, ServiceError):
            pass
    elif not failed:
        print("nothing to change")
    return EXIT_PROBLEM if failed else EXIT_OK


def cmd_backups(args, request: Request) -> int:
    for stamp, reason, files in backup.list_backups():
        print(f"{stamp}  {reason}")
        for b in files:
            print(f"    {b.original}")
    return EXIT_OK


def cmd_restore(args, request: Request) -> int:
    if not args.force:
        print("A restore puts back the WHOLE file as it was then, losing everything the client wrote "
              "since (history, trust, newly added servers…).\nTo undo mcp-keeper's own change, use "
              "'mcp-keeper unwrap' instead. To restore anyway, add --force.", file=sys.stderr)
        return EXIT_PROBLEM
    for path in backup.restore(args.stamp, args.file):
        print(f"restored {path}")
    print("Restart the affected clients for this to take effect.")
    return EXIT_OK


def cmd_restart(args, request: Request) -> int:
    data = request("restart", all_red=True) if args.all_red else request("restart", id=args.id)
    print(f"restart sent to {data['instances']} running instance(s) of: {', '.join(data['servers']) or 'none'}")
    return EXIT_OK


def cmd_test(args, request: Request) -> int:
    result = request("test", id=args.id, timeout=60)
    print(result["detail"])
    return EXIT_OK if result.get("ok") else EXIT_PROBLEM


def cmd_logs(args, request: Request) -> int:
    since = 0
    while True:
        for e in request("events", since=since, id=args.id, limit=args.lines if since == 0 else 5000):
            stamp = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
            print(f"{stamp} {e['level']:<6} {e['id'].rsplit(':', 1)[-1]:<20} {e['text']}", flush=True)
            since = e["seq"]
        if not args.follow:
            return EXIT_OK
        time.sleep(1)


def format_detail(d: dict) -> str:
    lines = [f"{d['id']}", f"  client     {d['client_label']} ({d['scope']} scope)",
             f"  config     {d['config_path']}", f"  transport  {d['transport']}"]
    if d["transport"] == "stdio":
        lines.append(f"  command    {d['command_line']}")
        lines.append(f"  wrapped    {'yes — the client starts it through mcp-keeper-shim' if d['wrapped'] else 'no'}")
    else:
        lines.append(f"  url        {d['full_url']}")
    lines.append(f"  env        {', '.join(d['env_names']) if d['env_names'] else '(none)'}"
                 + ("   (values hidden)" if d["env_names"] else ""))
    return "\n".join(lines)


def cmd_show(args, request: Request) -> int:
    print(format_detail(request("show", id=args.id)))
    return EXIT_OK


def cmd_refresh(args, request: Request) -> int:
    print(format_status(request("refresh")))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcp-keeper", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("status", help="every MCP server and its health (needs the service)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(run=cmd_status)
    sub.add_parser("list", help="configured servers per client (reads configs directly)").set_defaults(run=cmd_list)
    for name, wrapped, help_text in (("wrap", True, "put mcp-keeper in front of stdio servers (backs up first)"),
                                     ("unwrap", False, "restore the original server commands")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("names", nargs="*", help="server names (default: all)")
        s.add_argument("--client", help="only this client, e.g. claude-code, codex, cursor")
        s.add_argument("--dry-run", action="store_true")
        s.set_defaults(run=lambda a, r, w=wrapped: _change(a, w))
    sub.add_parser("backups", help="list config backups").set_defaults(run=cmd_backups)
    s = sub.add_parser("restore", help="put config files back from a backup")
    s.add_argument("stamp")
    s.add_argument("--file", help="only this original file path")
    s.add_argument("--force", action="store_true", help="really replace the whole file(s)")
    s.set_defaults(run=cmd_restore)
    s = sub.add_parser("restart", help="restart a server in every client running it")
    target = s.add_mutually_exclusive_group(required=True)
    target.add_argument("id", nargs="?", help="server id, e.g. claude-code:user:orders-db")
    target.add_argument("--all-red", action="store_true")
    s.set_defaults(run=cmd_restart)
    s = sub.add_parser("test", help="start a server on its own and check its handshake")
    s.add_argument("id")
    s.set_defaults(run=cmd_test)
    s = sub.add_parser("logs", help="event log (all servers, or one)")
    s.add_argument("id", nargs="?")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-n", "--lines", type=int, default=50)
    s.set_defaults(run=cmd_logs)
    sub.add_parser("refresh", help="re-read configs and re-check remote servers").set_defaults(run=cmd_refresh)
    s = sub.add_parser("show", help="a server's config and command (secrets masked)")
    s.add_argument("id")
    s.set_defaults(run=cmd_show)
    return p


def main(argv: list[str] | None = None, request: Request = client.request) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args, request)
    except ServiceUnavailable as exc:
        print(f"mcp-keeper: {exc}", file=sys.stderr)
        return EXIT_NO_SERVICE
    except (ServiceError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"mcp-keeper: {exc}", file=sys.stderr)
        return EXIT_PROBLEM
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
