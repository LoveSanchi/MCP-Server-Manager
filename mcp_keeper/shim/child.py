"""The real MCP server, run as a child of the shim in its own process group."""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
from typing import Awaitable, Callable, Mapping, Sequence

LINE_LIMIT = 64 * 1024 * 1024  # a tools/list of a big server easily exceeds asyncio's 64 KiB default
TERM_GRACE_SECONDS = 3.0
PIPE_GRACE_SECONDS = 1.0       # after exit, how long to keep reading output a helper may still hold open
DRAIN_LIMIT_SECONDS = 30.0

OnLine = Callable[[bytes], Awaitable[None]]
OnExit = Callable[[int], Awaitable[None]]

_PR_SET_PDEATHSIG = 1


def _die_with_parent() -> None:
    """Runs in the child before exec: if the shim is killed (even SIGKILL), the server gets
    SIGTERM instead of living on as an orphan."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except OSError:
        pass


class Child:
    def __init__(self, argv: Sequence[str], env: Mapping[str, str], cwd: str | None = None):
        self._argv = list(argv)
        self._env = dict(env)
        self._cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self, on_stdout: OnLine, on_stderr: OnLine, on_exit: OnExit) -> None:
        """Raises OSError when the command cannot be started at all."""
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self._env, cwd=self._cwd,
            start_new_session=True, preexec_fn=_die_with_parent, limit=LINE_LIMIT,
        )
        proc = self._proc
        out = asyncio.create_task(self._pump(proc.stdout, on_stdout))
        err = asyncio.create_task(self._pump(proc.stderr, on_stderr))

        async def wait_exit() -> None:
            code = await proc.wait()
            # Deliver the last lines, but don't wait for EOF forever: a helper the server
            # started may still hold its stdout/stderr open after the server itself died.
            done, pending = await asyncio.wait({out, err}, timeout=PIPE_GRACE_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                _signal_group(proc.pid, signal.SIGTERM)  # those helpers die with the server
            await on_exit(code)

        self._tasks = [out, err, asyncio.create_task(wait_exit())]

    @staticmethod
    async def _pump(stream: asyncio.StreamReader, on_line: OnLine) -> None:
        while True:
            try:
                line = await stream.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue  # an absurdly long line: drop it, keep the server
            if not line:
                return
            await on_line(line)

    def send(self, data: bytes) -> bool:
        if not self.alive or self._proc.stdin is None:
            return False
        try:
            self._proc.stdin.write(data)
        except (ConnectionError, RuntimeError):
            return False
        return True

    async def drain(self) -> None:
        """Wait while the server's stdin is backed up, so a stuck server cannot grow our memory."""
        if not self.alive or self._proc.stdin is None:
            return
        try:
            await asyncio.wait_for(self._proc.stdin.drain(), DRAIN_LIMIT_SECONDS)
        except (ConnectionError, RuntimeError, asyncio.TimeoutError):
            pass

    async def stop(self) -> None:
        """Terminate the whole process group (servers often start helpers of their own)."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), TERM_GRACE_SECONDS)
        except asyncio.TimeoutError:
            _signal_group(proc.pid, signal.SIGKILL)
            await proc.wait()


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
