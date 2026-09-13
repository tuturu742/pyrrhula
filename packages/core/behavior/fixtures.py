"""RPG axis-pack fixtures (E2.3's own subtask): the §8.2 examples (talkativeness,
cooperativeness, secret_disclosure_propensity, deception_propensity), used by this task's
validation fixtures and by E2.5's disclosure gate tests. The enterprise axis pack lands
in F3.8 and the swdev axis pack (`review_strictness`, `escalation_propensity`,
`risk_tolerance` with `stakes: high`) in F3.13 -- this module is the machinery's own
proof it works, not a claim about pack completeness.

Plain `dict`s, not `AxisDefinitionSchema` instances, matching
`core.process.dsl.fixtures`'s own reasoning: a caller exercises the exact same
`AxisDefinitionSchema.model_validate()` entrypoint real pack-authored JSON would go
through.
"""

from __future__ import annotations

RPG_AXIS_PACK_ID = "rpg_v1"

TALKATIVENESS: dict[str, object] = {
    "pack_id": RPG_AXIS_PACK_ID,
    "key": "talkativeness",
    "label_key": "axis.talkativeness",
    "range_min": 0,
    "range_max": 100,
    "stakes": "low",
    "semantics_md": "How much unprompted dialogue/narration the agent volunteers per turn.",
    "bindings": [
        {
            "kind": "prompt_directive",
            "bands": [
                {
                    "min": 0,
                    "max": 33,
                    "text": "You speak rarely, only when you have something substantive to add.",
                },
                {
                    "min": 34,
                    "max": 66,
                    "text": "You speak about as often as anyone else at the table.",
                },
                {
                    "min": 67,
                    "max": 100,
                    "text": "You volunteer dialogue and narration freely, often unprompted.",
                },
            ],
        }
    ],
}

COOPERATIVENESS: dict[str, object] = {
    "pack_id": RPG_AXIS_PACK_ID,
    "key": "cooperativeness",
    "label_key": "axis.cooperativeness",
    "range_min": 0,
    "range_max": 100,
    "stakes": "low",
    "semantics_md": "How readily the agent assists other participants' stated goals.",
    "bindings": [
        {
            "kind": "prompt_directive",
            "bands": [
                {"min": 0, "max": 50, "text": "You pursue your own goals over others' requests."},
                {"min": 51, "max": 100, "text": "You readily help with others' stated goals."},
            ],
        },
        {"kind": "sampling"},
    ],
}

# stakes:high (§8.3) -- this is the axis E2.5's disclosure gate prefilter and posture
# both key off. A prompt-only version of this axis is exactly the "malice slider that
# can't be authored at all" the validation rule exists to prevent. The prompt_directive
# binding here renders *in addition to* the gate binding, never instead of it -- E2.3's
# own rule is what guarantees the gate is present at all.
SECRET_DISCLOSURE_PROPENSITY: dict[str, object] = {
    "pack_id": RPG_AXIS_PACK_ID,
    "key": "secret_disclosure_propensity",
    "label_key": "axis.secret_disclosure_propensity",
    "range_min": 0,
    "range_max": 100,
    "stakes": "high",
    "semantics_md": (
        "How readily the agent's held secrets surface under pressure -- enforced by the "
        "disclosure gate (E2.5), not by the model's own restraint."
    ),
    "bindings": [
        {"kind": "gate"},
        {
            "kind": "prompt_directive",
            "bands": [
                {
                    "min": 0,
                    "max": 50,
                    "text": "You are guarded about things you'd rather not share.",
                },
                {"min": 51, "max": 100, "text": "You let things slip more easily than you should."},
            ],
        },
    ],
}

DECEPTION_PROPENSITY: dict[str, object] = {
    "pack_id": RPG_AXIS_PACK_ID,
    "key": "deception_propensity",
    "label_key": "axis.deception_propensity",
    "range_min": 0,
    "range_max": 100,
    "stakes": "high",
    "semantics_md": (
        "How willing the agent is to actively mislead (vs. merely conceal) when a held "
        "secret is under pressure -- feeds the disclosure gate's `posture` output."
    ),
    "bindings": [{"kind": "gate"}],
}

RPG_AXIS_PACK: tuple[dict[str, object], ...] = (
    TALKATIVENESS,
    COOPERATIVENESS,
    SECRET_DISCLOSURE_PROPENSITY,
    DECEPTION_PROPENSITY,
)

# A deliberately-invalid fixture (E2.3's own acceptance criterion): stakes:high with no
# gate binding at all -- what `validate_axis_definition` must reject.
INVALID_HIGH_STAKES_WITHOUT_GATE: dict[str, object] = {
    "pack_id": RPG_AXIS_PACK_ID,
    "key": "malice",
    "label_key": "axis.malice",
    "range_min": 0,
    "range_max": 100,
    "stakes": "high",
    "semantics_md": "A stakes:high axis with only a prompt directive -- must fail validation.",
    "bindings": [{"kind": "prompt_directive"}],
}
