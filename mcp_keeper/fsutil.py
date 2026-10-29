"""Safe edits of files other programs also write (a running Claude Code rewrites
~/.claude.json often).

A write only replaces the file if its content on disk is still byte-for-byte what we
read (compared by hash, not mtime), is synced to disk before the rename, and goes
through symlinks (dotfile managers) instead of replacing them."""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Callable

RETRIES = 5


class ConcurrentEdit(RuntimeError):
    """The file changed between our read and our write (e.g. the client saved it)."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(path: Path) -> tuple[bytes, str]:
    data = path.resolve().read_bytes()
    return data, sha256(data)


def write_bytes_atomic(path: Path, data: bytes, mode: int = 0o600, expect_sha: str | None = None) -> None:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if expect_sha is not None and target.exists() and sha256(target.read_bytes()) != expect_sha:
            raise ConcurrentEdit(f"{path} changed while it was being edited")
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_text_atomic(path: Path, text: str, mode: int = 0o600, expect_sha: str | None = None) -> None:
    write_bytes_atomic(path, text.encode("utf-8"), mode, expect_sha)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def with_retries(edit: Callable[[], list[str]], retries: int = RETRIES) -> list[str]:
    """Re-run a read-modify-write when the file changed underneath it."""
    for attempt in range(retries):
        try:
            return edit()
        except ConcurrentEdit:
            if attempt == retries - 1:
                raise ConcurrentEdit("the client kept rewriting its config; close it and try again") from None
            time.sleep(0.2 * (attempt + 1))
    return []
