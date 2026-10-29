"""Timestamped copies of every MCP config file before mcp-keeper changes it.

Backups contain whatever the configs contain — often tokens and passwords — so the
folder is 0700 and every file 0600. The newest KEEP backups are kept."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from mcp_keeper import fsutil, paths

MANIFEST = "manifest.json"
KEEP = 100


@dataclass(frozen=True)
class Backup:
    stamp: str
    original: str
    copy: Path
    reason: str
    sha256: str


def _new_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime()) + f"-{time.time_ns() % 1_000_000:06d}"


def _folder() -> tuple[str, Path]:
    stamp = _new_stamp()
    folder = paths.backup_dir() / stamp
    folder.mkdir(mode=0o700, parents=True)
    os.chmod(paths.backup_dir(), 0o700)
    return stamp, folder


def save(contents: dict[Path, bytes], reason: str) -> list[Backup]:
    """Store exactly these bytes as the backup of each path (the bytes an edit was based on)."""
    if not contents:
        return []
    stamp, folder = _folder()
    saved, manifest = [], []
    for i, (original, data) in enumerate(contents.items()):
        copy = folder / f"{i:02d}-{original.name}"
        fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        entry = Backup(stamp, str(original), copy, reason, fsutil.sha256(data))
        saved.append(entry)
        manifest.append({"original": entry.original, "copy": copy.name, "sha256": entry.sha256})
    fsutil.write_text_atomic(folder / MANIFEST, json.dumps({"stamp": stamp, "reason": reason,
                                                             "files": manifest}, indent=2), mode=0o600)
    _prune()
    return saved


def snapshot(files: list[Path], reason: str) -> list[Backup]:
    """Back up the current content of each existing file."""
    return save({f: f.resolve().read_bytes() for f in files if f.exists()}, reason)


def _prune() -> None:
    folders = sorted((p for p in paths.backup_dir().glob("*/") if (p / MANIFEST).exists()), reverse=True)
    for old in folders[KEEP:]:
        shutil.rmtree(old, ignore_errors=True)


def list_backups() -> list[tuple[str, str, list[Backup]]]:
    """Newest first: (stamp, reason, files). Unreadable manifests are skipped."""
    root = paths.backup_dir()
    out = []
    for folder in sorted(root.glob("*/"), reverse=True) if root.exists() else []:
        try:
            data = json.loads((folder / MANIFEST).read_text())
            files = [Backup(str(data["stamp"]), str(f["original"]), folder / str(f["copy"]),
                            str(data.get("reason", "")), str(f["sha256"])) for f in data["files"]]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        out.append((files[0].stamp if files else folder.name, str(data.get("reason", "")), files))
    return out


def restore(stamp: str, only: str | None = None) -> list[str]:
    """Put whole files back exactly as they were in backup `stamp`.

    This discards everything the client wrote since (history, trust, new servers…):
    prefer `unwrap`, which only reverts mcp-keeper's own change."""
    match = next((b for b in list_backups() if b[0] == stamp), None)
    if match is None:
        raise FileNotFoundError(f"no backup named {stamp}")
    files = [b for b in match[2] if only is None or b.original == only]
    if not files:
        raise FileNotFoundError(f"backup {stamp} has no copy of {only}")
    for b in files:
        if fsutil.sha256(b.copy.read_bytes()) != b.sha256:
            raise ValueError(f"backup copy {b.copy} is damaged (checksum mismatch); not restoring it")
    snapshot([Path(b.original) for b in files], f"before restoring {stamp}")
    restored = []
    for b in files:
        target = Path(b.original)
        fsutil.write_bytes_atomic(target, b.copy.read_bytes(), mode=_mode(target))
        restored.append(b.original)
    return restored


def _mode(path: Path) -> int:
    try:
        return path.stat().st_mode & 0o777
    except FileNotFoundError:
        return 0o600
