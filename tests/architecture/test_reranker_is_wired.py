"""The reranker a deployment pays for is the reranker a turn uses.

Three surfaces promise reranking works: `reranker_enabled` defaults true, both installers
download the cross-encoder at setup, and the admin console offers a toggle and a model
name. For a long while none of them were connected to anything -- the assembler accepted a
`reranker` argument that no production caller ever supplied, so every deployment paid for a
model that never ran and an admin toggling it got silence.

This pins the seam rather than the behaviour: the composition root supplies one, and the
turn hands it to the assembler.
"""

from __future__ import annotations

import inspect


def test_the_turn_hands_its_reranker_to_the_assembler() -> None:
    from core.process.live_session import run_one_persona_turn

    source = inspect.getsource(run_one_persona_turn)
    assert "reranker=reranker" in source, (
        "run_one_persona_turn no longer passes its reranker into assemble() -- retrieval "
        "silently stops reranking while every config surface still says it is on"
    )


def test_the_api_composition_root_supplies_a_real_reranker() -> None:
    from api.routes import sessions

    source = inspect.getsource(sessions)
    assert "reranker=await get_reranker_for(tenant_id)" in source, (
        "the session routes stopped supplying a reranker; assemble() would fall back to "
        "None and rank on WRRF order alone"
    )


def test_disabling_it_in_the_admin_console_still_yields_none() -> None:
    """The documented degraded mode has to keep working: the switch is the one knob a
    tiny deployment has, and it must reach the assembler as an absent reranker rather
    than as an error. (An organization's own switch sits above this one, in
    ``get_reranker_for``, and is covered by the tenant-settings API tests.)"""
    from api.reranker_factory import get_reranker
    from core.deployment_settings import current_retrieval_models

    models = current_retrieval_models()
    original = models.reranker_enabled
    try:
        models.reranker_enabled = False
        assert get_reranker() is None
    finally:
        models.reranker_enabled = original
