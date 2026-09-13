# Phase 2 exit gate

**Status:** open — blocked on live provider credentials, not on unbuilt code (see note)
**Plan refs:** §8.6, §15.5

This is the gate that decides whether §8 is right. Phase 2 is done when all of the following
are demonstrably true (each with a test, an eval run, or a recorded artefact):

- [ ] `unauthorized_disclosure_rate == 0` on arm 3 (exclusion) across ≥3 providers.
      → E2.8 nightly matrix. **A non-zero value is a P0 assembler bug, not a tuning
      problem** — the tokens were supposed to not be there.
- [ ] Arm 3 beats arm 2 (deliberation-without-exclusion) decisively.
      → E2.8 three-arm comparison. **If it doesn't, §8 is wrong — stop and redesign, don't
      ship.**
- [ ] `over_concealment_rate` is acceptable — agents aren't mute.
      → E2.8 metric + spot-review of transcripts. This is the metric most likely to be
      ignored; don't.
- [x] INV-5 holds: no code path reads secret plaintext without an audit row in the same
      transaction.
      → `test_inspect_read_and_audit_row_are_atomic` (E2.10) +
      `test_only_assembler_and_overseer_import_knowledge_or_secrets_repo` (T0.4/INV-1
      lint) — both pass on this branch. The MCP surface (E2.12) goes through the same
      `OverseerService.inspect()` call, proven identical to the UI surface's own audit-row
      shape by `test_mcp_and_ui_inspections_write_identical_audit_shape`.
- [ ] The three-arm results are written up in publishable form (per-provider matrix,
      methodology, numbers).
      → E2.8 report artefact — the marketing asset and the Q12 prerequisite (never the
      adjective without the numbers).

The wall is exclusion; everything else is tripwires and paperwork. If the wall needs the
tripwires to hit zero, the wall is broken.

## Why the other four boxes are still open

This is an environment limitation, not a missing-code gap: **there are no live model
provider credentials anywhere in this sandbox** (no Anthropic/OpenAI/etc. API keys, no
reachable Ollama instance — verified directly, not assumed). E2.8's own task file already
disclosed this: the eval harness (scenarios, three-arm matrix assembly, five metrics, the
blind-judge payload construction, the nightly CI workflow skeleton) is real and tested
against scripted/fake providers (`packages/eval/tests/test_harness.py`), but no run of it
has ever produced real `unauthorized_disclosure_rate` / `over_concealment_rate` numbers
against ≥3 actual providers, because none are reachable here.

Closing the remaining four boxes requires someone running `.github/workflows/eval-nightly.yml`
(or the harness directly, `pytest packages/eval` plus a real matrix invocation) in an
environment with real provider credentials configured, then attaching the resulting
per-provider matrix and report artefact here. No amount of additional code in this branch
changes that — the gate is genuinely a data-collection step, not an implementation one, at
this point. This is stated here rather than left implicit, per this repo's own rule:
"If a criterion is untestable as written, say so ... rather than quietly reinterpreting it."
