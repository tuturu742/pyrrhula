"""Fixture ProcessDefinitions (B1.1 subtask):  "Standard Session Flow"
example, translated into this module's concrete JSON shape (see ``schema.py``'s docstring
for why -- the plan's own YAML snippet uses informal ``-> target`` shorthand that isn't
valid, re-parseable syntax), and a minimal 3-phase MVP definition using core-neutral keys
(RPG labels arrive only through ``vocabulary_overlay`` label resolution, never hardcoded
here) that the golden interpreter test runs end to end.

Both are plain ``dict``s (not ``ProcessDefinitionDSL`` instances) so a caller exercises the
exact same ``validate_raw()`` entrypoint real authored JSON would go through.
"""

from __future__ import annotations

STANDARD_SESSION_FLOW: dict[str, object] = {
    "name": "Standard Session Flow",
    "vocabulary_overlay": "rpg_v1",
    "state": {
        "round": {"type": "integer", "default": 0},
        "scene_id": {"type": "string", "default": ""},
        "pending_feedback": {"type": "boolean", "default": False},
    },
    "initial_phase": "facilitator_frame",
    "phases": {
        "facilitator_frame": {
            "label_key": "phase.arbiter_narration",
            "actors": [{"persona_type": "supervisor", "mode": "generate"}],
            "visibility": {
                "knowledge_classes": ["rules", "lore", "misc"],
                "scopes": ["workspace_public", "facilitator_only"],
                "entity_fields": "all",
                "secrets": "held_by_actor",
            },
            "budget": {"ratio": {"rules": 0.3, "lore": 0.6, "misc": 0.1}, "max_tokens": 6000},
            "on_complete": "open_discussion",
        },
        "open_discussion": {
            "label_key": "phase.discussion",
            "actors": [
                {
                    "any_of": ["participant_agent", "human_participant"],
                    "mode": "free",
                    "max_turns": 12,
                }
            ],
            "visibility": {
                "knowledge_classes": ["lore", "misc"],
                "scopes": ["workspace_public"],
                "entity_fields": ["public"],
                "secrets": "held_by_actor",
            },
            "budget": {"ratio": {"lore": 0.8, "misc": 0.2}, "max_tokens": 4000},
            "gates": [
                {"on": "actor_declares_action", "to": "action_phase"},
                {"on": "timeout(24h)", "to": "action_phase"},
            ],
        },
        "action_phase": {
            "label_key": "phase.turn",
            "actors": [
                {"order": "initiative", "from": 'entity_field("initiative")', "mode": "generate"}
            ],
            "visibility": {
                "knowledge_classes": ["rules", "lore"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "held_by_actor",
            },
            "budget": {"ratio": {"rules": 0.75, "lore": 0.25}, "max_tokens": 5000},
            "flags": ["mechanical"],
            "tools": ["randomizer", "stat_calculator"],
            "on_complete": "resolution",
        },
        "resolution": {
            "label_key": "phase.resolution",
            "actors": [{"persona_type": "supervisor", "mode": "generate"}],
            # Not shown in the plan's illustrative snippet (visibility is mandatory --
            # every real phase needs one; the example just elides it for brevity).
            "visibility": {
                "knowledge_classes": ["rules"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "effects": [
                {"set": "round", "to": "state.round + 1"},
                {"set": "pending_feedback", "to": "state.round % 3 == 0"},
            ],
            "gates": [
                {"when": "state.pending_feedback", "to": "feedback_loop"},
                {"else": True, "to": "open_discussion"},
            ],
        },
        "feedback_loop": {
            "label_key": "phase.feedback",
            "actors": [{"human_participant": "all", "mode": "free"}],
            "visibility": {
                "knowledge_classes": [],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "await": {"type": "human_input", "timeout": "72h", "on_timeout": "open_discussion"},
            "on_complete": "open_discussion",
        },
    },
    # definition-level pacing. The feedback loop's await declares no reminder of its
    # own, so it inherits this one -- 24h into a 72h window, which is the shape the task's
    # own example names ("each actor has 48h, reminder at 24h") applied to this flow.
    "pacing": {"reminder_at": "24h"},
}


MINIMAL_MVP_FLOW: dict[str, object] = {
    "name": "Minimal MVP Flow",
    "vocabulary_overlay": "rpg_v1",
    "state": {"round": {"type": "integer", "default": 0}},
    "initial_phase": "arbiter_narrate",
    "phases": {
        "arbiter_narrate": {
            "label_key": "phase.arbiter_narrate",
            "actors": [{"persona_type": "supervisor", "mode": "generate"}],
            "visibility": {
                "knowledge_classes": ["rules", "lore"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {"ratio": {"rules": 0.3, "lore": 0.7}, "max_tokens": 3000},
            "on_complete": "player_act",
        },
        "player_act": {
            "label_key": "phase.player_act",
            "actors": [{"human_participant": "all", "mode": "free", "max_turns": 1}],
            "visibility": {
                "knowledge_classes": ["lore"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "on_complete": "resolve",
        },
        "resolve": {
            "label_key": "phase.resolve",
            "actors": [{"persona_type": "supervisor", "mode": "generate"}],
            "visibility": {
                "knowledge_classes": ["rules"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {"ratio": {"rules": 1.0}, "max_tokens": 2000},
            "effects": [{"set": "round", "to": "state.round + 1"}],
            "on_complete": "arbiter_narrate",
        },
    },
}


# A multi-persona discussion that runs EITHER autonomously OR human-conducted, chosen live
# per session via ``session.turn_policy`` (#7) -- not baked into the definition, so one
# pinned flow serves both modes:
#
#   * auto (turn_policy='auto'): framing -> (discussion -> regroup) x max_rounds ->
#     synthesis. Each round, every participant persona takes one model-generated turn (in
#     principal-id order via ``order: declared``), then the supervisor regroups and steers
#     (its agenda is injected there, core.process.live_session), until ``max_rounds`` rounds
#     have run. The discussion<->regroup alternation is what makes multi-round work: a
#     self-transition (discussion->discussion) would keep the same phase_key and so keep the
#     scheduler's "everyone already went" cursor (core.process.scheduler resets the cursor
#     only on a phase_key change), giving no second round. Bouncing through ``regroup`` flips
#     the phase_key each way, resetting the rotation for the next round.
#
#   * directed (turn_policy='directed'): the conduct-gated scheduler
#     (core.process.live_session) parks at ``discussion`` (flagged ``conductable``) instead
#     of auto-running participants -- a human overseer conducts each turn (a directed model
#     generation, or answering *as* a persona via the G4.4 override), then ends it, which
#     sets ``conductor_wrap_up`` and lets the discussion phase's first gate jump straight to
#     synthesis. ``regroup``/``max_rounds`` are an auto-mode concern the human paces manually.
#
# Core-neutral keys throughout; RPG (or any other) labels arrive only through
# vocabulary_overlay resolution, never hardcoded here.
AGENT_ROUND_TABLE_FLOW: dict[str, object] = {
    "name": "Persona Round Table",
    "vocabulary_overlay": "rpg_v1",
    "state": {
        "round": {"type": "integer", "default": 0},
        # How many participant rounds an autonomous run makes before synthesis. A directed
        # (human-conducted) run ignores this -- the overseer decides when to wrap up.
        "max_rounds": {"type": "integer", "default": 3},
        # Set by the overseer ending a conducted discussion (core.sessions.lifecycle
        # .set_conductor_wrap_up); the discussion phase's first gate keys on it to reach
        # synthesis. Stays False for a whole autonomous run.
        "conductor_wrap_up": {"type": "boolean", "default": False},
    },
    "initial_phase": "framing",
    "phases": {
        "framing": {
            "label_key": "phase.framing",
            "prompt": (
                "Open the session. Output well-structured markdown for human readers: a "
                "one-line objective, then '## Plan' with a numbered list of what this "
                "session will cover, then '## First step'. No conversational filler "
                "(never open with 'Sure' or 'Certainly'), no sign-off."
            ),
            "actors": [{"persona_type": "supervisor", "mode": "generate"}],
            "visibility": {
                "knowledge_classes": ["rules", "lore"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {"ratio": {"rules": 0.3, "lore": 0.7}, "max_tokens": 3000},
            "on_complete": "discussion",
        },
        "discussion": {
            "label_key": "phase.discussion",
            "prompt": (
                "Contribute your part. Structure your turn as markdown: '### Assessment' "
                "(short bullets reacting to what stands) and '### Proposal' (concrete "
                "next actions you commit to). Stay under ~200 words; no filler."
            ),
            # ``conductable``: the conduct-gated scheduler (core.process.live_session) parks
            # here in directed mode instead of auto-running the participant actor below.
            "flags": ["conductable"],
            # declared order + a cap larger than any realistic participant count: each
            # participant agent gets exactly one model-generated turn per round, in
            # principal-id order, then the entry is exhausted and the phase transitions.
            "actors": [
                {
                    "persona_type": "participant",
                    "order": "declared",
                    "mode": "generate",
                    "max_turns": 16,
                }
            ],
            "visibility": {
                "knowledge_classes": ["lore"],
                "scopes": ["workspace_public"],
                "entity_fields": ["public"],
                "secrets": "held_by_actor",
            },
            "budget": {"ratio": {"lore": 1.0}, "max_tokens": 3000},
            "gates": [
                # Directed wrap-up short-circuits straight to synthesis (no regroup round).
                {"when": "state.conductor_wrap_up", "to": "synthesis"},
                # Autonomous: after the round's participants are exhausted, the supervisor
                # regroups (and the round counter advances there).
                {"else": True, "to": "regroup"},
            ],
        },
        "regroup": {
            "label_key": "phase.regroup",
            "prompt": (
                "Regroup: summarize the round in '## Where we are' (bullets of decisions "
                "and open points) and '## Next' (what the coming round must settle). "
                "Markdown only, no filler."
            ),
            "actors": [{"persona_type": "supervisor", "mode": "generate"}],
            "visibility": {
                "knowledge_classes": ["rules", "lore"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {"ratio": {"rules": 0.3, "lore": 0.7}, "max_tokens": 3000},
            "effects": [{"set": "round", "to": "state.round + 1"}],
            "gates": [
                {"when": "state.round >= state.max_rounds", "to": "synthesis"},
                # Back to discussion for the next round -- the phase_key change resets the
                # scheduler's participant rotation so everyone speaks again.
                {"else": True, "to": "discussion"},
            ],
        },
        "synthesis": {
            "label_key": "phase.synthesis",
            "prompt": (
                "Deliver the final synthesis as a self-contained markdown document with "
                "clear '##' sections. Follow the agenda's requested deliverable exactly; "
                "no meeting minutes, no letter format, no sign-off."
            ),
            "actors": [{"persona_type": "supervisor", "mode": "generate"}],
            "visibility": {
                "knowledge_classes": ["rules", "lore"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {"ratio": {"rules": 0.3, "lore": 0.7}, "max_tokens": 3000},
            # No on_complete and no gates: the flow is terminal once the supervisor has
            # synthesized.
        },
    },
}
