"""Turn a process's error output into the one line that explains a failure."""

from __future__ import annotations

import re
from typing import Iterable

_ERROR_WORDS = re.compile(r"error|fail|refused|denied|timed? ?out|unreachable|ECONN|EHOST|ENOTFOUND|"
                          r"cannot|can't|unable|not found|invalid|fatal|panic|exception", re.I)


def best_reason(lines: Iterable[str]) -> str:
    """The stderr line that best explains an exit: the last one that mentions an error,
    else the last one with real words in it (servers often end with a lone '}')."""
    lines = [ln.strip() for ln in lines if ln.strip()]
    for line in reversed(lines):
        if _ERROR_WORDS.search(line):
            return line[:240]
    for line in reversed(lines):
        if sum(c.isalnum() for c in line) >= 4:
            return line[:240]
    return "no output"
