"""Mask secrets before anything is logged, shown or sent to the indicator.

Server command lines and env often carry credentials (a DSN with a password, an API
token), and so can a server's own stderr. Nothing leaves a shim unredacted. This is
best effort: log files are also owner-only (0600) in an owner-only folder."""

import re

_SECRET_WORDS = r"password|passwd|pwd|pw|secret|token|api[_-]?key|access[_-]?key|auth|credential"
# scheme://user:PASSWORD@host — the password may itself contain '@' or '/', so take up to the last '@'
_URL_CREDS = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)(\S+)(@[^\s@/?#]+)", re.I)
# key=value, key: value, "key": "value", 'key':'value'
_KEY_VALUE = re.compile(
    rf"([\w.-]*(?:{_SECRET_WORDS})[\w.-]*[\"']?)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"'}}]+)", re.I)
# --password X, --token=X
_FLAG = re.compile(rf"(--?[\w-]*(?:{_SECRET_WORDS})[\w-]*)(=|\s+)(?!-)(\S+)", re.I)
_BEARER = re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.I)
_TOKENS = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{10,}|glpat-[A-Za-z0-9_-]{10,}|xox[abpr]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})")

MASK = "***"


def text(value: str) -> str:
    # Bearer first: "Authorization: Bearer X" must lose X, not just the word "Bearer".
    value = _BEARER.sub(rf"\1 {MASK}", value)
    value = _URL_CREDS.sub(rf"\1{MASK}\3", value)
    value = _FLAG.sub(rf"\1\2{MASK}", value)
    value = _KEY_VALUE.sub(rf"\1\2{MASK}", value)
    return _TOKENS.sub(MASK, value)
