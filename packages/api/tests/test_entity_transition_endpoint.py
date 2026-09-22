"""Moving an entity by hand.

Automation drives these state machines nearly always, and nearly always correctly. The
cases it gets wrong are the ones nobody can automate away: work merged somewhere no sweep
watches, a pull request that will never land, an item filed twice. Before this endpoint
the only correction was a direct database write -- unaudited, ungurarded, and invisible to
the history the rest of the system relies on.
"""

from __future__ import annotations

import inspect

from api.routes import entities


def test_the_endpoints_exist_and_are_shaped_for_a_person() -> None:
    """A correction needs two calls: what can I do from here, and do it. Offering only
    the second means finding out a trigger was illegal by being refused."""
    from api.main import app

    paths = app.openapi()["paths"]
    assert "get" in paths["/entities/{entity_id}/transitions"]
    assert "post" in paths["/entities/{entity_id}/transition"]


def test_a_manual_transition_is_permission_checked_like_any_mutation() -> None:
    src = inspect.getsource(entities.transition_entity_endpoint)
    assert 'require_permission(ctx, "entity:mutate", "workspace"' in src


def test_it_goes_through_the_real_transition_not_around_it() -> None:
    """The guard, the append-only state-change row and the version bump all live in
    ``core.entities.mutation.transition``. A hand correction that wrote the column
    directly would skip all three -- which is exactly what it is replacing."""
    src = inspect.getsource(entities.transition_entity_endpoint)
    assert "entity_transition(" in src
    assert 'cause="human"' in src, "the history has to say a person did this"


def test_a_manual_call_is_not_deduplicated() -> None:
    """Idempotency keys exist so automation can replay without double-acting. A person
    pressing the same button twice means it twice, so the key is unique per call."""
    src = inspect.getsource(entities.transition_entity_endpoint)
    assert "uuid.uuid4()" in src


def test_a_stale_version_is_refused_rather_than_overwritten() -> None:
    src = inspect.getsource(entities.transition_entity_endpoint)
    assert "expected_version=body.expected_version" in src


def test_guarded_transitions_are_shown_but_not_offered() -> None:
    """Hiding a blocked transition entirely leaves someone asking why the move they
    remember is missing; showing it with allowed=False answers that."""
    src = inspect.getsource(entities.list_entity_transitions_endpoint)
    assert "guard_passes(" in src
    assert "blocked_by_guard=not passes" in src


def test_wildcard_transitions_are_offered_from_every_state() -> None:
    """`abandon` is declared from `*`. A listing that only matched exact from-states
    would never offer it, which would make the state unreachable by hand -- and it exists
    precisely for the cases only a person can judge."""
    src = inspect.getsource(entities.list_entity_transitions_endpoint)
    assert "t.from_state not in (current, ANY_STATE)" in src
