# Pyrrhula task backlog

One file per task. Phases 0–4 are decomposed to coding-agent granularity (self-contained
subtasks, falsifiable acceptance criteria, explicit module targets). Phases 5–6 are kept at
the development plan's granularity in one file per phase and **must be decomposed before
their phase starts** (mirroring the phase-0/1 format).

Authority: `../pyrrhula-development-plan.md` (v1.2). Task IDs match the plan's §15 tables.
The v1.2 additions (D15 — the software-development use case) appear as F3.13/F3.14 and
G4.15–G4.17; all three use cases (RPG, enterprise, swdev) ride the same domain-neutral core.

## How to work a task

1. Confirm every task in `Depends on:` has `Status: done`.
2. Set `Status: in-progress` (+ your agent/branch name), branch, implement.
3. Tick subtask checkboxes as you go; acceptance criteria define done — if one is untestable
   as written, flag it in the PR instead of reinterpreting it.
4. Default: **one task = one branch = one PR.** Keep the CI-blocking suites green
   (`tests/isolation/`, `tests/architecture/`, `tests/replay/`, `tests/leak/`, `tests/packs/`).
5. On merge, set `Status: done` and note any deviations in the task file.

Status values: `todo` · `in-progress` · `blocked` · `done`.

**Working a whole phase or track sequentially instead?** When a human explicitly assigns an
entire phase or track (e.g. "work through all of Track E, one branch, PR at the end"), use
`docs/phase-workflow.md` instead of the per-task loop above: one branch for the whole
track, one commit per task in dependency/numeric order, one PR at the end. That document
also covers the "build the tested piece, defer full-pipeline wiring" pattern for tasks that
depend on infrastructure a later phase hasn't built yet, and the end-of-track verification
checklist (whole-tree mypy, not just per-file `--strict`; an honest `EXIT-GATE.md` update).
This is how Phase 2 Track E (`E2.1`–`E2.12`) shipped — repeat it for the next phase/track.

## Phase overview

| Phase | Directory | Weeks | Exit criterion |
|---|---|---|---|
| 0 — Foundations & walking skeleton | `phase-0/` | 4–6 | Message round-trips through a 2-phase process for one tenant; isolation negative tests + INV-1 lint green and CI-blocking |
| 1 — Core loop MVP | `phase-1/` | 8–10 | The four-claim slice (below) runs end to end |
| 2 — Secrets, overseer, behavioural gate | `phase-2/` | 6–8 | `unauthorized_disclosure_rate == 0` on the exclusion arm across ≥3 providers |
| 3 — Entity framework + packs (rpg, enterprise, swdev) | `phase-3/` | 6–8 | INV-9: all three packs run with zero core diffs |
| 4 — Continuity, import/export, reporting, delegation | `phase-4/` | 7 | CCv3 imports and plays; `.pyr` round-trips; sanitised report leak-free; dogfood pilot (G4.17) completes one real backlog task |
| 5 — Enterprise overlay | `phase-5.md` | 8–10 | SSO; custom role; audit export; tamper-evidence verification |
| 6 — Scale & hardening | `phase-6.md` | ongoing | p95 turn latency < 8s @ 100 concurrent sessions |

## The Phase 1 exit slice (§15.2) — the whole bet

One tenant, one workspace, one Arbiter agent, one human player. A 3-phase ProcessDefinition
(`arbiter_narrate → player_act → resolve`) authored as data and edited in the UI. Three
KnowledgeSources (rules/lore/misc) with per-phase budget ratios, the difference visible in
the context inspector. A `dice_roller` that validates `1d20+STR` against the entity's actual
STR, executes seeded, writes a `ResolutionRecord`, and renders in the UI from the record. A
second tenant that provably cannot see any of it — asserted by a test that deliberately omits
the ORM filter.

**Explicitly NOT in the MVP:** secrets, behavioural parameters, multiple agents, the generic
entity framework (a hardcoded minimal schema suffices), import/export, reports, SSO, MCP.

## Dependency graph (critical path ★)

```
T0.1 → T0.2 → T0.3 ─┬→ A1.1 → A1.2 → A1.3 → A1.4 ─┐
                    │                A1.5 ────────┤
                    │                             ├→ A1.6 → A1.7 ─┐
                    ├→ B1.1 → B1.2 → B1.4 ────────┼───────────────┼→ C1.2 ★ → C1.3
                    │                             │               │        ↘
                    └→ C1.1 ──────────────────────┘               │         C1.4
                       C1.5 → C1.6 ★ ─────────────────────────────┘
                                                  ↓
                                        ═══ PHASE 1 GATE ═══
                                                  ↓
                       E2.1 → E2.5 → E2.6 ★★ → E2.8 ★  ═ PHASE 2 GATE (3-arm eval) ═
                                                  ↓
                       F3.1 → F3.4 ─┬→ F3.7 ──┬→ F3.9 ★ → F3.14  ═ PHASE 3 GATE (INV-9 ×3) ═
                                    ├→ F3.8 ──┤   ↓
                                    └→ F3.13 ─┘   ↓
                                            G4.* (parallelisable) → H5.* (customer-driven)
                                                  ↓
                       G4.12 → G4.16 ─┬→ G4.17 ★ (dogfood — closes the Phase 4 gate)
                       G4.15 ─────────┘
```

The three real bottlenecks: **C1.2** (ContextAssembler — freeze its signature in week 1 of
Phase 1), **E2.6 + E2.8** (exclusion + eval — author eval scenarios *before* the
implementation they judge), **F3.8/F3.9** (the enterprise pack exists to falsify the
genericity bet early — do not let it slip). The D15 lane (F3.13 → F3.14, G4.15/G4.16 →
G4.17) rides beside the critical path, not on it.

## Track ownership (suggested lanes for parallel agents)

| Track | Tasks | Surface |
|---|---|---|
| Infra | T0.1–T0.9 | repo, CI, tenancy, ports, auth, skeleton |
| A — Knowledge & retrieval | A1.1–A1.10 | `core/knowledge/`, worker ingestion/embedding |
| B — Process engine | B1.1–B1.7 | `core/process/`, `core/agents/` |
| C — Assembler & resolution | C1.1–C1.8 | `core/assembler/`, `core/resolution/` |
| D — Frontend | D1.1–D1.6 | `web/` |
| E — Secrets & behaviour (Phase 2) | E2.1–E2.12 | `core/secrets/`, `core/behavior/`, `core/overseer/`, `packages/eval/` |
| F — Entities & packs (Phase 3) | F3.1–F3.14 | `core/entities/`, `.plugins/`, `tests/packs/` |
| G — Continuity & portability (Phase 4) | G4.1–G4.14 | `core/portability/`, `core/reporting/`, `core/actions/`, `api/mcp_server/` |
| S — swdev / delegation lane (Phase 4, D15) | G4.15–G4.18 | repo ingestion, MCP delegation, dogfood pilot, per-agent git identity |

Tracks A, B and C1.1/C1.5 can run in parallel after Phase 0. C1.2 needs A1.7 + C1.1; C1.6
needs C1.5. Track D trails its backend counterparts.

## Reading this backlog in 2026 and later

Most of these files were written against a sandbox with no live models, no git remote, no
MCP server and in-tree packs. All four of those constraints are gone, so a scope note
saying "not wired yet" usually means *was* not wired. Where that is most misleading the
file carries a dated **Since then** banner; otherwise, check the code before believing a
gap. Four changes the backlog never absorbed, and which make older files read wrong:

**Two renames.** What the early tasks call a *model profile* is the `agent` table; what
they call an *agent* is the `persona` table (`persona_md`, `entity_id`, `persona_type ∈
supervisor|participant|informational`). There is no `model_profile` table and no
`agent.agent_role` column. Separately, `facilitator` is now a **workspace-membership**
role, not a persona type — and a `steward` role exists (the solo-creator seat that collapses
the author/overseer split) with no task file at all.

**`packs/` is gone.** Pack content lives in a pinned external repository
(`deploy/plugins.json` → `pyrrhula-workflows`), fetched by `scripts/fetch_plugins.py` into
gitignored `.plugins/`. Every `packs/rpg/…` path in F3.* is now a plugin path. This changes
what "INV-9 green in CI" means: `tests/architecture/test_packs_independence.py` scans
`.plugins/`, so a checkout where the fetch failed lints an empty corpus and passes
vacuously.

**The migrations were squashed.** ~80 incremental revisions became
`migrations/versions/a0000000b458_baseline.py`. Nine revision ids cited as evidence in
`done` tasks no longer resolve; that is the squash, not a missing migration.

**Shipped subsystems with no task file.** Steward role, plugin repositories, preview
environments, exec engines, usage limits, workspace clock, the assistant widget,
notifications, tenant workflows, and the config-as-settings work
(`tasks/config-as-settings.md`, the one loose file that is accurate). Absence from this
backlog is not evidence of absence from the product.
