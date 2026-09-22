"""An entity remembers which session filed it.

Entities are workspace-scoped on purpose -- a backlog is shared, and a standup, a triage
and a planning session all legitimately look at the same one. But nothing recorded where
each came from, so every view of them was the same view: a session panel listed thirty
items accumulated over eight runs, most stuck mid-lifecycle because until recently
nothing could put them down, and the six that session had just filed were
indistinguishable in the list.
"""

from __future__ import annotations

import inspect

from api.routes import entities


def test_the_listing_can_narrow_to_one_sessions_work() -> None:
    from api.main import app

    params = app.openapi()["paths"]["/entities"]["get"]["parameters"]
    assert "origin_session_id" in {p["name"] for p in params}


def test_the_listing_reports_the_origin_so_a_client_can_group_by_it() -> None:
    src = inspect.getsource(entities.list_entities_endpoint)
    assert "origin_session_id=row.origin_session_id" in src


def test_an_entity_with_no_origin_is_backlog_not_an_error() -> None:
    """Created through the API, imported from a bundle, seeded by a pack -- all real, and
    none of them belongs to a session. Nullable, and never backfilled with a guess."""
    from core.entities.storage import EntityRow

    assert EntityRow.__table__.c.origin_session_id.nullable


def test_deleting_a_session_does_not_delete_the_work_it_filed() -> None:
    """SET NULL, not CASCADE. The item outlives the conversation that produced it, which
    is the whole reason entities are workspace-scoped rather than session-scoped."""
    from core.entities.storage import EntityRow

    fk = next(iter(EntityRow.__table__.c.origin_session_id.foreign_keys))
    assert fk.ondelete == "SET NULL"


def test_the_creating_tool_records_the_session() -> None:
    from core.process import session_entity_tools

    src = inspect.getsource(session_entity_tools.make_entity_create_handler)
    assert "origin_session_id=ctx.session_id" in src
