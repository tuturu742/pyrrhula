"""Canonical JSON serialisation: stable key order, compact separators, no float drift.
Shared by the audit hash chain and any future content-hash user (e.g.
``knowledge_source_version.content_hash``) — one canonicalisation rule, not one per
caller that quietly drift apart.

Floats are the classic hash-instability source (``json.dumps`` repr can vary by platform/
version for the same value). The payloads hashed here are IDs, enum-like strings, and
JSON-able dicts — never floats — so this raises rather than silently normalising one,
which would hide a real bug in the caller.
"""

from __future__ import annotations

import json
from typing import Any


class UnhashableFloatError(TypeError):
    pass


def _reject_floats(obj: Any) -> Any:
    if isinstance(obj, float):
        raise UnhashableFloatError(
            "canonical_json() payloads must not contain floats (hash instability); "
            "convert to a string or int before hashing"
        )
    if isinstance(obj, dict):
        return {k: _reject_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_reject_floats(v) for v in obj]
    return obj


def canonical_json(data: Any) -> str:
    checked = _reject_floats(data)
    return json.dumps(checked, sort_keys=True, separators=(",", ":"), default=str)
