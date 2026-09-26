"""The narrower cousin of the INV-1 import-graph lint: *production*
code must only ever open a database session via ``core.tenancy.scope`` —
``tenant_scope()`` or ``unscoped_session()``. Anything else constructing a SQLAlchemy
engine/sessionmaker directly has bypassed the one place that guarantees the RLS GUC is
set correctly.

Test code is exempt (mirrors INV-1's exemption): infrastructure tests legitimately need
raw connections for things the app itself must never do — proving pooled-connection
reuse doesn't leak a tenant GUC (``tests/isolation/test_pooler_leak.py``), or connecting
as the admin/migrator role to simulate a superuser bypassing RLS
(``packages/core/audit/tests/test_service_and_verify.py``).
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"

_ALLOWED_FILE = PACKAGES / "core" / "tenancy" / "scope.py"
_ALLOWED_ALEMBIC_ENV = ROOT / "migrations" / "env.py"  # migrations run outside app scoping

_FORBIDDEN_PATTERNS = (
    re.compile(r"\bcreate_async_engine\s*\("),
    re.compile(r"\basync_sessionmaker\s*\("),
    re.compile(r"\bsessionmaker\s*\("),
)


def test_no_engine_or_sessionmaker_outside_scope_py() -> None:
    offenders: list[str] = []
    for path in PACKAGES.rglob("*.py"):
        if path in (_ALLOWED_FILE,) or path == _ALLOWED_ALEMBIC_ENV:
            continue
        if "tests" in path.parts:
            continue
        text = path.read_text()
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)}: {pattern.pattern}")

    assert not offenders, (
        "Only core/tenancy/scope.py may construct a SQLAlchemy engine or sessionmaker "
        "(CLAUDE.md rule 4). Offending files:\n" + "\n".join(offenders)
    )
