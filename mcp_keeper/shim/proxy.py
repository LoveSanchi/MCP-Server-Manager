"""The stand-in between an MCP client and the real server.

The client only ever talks to this proxy, so the client's connection survives the
server crashing, failing to start (a database tunnel that is down), or being restarted
from the top bar. After every (re)start the proxy performs the MCP handshake with the
server itself, using the client's original initialize parameters, then tells the client
to re-fetch its lists. While the server is down, list requests are answered from the
last known lists and calls fail fast with the reason, instead of hanging.

Every server process gets a generation number. Output, exits and handshakes of an older
generation are ignored, so a restart can never mix up two processes.
"""

from __future__ import annotations

import asyncio
import collections
import copy
import itertools
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, Mapping, Sequence

from mcp_keeper import jsonrpc, redact
from mcp_keeper.reasons import best_reason
from mcp_keeper.shim.cache import Cache
from mcp_keeper.shim.child import Child
from mcp_keeper.shim.journal import Journal

STARTING, UP, DOWN, RESTARTING, STOPPED = "starting", "up", "down", "restarting", "stopped"
LIST_KEYS = {"tools/list": "tools", "prompts/list": "prompts", "resources/list": "resources",
             "resources/templates/list": "resourceTemplates"}
CHANGED = {"tools": "notifications/tools/list_changed", "prompts": "notifications/prompts/list_changed",
           "resources": "notifications/resources/list_changed"}
SHIM_IDS = "⁣mcpk-"     # invisible separator: no real client produces ids like these
MAX_EARLY_MESSAGES = 200    # server messages held until the client's handshake is answered
MAX_LIST_PAGES = 50
log = logging.getLogger("mcp-keeper-shim")


@dataclass(frozen=True)
class Timings:
    client_init_wait: float = 20.0   # how long the client's initialize may wait for a (re)starting server
    handshake_timeout: float = 30.0
    ping_interval: float = 30.0
    ping_timeout: float = 10.0
    ping_misses: int = 3
    backoff: tuple[float, ...] = (1, 2, 5, 10, 30, 60)
    stable_after: float = 60.0       # an up-time that resets the backoff


async def _no_drain() -> None:
    return None


class Shim:
    def __init__(self, sid: str, argv: Sequence[str], env: Mapping[str, str],
                 write_client: Callable[[bytes], None], *, cache: Cache | None = None,
                 journal: Journal | None = None, timings: Timings = Timings(), cwd: str | None = None,
                 drain_client: Callable[[], Awaitable[None]] = _no_drain):
        self.sid = sid
        self.name = sid.rsplit(":", 1)[-1]
        self._argv, self._env, self._cwd = list(argv), dict(env), cwd
        self._write_client_raw = write_client
        self._drain_client = drain_client
        self._cache = cache or Cache(sid)
        self._journal = journal or Journal(sid)
        self._t = timings
        self._ids = itertools.count(1)
        self._lifecycle = asyncio.Lock()     # one spawn / restart at a time
        self._child: Child | None = None
        self._generation = 0
        self._handshake_generation = 0       # the generation a handshake is running (or done) for
        self._state, self._detail, self._since = STARTING, "starting", time.time()
        self._init_params: dict | None = None
        self._init_result: dict | None = None
        self._client_answered = False
        self._early: list[bytes] = []
        self._ready = asyncio.Event()
        self._failed = asyncio.Event()       # a start attempt failed: lets initialize answer early
        self._pending: dict[Any, tuple[str, float, str]] = {}
        self._waiters: dict[str, asyncio.Future] = {}
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self._failures = 0
        self._last_failure = ""
        self._restart_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        self.restarts = 0
        self.requests = 0
        self.errors = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._background(self._ping_loop())
        async with self._lifecycle:
            await self._spawn()

    async def close(self) -> None:
        self._closing = True
        self._generation += 1
        self._abort_waiters()
        for task in [*self._tasks, self._restart_task]:
            if task:
                task.cancel()
        if self._child:
            await self._child.stop()
        self._set_state(STOPPED, "client disconnected")

    async def restart(self, reason: str) -> None:
        """Restart the server now (from the top bar, the CLI, or a failed health check)."""
        if self._closing:
            return
        async with self._lifecycle:
            self._journal.event("warn", f"restarting: {reason}")
            self._generation += 1  # the old child's exit is expected, not a crash
            self._abort_waiters()
            self._ready.clear()
            self._set_state(RESTARTING, reason)
            if self._restart_task:
                self._restart_task.cancel()
            if self._child:
                await self._child.stop()
            self._fail_pending(f"{self.name} was restarted ({reason}); please retry")
            self._failures = 0
            self.restarts += 1
            await self._spawn()

    def _background(self, coro: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._journal.event("error", f"internal error: {task.exception()!r}")

    # ── state ─────────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        info = (self._init_result or self._cache.get("initialize") or {}).get("serverInfo", {})
        client = (self._init_params or {}).get("clientInfo", {})
        tools = self._cache.get("tools/list") or {}
        return {
            "id": self.sid, "state": self._state, "detail": self._detail, "since": self._since,
            "shim_pid": os.getpid(), "child_pid": self._child.pid if self._child else None,
            "client": str(client.get("name", "")) if isinstance(client, dict) else "",
            "server": f"{info.get('name', '')} {info.get('version', '')}".strip(),
            "tools": len(tools.get("tools", [])), "restarts": self.restarts, "requests": self.requests,
            "errors": self.errors, "cwd": self._cwd or os.getcwd(), "path": os.environ.get("PATH", ""),
        }

    def _set_state(self, state: str, detail: str) -> None:
        changed = (state, detail) != (self._state, self._detail)
        self._state, self._detail = state, redact.text(detail)
        if changed:
            self._since = time.time()
            self._journal.state(self.snapshot())

    # ── child process ─────────────────────────────────────────────────────────
    async def _spawn(self) -> None:
        """Start a new server process. Caller holds self._lifecycle."""
        self._generation += 1
        generation = self._generation
        self._child = Child(self._argv, self._env, self._cwd)
        self._ready.clear()
        self._failed.clear()
        self._stderr_tail.clear()
        try:
            await self._child.start(lambda line: self._on_child_line(generation, line),
                                    lambda line: self._on_child_stderr(generation, line),
                                    lambda code: self._on_child_exit(generation, code))
        except OSError as exc:
            self._child = None
            await self._on_child_exit(generation, None, f"cannot start: {exc.strerror or exc}")
            return
        self._set_state(STARTING, f"starting again (last failure: {self._last_failure})"
                        if self._last_failure else "server starting")
        if self._init_params is not None:
            self._background(self._handshake(generation))

    async def _on_child_exit(self, generation: int, code: int | None, why: str = "") -> None:
        if generation != self._generation or self._closing:
            return
        if self._state == UP and time.time() - self._since >= self._t.stable_after:
            self._failures = 0
        self._ready.clear()
        self._abort_waiters()  # an in-flight handshake must not wait out its timeout
        reason = why or f"exited with code {code}: {best_reason(self._stderr_tail)}"
        self._failed.set()
        self._fail_pending(f"{self.name} stopped ({reason})")
        delay = self._t.backoff[min(self._failures, len(self._t.backoff) - 1)]
        self._failures += 1
        self._last_failure = reason
        self._set_state(DOWN, f"{reason}; retrying in {int(delay)}s")
        self._journal.event("error", f"server down: {reason}")
        self._restart_task = asyncio.create_task(self._respawn_after(generation, delay))

    async def _respawn_after(self, generation: int, delay: float) -> None:
        await asyncio.sleep(delay)
        async with self._lifecycle:
            if generation != self._generation or self._closing:
                return  # someone restarted it meanwhile
            self.restarts += 1
            await self._spawn()

    async def _handshake(self, generation: int) -> None:
        if self._handshake_generation >= generation or self._child is None:
            return  # already running (or done) for this process
        self._handshake_generation = generation
        reply = await self._ask("initialize", self._init_params, self._t.handshake_timeout)
        if generation != self._generation:
            return
        if reply is None and not (self._child and self._child.alive):
            return  # it exited: the exit handler reports why (with its stderr) and schedules the retry
        error = _error_of(reply)
        if reply is None or error is not None or not isinstance(reply.get("result"), dict):
            why = "no reply to initialize" if reply is None else f"initialize failed: {error or 'bad result'}"
            await self._fail_start(generation, why)
            return
        self._init_result = reply["result"]
        self._cache.put("initialize", self._init_result)
        self._child.send(jsonrpc.encode(jsonrpc.notification("notifications/initialized")))
        self._last_failure = ""
        self._set_state(UP, "ready")
        self._journal.event("info", f"ready: {self.snapshot()['server'] or self.name}")
        self._ready.set()
        if self._client_answered:
            self._announce_changes()
        self._background(self._refresh_lists(generation))

    async def _fail_start(self, generation: int, why: str) -> None:
        """A server that starts but will not handshake is treated like a crash: back off, retry."""
        async with self._lifecycle:
            if generation != self._generation:
                return
            self._generation += 1
            if self._child:
                await self._child.stop()
            await self._on_child_exit(self._generation, None, why)

    def _announce_changes(self) -> None:
        """The client is already past its handshake: tell it to re-fetch what may have changed."""
        capabilities = (self._init_result or {}).get("capabilities", {})
        for kind, method in CHANGED.items():
            if isinstance(capabilities, dict) and kind in capabilities:
                self._to_client(jsonrpc.encode(jsonrpc.notification(method)))

    async def _refresh_lists(self, generation: int) -> None:
        """Keep the offline copy of every list complete (following pagination)."""
        capabilities = (self._init_result or {}).get("capabilities", {})
        for method, key in LIST_KEYS.items():
            if not isinstance(capabilities, dict) or method.split("/")[0] not in capabilities:
                continue
            items, cursor = [], None
            for _ in range(MAX_LIST_PAGES):
                reply = await self._ask(method, {"cursor": cursor} if cursor else {}, self._t.handshake_timeout)
                result = (reply or {}).get("result")
                if generation != self._generation or not isinstance(result, dict):
                    return
                items.extend(result.get(key) or [])
                cursor = result.get("nextCursor")
                if not cursor:
                    self._cache.put(method, {key: items})
                    break

    async def _ask(self, method: str, params: dict | None, timeout: float) -> dict | None:
        """A request of the shim's own; its id never reaches the client."""
        if self._child is None:
            return None
        rid = f"{SHIM_IDS}{method}-{next(self._ids)}"
        future = asyncio.get_running_loop().create_future()
        self._waiters[rid] = future
        if not self._child.send(jsonrpc.encode(jsonrpc.request(rid, method, params))):
            self._waiters.pop(rid, None)
            return None
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waiters.pop(rid, None)

    def _abort_waiters(self) -> None:
        for future in self._waiters.values():
            if not future.done():
                future.set_result(None)
        self._waiters = {}

    async def _ping_loop(self) -> None:
        misses = 0
        while True:
            await asyncio.sleep(self._t.ping_interval)
            self._journal.state(self.snapshot())  # heartbeat for the service
            if self._state != UP or self._pending:
                misses = 0  # never judge a server that is busy answering the client
                continue
            reply = await self._ask("ping", None, self._t.ping_timeout)
            misses = 0 if reply is not None else misses + 1
            if misses >= self._t.ping_misses:
                misses = 0
                await self.restart(f"no reply to {self._t.ping_misses} pings")

    # ── messages from the server ──────────────────────────────────────────────
    async def _on_child_line(self, generation: int, line: bytes) -> None:
        if generation != self._generation:
            return  # output of a process that has been replaced
        try:
            self._route_from_server(line)
        except Exception as exc:  # noqa: BLE001 - one odd message must never stop the pump
            self._journal.event("error", f"could not handle a server message: {exc!r}")
        await self._drain_client()

    def _route_from_server(self, line: bytes) -> None:
        message = jsonrpc.decode(line)
        if message is None:
            if line.strip():
                text = line.decode("utf-8", "replace").strip()[:200]
                self._journal.event("warn", f"non-JSON output on stdout: {redact.text(text)}")
            return
        rid = message.get("id")
        if jsonrpc.is_response(message) and isinstance(rid, str) and rid in self._waiters:
            future = self._waiters[rid]
            if not future.done():
                future.set_result(message)
            return
        if jsonrpc.is_response(message):
            self._account(self._pending.pop(rid, None) if _hashable(rid) else None, message)
        data = line if line.endswith(b"\n") else line + b"\n"
        if not self._client_answered and not jsonrpc.is_response(message):
            if len(self._early) < MAX_EARLY_MESSAGES:
                self._early.append(data)  # hold until the client has its initialize answer
            return
        self._to_client(data)
        if message.get("method") == "notifications/tools/list_changed":
            self._background(self._refresh_lists(self._generation))

    def _account(self, entry: tuple[str, float, str] | None, message: dict) -> None:
        if entry is None:
            return
        method, started, tool = entry
        ms = int((time.monotonic() - started) * 1000)
        error = _error_of(message)
        result = message.get("result")
        if error is not None:
            self.errors += 1
            self._journal.event("error", f"{method}{' ' + tool if tool else ''} failed in {ms} ms: "
                                         f"{redact.text(error)[:200]}")
        elif method == "tools/call":
            failed = isinstance(result, dict) and bool(result.get("isError"))
            self._journal.event("warn" if failed else "info",
                                f"tools/call {tool} → {'tool error' if failed else 'ok'} in {ms} ms")

    async def _on_child_stderr(self, generation: int, line: bytes) -> None:
        if generation != self._generation:
            return
        text = redact.text(line.decode("utf-8", "replace").rstrip())
        if text:
            self._stderr_tail.append(text[:300])
            self._journal.stderr(text)

    # ── messages from the client ──────────────────────────────────────────────
    async def on_client_line(self, line: bytes) -> None:
        try:
            self._route_from_client(line)
        except Exception as exc:  # noqa: BLE001 - one odd message must never stop the pump
            self._journal.event("error", f"could not handle a client message: {exc!r}")
        if self._child is not None:
            await self._child.drain()

    def _route_from_client(self, line: bytes) -> None:
        stripped = line.strip()
        if stripped.startswith(b"["):
            self._to_client(jsonrpc.encode(jsonrpc.error(None, "JSON-RPC batches are not supported", -32600)))
            return
        message = jsonrpc.decode(line)
        if message is None:
            if stripped:
                self._to_client(jsonrpc.encode(jsonrpc.error(None, "parse error", -32700)))
            return
        method, rid = message.get("method"), message.get("id")
        problem = _invalid_request(message)
        if problem:
            self._to_client(jsonrpc.encode(jsonrpc.error(rid if _hashable(rid) else None, problem, -32600)))
            return
        if jsonrpc.is_request(message):
            if method == "initialize":
                self._background(self._client_initialize(message))
            elif method == "ping":
                self._to_client(jsonrpc.encode(jsonrpc.result(rid, {})))
            else:
                self._client_request(message, line)
        elif jsonrpc.is_notification(message):
            if method != "notifications/initialized" and self._ready.is_set():
                self._child.send(line)
        elif self._ready.is_set():  # the client answering a request of the server's
            self._child.send(line)

    async def _client_initialize(self, message: dict) -> None:
        self._init_params = _complete_init_params(message.get("params"))
        if self._child and self._child.alive and not self._ready.is_set():
            self._background(self._handshake(self._generation))
        ready = asyncio.create_task(self._ready.wait())
        failed = asyncio.create_task(self._failed.wait())
        await asyncio.wait({ready, failed}, timeout=self._t.client_init_wait, return_when=asyncio.FIRST_COMPLETED)
        ready.cancel()
        failed.cancel()
        if self._ready.is_set():
            result = self._init_result
        else:  # the server failed to start (or is slow): never hold up the client's own startup
            result = self._cache.get("initialize") or self._minimal_init()
            self._journal.event("warn", "answered the client's handshake from cache: server not ready")
        self._to_client(jsonrpc.encode(jsonrpc.result(message["id"], _announce_lists(result))))
        self._client_answered = True
        early, self._early = self._early, []
        for data in early:
            self._to_client(data)

    def _minimal_init(self) -> dict:
        return {
            "protocolVersion": (self._init_params or {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": self.name, "version": "0 (mcp-keeper: server not running yet)"},
        }

    def _client_request(self, message: dict, line: bytes) -> None:
        rid, method = message["id"], message["method"]
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        self.requests += 1
        if self._ready.is_set():
            tool = str(params.get("name", "")) if method == "tools/call" else ""
            self._pending[rid] = (method, time.monotonic(), tool)
            self._child.send(line if line.endswith(b"\n") else line + b"\n")
            return
        if method in LIST_KEYS and not params.get("cursor"):
            cached = self._cache.get(method) or {LIST_KEYS[method]: []}
            self._to_client(jsonrpc.encode(jsonrpc.result(rid, cached)))
            return
        self.errors += 1
        self._to_client(jsonrpc.encode(jsonrpc.error(
            rid, f"{self.name} is {self._state}: {self._detail}. mcp-keeper is bringing it back — retry shortly.")))

    def _fail_pending(self, why: str) -> None:
        for rid in list(self._pending):
            self._to_client(jsonrpc.encode(jsonrpc.error(rid, why)))
        self.errors += len(self._pending)
        self._pending = {}

    def _to_client(self, data: bytes) -> None:
        try:
            self._write_client_raw(data)
        except (ConnectionError, RuntimeError, OSError) as exc:
            self._journal.event("error", f"cannot write to the client: {exc!r}")


def _hashable(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float))


def _invalid_request(message: dict) -> str:
    if "id" in message and not _hashable(message["id"]):
        return "invalid request id"
    if isinstance(message.get("id"), str) and message["id"].startswith(SHIM_IDS):
        return "request id uses a prefix reserved by mcp-keeper"
    if "method" in message and not isinstance(message["method"], str):
        return "method must be a string"
    if message.get("params") is not None and not isinstance(message.get("params"), (dict, list)):
        return "params must be an object"
    return ""


def _error_of(message: dict | None) -> str | None:
    if not message or "error" not in message:
        return None
    error = message["error"]
    return str(error.get("message", error)) if isinstance(error, dict) else str(error)


def _complete_init_params(params: Any) -> dict:
    """Fill in what the spec requires, so a sloppy client cannot make strict servers
    (e.g. ones validating with zod) reject every handshake replayed on a restart."""
    given = params if isinstance(params, dict) else {}
    return {"protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "unknown-client", "version": "0"}, **given}


def _announce_lists(result: dict) -> dict:
    """Promise list_changed for every list the server has, so the client re-fetches after a restart."""
    out = copy.deepcopy(result) if isinstance(result, dict) else {}
    capabilities = out.setdefault("capabilities", {})
    for kind in CHANGED:
        if isinstance(capabilities, dict) and isinstance(capabilities.get(kind), dict):
            capabilities[kind] = {**capabilities[kind], "listChanged": True}
    return out
