"""A scriptable stdio MCP server for tests.

Environment switches:
  FAKE_FAIL_FILE   if this file exists at start, print an error to stderr and exit 1
                   (like orders-db when its database tunnel is down)
  FAKE_NO_INIT=1   never answer initialize
  FAKE_NO_PING=1   never answer ping
Tools: echo (returns its text), crash (exits 3 mid-call), ask (asks the client for roots
first), fail (returns a JSON-RPC error), leak (prints a password to stderr).
"""

import json
import os
import sys

if os.environ.get("FAKE_FAIL_FILE") and os.path.exists(os.environ["FAKE_FAIL_FILE"]):
    print("fatal: cannot connect to database: connection refused", file=sys.stderr, flush=True)
    sys.exit(1)

once = os.environ.get("FAKE_DIE_ON_FIRST_INIT")  # crash during the first handshake only
if os.environ.get("FAKE_EARLY_NOTE"):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/message",
                                 "params": {"level": "info", "data": "hello"}}) + "\n")
    sys.stdout.flush()

TOOLS = [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}},
         {"name": "crash", "description": "crash", "inputSchema": {"type": "object"}}]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def reply(rid, result):
    send({"jsonrpc": "2.0", "id": rid, "result": result})


for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        if once and not os.path.exists(once):
            open(once, "w").close()
            sys.exit(5)
        if os.environ.get("FAKE_NO_INIT"):
            continue
        reply(rid, {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1.0"}})
    elif method == "ping":
        if not os.environ.get("FAKE_NO_PING"):
            reply(rid, {})
    elif method == "tools/list":
        if os.environ.get("FAKE_PAGES"):
            cursor = (msg.get("params") or {}).get("cursor")
            reply(rid, {"tools": TOOLS[1:]} if cursor else {"tools": TOOLS[:1], "nextCursor": "p2"})
        else:
            reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments", {})
        if name == "orphan":  # leave a helper holding our stdout/stderr open, then die
            import subprocess
            subprocess.Popen(["sleep", "30"])
            sys.exit(4)
        if name == "bad_error":
            send({"jsonrpc": "2.0", "id": rid, "error": "not an object"})
            continue
        if name == "crash":
            print("panic: boom", file=sys.stderr, flush=True)
            sys.exit(3)
        if name == "ask":
            send({"jsonrpc": "2.0", "id": 99, "method": "roots/list"})
            answer = json.loads(sys.stdin.readline())
            reply(rid, {"content": [{"type": "text", "text": json.dumps(answer.get("result"))}]})
        elif name == "fail":
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "db said no"}})
        elif name == "leak":
            print("connecting to mysql://reader:hunter2@db:3306 password=hunter2", file=sys.stderr, flush=True)
            reply(rid, {"content": [{"type": "text", "text": "ok"}]})
        else:
            reply(rid, {"content": [{"type": "text", "text": args.get("text", "")}]})
    elif "id" in msg and "method" not in msg:
        pass  # a response we did not ask for
