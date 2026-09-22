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


def test_entity_create_does_not_treat_every_entity_as_the_caller_itself() -> None:
    """`entity_create` carried assumptions that only hold when a persona has exactly one
    entity and that entity is itself.

    The first entity a persona ever created was bound as the persona's own, a cap then
    refused every later create, and every create reused one deterministic per-principal
    key. Observed live on a software-delivery flow twice: a lead asked for six work items
    created one, and -- because that work item had become the lead's own entity -- stayed
    capped in the *next* session too. Both runs delegated nothing and reviewed nothing,
    so the flow looked like it ran and touched no repository.

    Binding is now something the caller asks for (`bind_to_self`), the cap guards only
    that binding, and keys are per entity.

    Asserted on the source because the handler needs a live tenant, a persona and a
    schema to exercise; what regresses here is the *shape* of the rule, not its wiring.
    """
    import inspect

    from core.process import session_entity_tools as t

    src = inspect.getsource(t.make_entity_create_handler)
    # Binding is opt-in, not "whatever you created first".
    assert 'args.get("bind_to_self")' in src
    # The cap guards the self-binding alone.
    assert "if bind_to_self and existing_entity is not None:" in src
    # Keys are per entity, and no RPG word survives in core.
    assert 'f"{schema_key}-{uuid.uuid4().hex[:8]}"' in src
    assert "char-" not in src
    # Idempotency distinguishes the entities, not just their author.
    assert "{entity_key}" in src


def test_entity_create_tool_schema_offers_bind_to_self() -> None:
    """The tool description is what the model reads. It used to say the entity was
    'for yourself', which is why a lead believed it owned the work item it filed."""
    import inspect

    from core.process import live_session

    src = inspect.getsource(live_session)
    assert '"bind_to_self"' in src
    assert "character sheet) for yourself" not in src
