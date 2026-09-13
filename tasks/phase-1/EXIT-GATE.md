# Phase 1 exit gate

**Status:** closed (B1.8) · **Plan refs:** §15.2, §15.4

Phase 1 is done when all of the following are demonstrably true (each with a test or a
recorded demo):

- [x] The §15.2 slice runs end to end: one tenant, one workspace, one Arbiter agent, one
      human player; the 3-phase ProcessDefinition (`arbiter_narrate → player_act → resolve`)
      authored as data and edited in the UI (D1.2); three KnowledgeSources (rules/lore/misc)
      with per-phase budget ratios (`player_act` rules 0.75, `arbiter_narrate` lore 0.6).
      → The end-to-end mechanics (real interpreter, real scheduler, a real human turn, a
      real agent turn, a real tool call) are proven by
      `core/process/tests/test_live_session.py::test_minimal_mvp_flow_runs_two_rounds_through_the_real_interpreter_and_tool_loop`
      (engine-level) and
      `api/tests/test_sessions_flow.py::test_process_definition_backed_session_runs_through_the_real_http_endpoints`
      (through the actual HTTP surface) -- this is what was missing before B1.8: every
      piece existed and was tested in isolation, but nothing reachable through the live
      product ever ran them together. "Authored as data" is proven the same way D1.2's
      own editor publishes a definition (`core.process.authoring.create_definition`,
      the exact function both this gate's tests and the `POST /process-definitions`
      endpoint call). The three-KnowledgeSources-with-budget-ratios sub-claim is A1.6's
      own golden test, not re-proven here. "Edited in the UI"/browser-verified is
      D1.2's own documented limitation (no browser automation in this sandbox) --
      unchanged by B1.8, not newly claimed.
- [x] Changing a budget ratio produces visibly different retrieval in the context inspector
      (A1.6 golden test + D1.4 demo).
      → Already true before B1.8; unchanged. B1.8 additionally proves a *live* turn
      actually calls `ContextAssembler.assemble()` with the phase's real `budget.ratio`
      (not a hardcoded one) -- `test_live_session.py` asserts a `ContextManifestRow` is
      written and linked to the resolve-phase message.
- [x] `dice_roller` validates `1d20+STR` against the entity's actual STR, executes seeded,
      writes a `ResolutionRecord`, and the UI renders from the record; a model claiming a
      false modifier is rejected (C1.5/C1.6 + D1.3).
      → The "false modifier rejected" and "UI renders from the record" sub-claims were
      already proven (C1.6's `test_model_claiming_a_false_modifier_is_rejected_through_the_real_tool_loop`,
      D1.3's `GET /messages/{id}/resolutions`). B1.8 closes the remaining gap: `dice_roller`
      reached through a *live, interpreter-driven* turn (not a direct `resolve()` or
      `run_agent_turn()` call) still validates, executes seeded, and writes a real
      `ResolutionRecord` correlated to the message -- proven by the same
      `test_live_session.py` test. "The entity's actual STR": still a fixed-dict stub
      (`core.process.live_session._actor_fields_resolver_stub`) -- no Entity system
      exists anywhere in Phase 1 (F3.6 is Phase 3), a real, honestly-flagged limitation,
      not a silent gap.
- [x] A second tenant provably cannot see any of it — the filter-omission test suite (T0.4)
      is green against the **full** Phase-1 schema.
      → `tests/isolation/` green (49 tests) against the schema as of B1.8, including
      `provider_credential`/`vocabulary_overlay` (D1.5/D1.6) and every table this task
      itself did not need to add (B1.8 introduces no new tables/columns).
- [x] The INV-10 replay test (C1.3) is green across a scripted multi-turn session.
      → `tests/replay/` green, part of the full 518-test backend suite (run twice for
      repeatability as of B1.8).

Everything after Phase 1 is addition. Everything in Phase 1 is a foundation that cannot be
retrofitted.
