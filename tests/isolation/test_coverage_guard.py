"""Coverage guard: every tenant_id-bearing table must have an explicit
filter-omission test, not just an RLS policy. ``test_rls_catalog.py`` proves the policy
exists; this proves *someone actually exercised it* — the two failure modes are
different (a policy can exist and still be wrong, e.g. the NULLIF gotcha).

When this fails after adding a table, the fix is almost always: add a row-seeding helper
and a filter-omission test for the new table in ``test_filter_omission_matrix.py``, then
add its name here.
"""

from __future__ import annotations

from sqlalchemy import text

from core.tenancy.scope import unscoped_session
from tests.isolation.test_rls_catalog import _DOCUMENTED_NO_RLS_EXCEPTION

# Tables with an explicit filter-omission test: test_filter_omission_matrix.py, or
# test_tenant_scope_smoke.py (`principal`/`workspace`), or
# test_walking_skeleton_filter_omission.py, or
# test_knowledge_filter_omission.py, or test_secret_tables.py, or
# test_behavior_profile.py, or test_entity_schema.py, or
# packages/core/agents/tests/test_editing.py, or
# test_between_session_state.py, or test_async_pacing.py, or
# test_report_pipeline.py, or test_mcp_client.py.
_COVERED_TABLES = {
    # Has test_persona_git_credential_filter_omission in the matrix but was never
    # registered here, so the guard has been failing for it.
    "persona_git_credential",
    "entry_activation_state",
    "principal",
    "tenant_mcp_capability",
    "exec_environment",
    "preview_environment",
    # test_image_lifecycle.py
    "image_definition",
    "image_build",
    "identity",
    "membership",
    "workspace",
    "workspace_membership",
    "vector_store_item",
    "audit_log",
    "completed_operation",
    "agent",
    "persona",
    "session_persona",
    "session",
    "session_event",
    "message",
    "usage_record",
    "knowledge_source",
    "knowledge_source_version",
    "knowledge_entry",
    "knowledge_chunk",
    "workspace_knowledge_attachment",
    "process_definition",
    "checkpoint",
    "await_state",
    "scope",
    "context_manifest",
    "rule_system",
    "resolution_record",
    "tool_definition",
    "provider_credential",
    "vocabulary_overlay",
    "secret",
    "secret_holder",
    "secret_disclosure_event",
    "disclosure_decision",
    "axis_definition",
    "behavior_profile",
    "entity_schema",
    "entity_state_change",
    "entity",
    "persona_version",
    "entity_schedule",
    "notification",
    "report",
    "mcp_server",
    "mcp_call_record",
    "registration_request",
    "action_record",
    # test_workflow_authoring.py (moddable workflows)
    "workflow",
    # test_repo_registry.py (repo registry + per-session selection)
    "repo",
    "session_repo",
}


async def test_every_rls_table_has_explicit_filter_omission_coverage(
    db_available: None,
) -> None:
    async with unscoped_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.relname
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relkind = 'r'
                      AND c.relrowsecurity
                      AND c.relforcerowsecurity
                    """
                )
            )
        ).all()

    tenant_scoped_tables = {row[0] for row in rows}
    assert _DOCUMENTED_NO_RLS_EXCEPTION not in tenant_scoped_tables  # sanity: it has no RLS

    uncovered = tenant_scoped_tables - _COVERED_TABLES
    assert not uncovered, (
        f"RLS-covered tables with no filter-omission test: {sorted(uncovered)}. Add one "
        f"in tests/isolation/test_filter_omission_matrix.py and register it in "
        f"_COVERED_TABLES (tests/isolation/test_coverage_guard.py)."
    )

    stale = _COVERED_TABLES - tenant_scoped_tables
    assert not stale, f"_COVERED_TABLES references tables that no longer exist: {sorted(stale)}"
