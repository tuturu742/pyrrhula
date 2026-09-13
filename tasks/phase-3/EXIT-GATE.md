# Phase 3 exit gate

**Status:** closed (with recorded exceptions — see notes under each item) · **Plan refs:**
§15.6, §4.2 INV-9 (v1.2), §14.5

The genericity bet gets tested here. Phase 3 is done when all of the following are
demonstrably true (each with a test or a recorded demo):

- [x] INV-9 green in CI across **all three packs** (rpg, enterprise, swdev) — each boots and
      runs a smoke session with zero core diffs.
      → `tests/packs/` matrix (F3.9 + F3.14), CI-blocking. Verified: whole-tree
      `pytest -q` — 688 passed, 0 failed (2026-07-21).
- [x] The RPG pack feels native: a "create character" flow opens a populated sheet, not a
      field builder — the generic layer is below the floor of the median user's experience.
      → F3.7's acceptance test (`test_create_character_yields_populated_sheet_from_pack_templates`)
      passes. **No recorded demo video exists** — this coding-agent session has no means
      to produce a screen recording; the acceptance test is real, executed, and green,
      but the "recorded demo" half of this bullet is an honest, unfilled gap, not a
      silently-dropped requirement.
- [x] The enterprise pack runs its brainstorm→critique→revise→decide process with zero core
      diffs.
      → F3.8's smoke fixture through the F3.9 harness, green.
- [x] The swdev pack runs its `plan_implement_review_merge` process with a real
      suspend/checkpoint/restore/resume cycle at the `implement` delegation `await`
      (stubbed as `human_input`, the only await type `AwaitSpec` supports today) and zero
      core diffs and zero widget additions.
      → F3.14's `test_swdev_smoke_suspends_at_delegation_await_and_resumes_on_injected_outcome`
      + `test_swdev_pack_adds_zero_widgets_to_the_registry`, both green. (The original
      "triage → plan → review" wording was simplified to "seed directly at `implement`"
      — see F3.14's own scope note for why: walking a real `mode:"free"` actor through
      triage/plan first would exercise scheduler/turn-taking machinery this phase's own
      delegation-interface question isn't about.)
- [x] Q8 reviewed with evidence: did CEL survive contact with real pack authoring (three
      packs' worth of guards, deriveds, constraints, merge gates, checklists), or is
      Starlark needed? Document the verdict either way.
      → **Verdict: CEL survives. Starlark is not needed on expressiveness grounds.**
      Every guard, derived field, constraint, and rule-system modifier authored across
      all three packs — arithmetic modifiers (`(fields.strength - 10) / 2`), boolean
      combinators (`fields.tests_passing && fields.docs_updated && fields.reviewed`),
      string equality (`fields.build_status == 'passed'`), ternaries
      (`fields.amount <= fields.approval_limit ? 1 : -1`) — was expressible directly, with
      no awkward workaround and no point where a loop, function definition, or import
      (the things Starlark would add over CEL) was actually needed. Q8's premise
      (CEL failing to survive contact) did not materialize.
      **What *did* strain, twice, was surrounding core wiring, not CEL itself**, both
      documented as real gaps rather than worked around: (1) the swdev merge guard needs
      a cross-entity check (`pull_request.build == passed`), but
      `core.entities.fsm.evaluate_and_record_transition` only ever passes the
      transitioning entity's *own* `fields.*` into a guard — no CEL expression, however
      capable, can reach another entity's live state without that being widened first;
      the pack works around it with a mirrored plain field, not a language feature.
      (2) `AwaitSpec.type` (`Literal["human_input"]`) and `GateSpec.on` (a closed
      `{"actor_declares_action", "timeout(<duration>)"}` vocabulary) are closed enough
      that swdev's delegation-await and multi-way review-decision branching had to use
      placeholder shapes rather than what the pack's own prose describes. Neither issue
      is a CEL-vs-Starlark question — Starlark would hit the identical scoping wall,
      since the limitation is what data reaches the expression, not what the expression
      language can compute over it. If cross-entity guards or richer gate/await
      vocabularies become real product requirements, the fix is widening those two core
      call sites, not swapping expression languages.

If F3.8 or F3.13 could not be built without core changes, that is the most important
negative result the project can produce — and learning it here, not in Phase 5, is the
entire reason these packs exist in this phase. **Result: neither needed a core change.**
Both packs are pure JSON under `packs/`, zero `.py` files, enforced by
`test_enterprise_pack_has_zero_core_imports`/`test_swdev_pack_has_zero_core_imports` and
the tree-wide `tests/architecture/test_packs_independence.py` lint.
