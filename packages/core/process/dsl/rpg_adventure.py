"""A dual-mode RPG adventure flow that exercises the P1 entity tools in-session.

Same auto/directed shape as the round table (``core.process.dsl.fixtures``), plus two
tool-bearing phases: ``character_creation`` (each participant may call ``entity_create`` to
make their own sheet) and ``encounter`` (the GM/supervisor may call ``resolve_and_apply`` to
resolve a check and drive a character's state machine). Core-neutral keys throughout; the RPG
reading arrives via ``vocabulary_overlay`` only.

Whether a local model reliably emits those tool calls is a model-quality matter (upgradeable);
the mechanics themselves are covered deterministically by tests/isolation/test_rpg_pack.py.
"""

from __future__ import annotations

RPG_ADVENTURE_FLOW: dict[str, object] = {
    "name": "RPG Adventure",
    "vocabulary_overlay": "rpg_v1",
    "state": {
        "conductor_wrap_up": {"type": "boolean", "default": False},
    },
    "initial_phase": "framing",
    "phases": {
        "framing": {
            "label_key": "phase.framing",
            "actors": [{"persona_type": "supervisor", "mode": "generate"}],
            "visibility": {
                "knowledge_classes": ["rules", "lore"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {"ratio": {"rules": 0.3, "lore": 0.7}, "max_tokens": 3000},
            "on_complete": "character_creation",
        },
        "character_creation": {
            "label_key": "phase.discussion",
            "actors": [
                {
                    "persona_type": "participant",
                    "order": "declared",
                    "mode": "generate",
                    "max_turns": 8,
                }
            ],
            "visibility": {
                "knowledge_classes": ["rules"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {"ratio": {"rules": 1.0}, "max_tokens": 3000},
            "tools": ["entity_create"],
            "on_complete": "encounter",
        },
        "encounter": {
            "label_key": "phase.turn",
            "flags": ["conductable"],
            "actors": [{"persona_type": "supervisor", "mode": "generate", "max_turns": 8}],
            "visibility": {
                "knowledge_classes": ["rules", "lore"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {"ratio": {"rules": 0.6, "lore": 0.4}, "max_tokens": 3000},
            "tools": ["randomizer", "resolve_and_apply"],
            "on_complete": "synthesis",
        },
        "synthesis": {
            "label_key": "phase.synthesis",
            "actors": [{"persona_type": "supervisor", "mode": "generate"}],
            "visibility": {
                "knowledge_classes": ["rules", "lore"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {"ratio": {"rules": 0.3, "lore": 0.7}, "max_tokens": 3000},
        },
    },
}
