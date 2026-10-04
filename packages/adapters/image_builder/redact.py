"""Scrub anything credential-shaped from text a builder sent back.

Build logs are written by tenant RUN steps and by the operator's CI, and both can echo a
secret -- an ``Authorization`` header in a curl -v, a token in a URL, the very API key
Pyrrhula used to call the builder. Every message an adapter returns passes through here
before it reaches ``image_build.log_tail``, ``error`` or a job row.

Two passes: the exact secrets this adapter holds (always replaced, wherever they appear),
then shapes that are credentials whatever they belong to.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_MASK = "[redacted]"
_PATTERNS = [
    re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)\s+)\S+"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bptr_[A-Za-z0-9+/=]{20,}\b"),  # Portainer API keys
    re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)\S+"),
]


def redact(text: str, secrets: Iterable[str] = (), *, limit: int = 4000) -> str:
    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, _MASK)
    for pattern in _PATTERNS:
        out = pattern.sub(lambda m: (m.group(1) if m.groups() and m.group(1) else "") + _MASK, out)
    return out[-limit:] if limit else out
