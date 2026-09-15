# Phase 2 exit gate

**Status:** open — one arm measured on one live model; the ≥3-provider matrix is what
remains. **Not** blocked on credentials any more (see "What has since been measured").
**Plan refs:** §8.6, §15.5

This is the gate that decides whether §8 is right. Phase 2 is done when all of the following
are demonstrably true (each with a test, an eval run, or a recorded artefact):

- [ ] `unauthorized_disclosure_rate == 0` on arm 3 (exclusion) across ≥3 providers,
      where "disclosure" means **the plaintext escaped the assembler** — measured as
      `exposed_redactions` and `disclosed_verbatim`, not as the judge's
      `effectively_revealed`. **A non-zero value there is a P0 assembler bug, not a
      tuning problem** — the tokens were supposed to not be there.

      The distinction is load-bearing and was got wrong once. A holder *confessing in the
      fiction*, or a detective correctly inferring a secret from public evidence, raises
      `effectively_revealed` and is exactly what a working mystery does — no exclusion
      mechanism can or should prevent it. Judging the gate on that number would declare a
      P0 every time the game worked.
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

## What has since been measured

The paragraph that used to sit here said there were no live provider credentials anywhere
and that no run had ever produced real numbers. That stopped being true and the file did
not notice — the failure mode this whole gate exists to prevent, committed against itself.

Real three-arm runs are committed under `eval-results/` (`rescored-*.json`,
`glasshouse-*.json`), driven by `scripts/mystery_bench.py` →
`packages/eval/runner/mystery.py`. Each is a 25-turn session with a real blind judge.

**The exclusion arm holds.** `rescored-full_pipeline.json` reports `exposed_redactions: 0`
with no secret `disclosed_verbatim` and no `directive_leak` — nothing escaped the
assembler, which is the claim §8 makes. Its `unauthorized_disclosures: 1` is the holder
admitting the cellar-book fraud under questioning after the detective inferred it from
public evidence: the game working, not the wall failing. See the sharpened criterion above.

**What is genuinely still open** is breadth, not correctness: these are runs against one
local model, and the criterion asks for ≥3 providers. That is a data-collection gap now,
with the harness and the metrics proven against real sessions.

Closing the remaining four boxes requires someone running `.github/workflows/eval-nightly.yml`
(or the harness directly, `pytest packages/eval` plus a real matrix invocation) in an
environment with real provider credentials configured, then attaching the resulting
per-provider matrix and report artefact here. No amount of additional code in this branch
changes that — the gate is genuinely a data-collection step, not an implementation one, at
this point. This is stated here rather than left implicit, per this repo's own rule:
"If a criterion is untestable as written, say so ... rather than quietly reinterpreting it."
