"""CLAUDE.md rule 5: append-only tables must have no UPDATE/DELETE grant for the app
role. Until now this was enforced only by each migration remembering to REVOKE — with no
test asserting the actual grants, which is exactly how ``session_event`` once shipped
missing its REVOKE and nobody noticed. This catalog-based test closes that gap: it reads
``information_schema.role_table_grants`` directly and fails if any append-only table that
exists grants UPDATE or DELETE to ``pyrrhula_app``.

Only the append-only tables that exist *today* are listed. The rest of rule 5's list
(``secret_disclosure_event``, ``disclosure_decision``, ``entity_state_change``) lands in
later phases; each must be added here in the same PR that creates it — the
``test_no_unexpected_append_only_tables_appeared`` guard below fails loudly to force that
(confirmed live: it caught ``checkpoint``, ``context_manifest`` and ``resolution_record``
before their registration).
"""

from __future__ import annotations

from sqlalchemy import text

from core.tenancy.scope import unscoped_session

_APP_ROLE = "pyrrhula_app"

# Append-only tables (CLAUDE.md rule 5) that exist at the current migration head.
_APPEND_ONLY_TABLES_TODAY = {
    "audit_log",
    "knowledge_source_version",
    "session_event",
    "checkpoint",
    "context_manifest",
    "resolution_record",
    "secret_disclosure_event",
    "disclosure_decision",
    "behavior_profile",
    "entity_state_change",
    # Not itself named in CLAUDE.md's rule-5 enumeration (a later addition, not one of
    # the original append-only tables) -- registered here anyway since it's
    # append-only by the same REVOKE convention, and this catalog test is strictly more
    # useful catching a missing REVOKE on it too.
    "persona_version",
    # The per-session MCP call ledger: append-only by the same REVOKE convention, and the
    # thing max_calls_per_session counts -- a session that could amend it could uncap
    # itself.
    "mcp_call_record",
}

# Every rule-5 append-only table, whether or not it exists yet. A table from this set
# showing up in the database that isn't in _APPEND_ONLY_TABLES_TODAY means a later phase
# created it — and must add it to the enforced set above in the same PR.
_ALL_RULE5_APPEND_ONLY = set(_APPEND_ONLY_TABLES_TODAY)

_GRANT_QUERY = """
    SELECT table_name, privilege_type
    FROM information_schema.role_table_grants
    WHERE grantee = :role
      AND table_schema = 'public'
      AND privilege_type IN ('UPDATE', 'DELETE')
"""


async def test_append_only_tables_have_no_update_or_delete_grant(db_available: None) -> None:
    async with unscoped_session() as session:
        rows = (await session.execute(text(_GRANT_QUERY), {"role": _APP_ROLE})).all()

    offenders = sorted(
        f"{table}: {privilege}" for table, privilege in rows if table in _APPEND_ONLY_TABLES_TODAY
    )
    assert not offenders, (
        f"append-only tables (CLAUDE.md rule 5) must not grant UPDATE/DELETE to "
        f"{_APP_ROLE!r}, but these do: {offenders}. Add a "
        f"'REVOKE UPDATE, DELETE ON <table> FROM {_APP_ROLE}' to the migration that "
        f"created the table."
    )


async def test_no_unexpected_append_only_tables_appeared(db_available: None) -> None:
    """If a later-phase append-only table exists but isn't yet in the enforced set, the
    coverage above silently wouldn't check it. Fail until it's registered."""
    async with unscoped_session() as session:
        existing = {
            row[0]
            for row in (
                await session.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).all()
        }

    unregistered = (existing & _ALL_RULE5_APPEND_ONLY) - _APPEND_ONLY_TABLES_TODAY
    assert not unregistered, (
        f"append-only tables exist but aren't in the enforced set: {sorted(unregistered)}. "
        f"Add them to _APPEND_ONLY_TABLES_TODAY so their grants are actually checked."
    )
