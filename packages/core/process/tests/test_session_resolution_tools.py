"""The resolution preset's synthetic phase must be a VALID PhaseSpec: it is built on
every live turn in any workspace whose workflow registers a resolution server, so a
schema drift here (e.g. an ``entity_fields`` literal the DSL doesn't accept) crashes
all generation for game-like tenants before the model is ever called."""

from core.process.session_resolution_tools import _resolution_phase


def test_resolution_phase_is_a_valid_phase_spec() -> None:
    phase = _resolution_phase(["randomizer", "checklist_eval"])
    assert phase.tools == ["randomizer", "checklist_eval"]
    assert phase.visibility.entity_fields == []
    assert phase.visibility.secrets == "none"


def test_resolution_phase_accepts_empty_tool_list() -> None:
    assert _resolution_phase([]).tools == []


def test_entity_create_key_and_cap_are_not_character_shaped() -> None:
    """`entity_create` carried three assumptions that only hold for RPG characters.

    A persona could own one entity ever ("this persona already has a character"), every
    create reused one deterministic `char-<principal>` key, and the idempotency key was
    per-persona-per-session. All three encode "a player has one character". Observed
    live on a software-delivery flow: a lead asked for six work items created one, was
    refused five times, and the session then had nothing to delegate and reviewed
    nothing -- a flow that looked like it ran and touched no repository.

    Asserted on the source because the handler needs a live tenant, a persona and a
    schema to exercise; what regresses here is the *shape* of the rule, not its wiring.
    """
    import inspect

    from core.process import session_entity_tools as t

    src = inspect.getsource(t.make_entity_create_handler)
    # The cap is scoped to the schema the persona is actually bound to.
    assert "bound_schema == schema_key" in src
    # Keys are per entity once a persona already owns one.
    assert "uuid.uuid4().hex[:8]" in src
    # Idempotency distinguishes the entities, not just their author.
    assert "{entity_key}" in src
