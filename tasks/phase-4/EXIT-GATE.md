# Phase 4 exit gate

**Status:** open — every criterion but one is met and recorded below; the dogfood pilot
(G4.17) is the one still outstanding, and `tasks/README.md` names it as *the* Phase-4 exit
criterion. This said "closed (with recorded exceptions)" while its headline box was
unticked, which is not what closed means. · **Plan refs:** §15.7, §11, §14.5

Phase 4 is done when all of the following are demonstrably true (each with a test or a
recorded artefact):

- [x] A CCv3 card imports and plays — its lore activates through the standard cascade, its
      persona speaks in a session.
      → G4.8's four acceptance tests pass, including decorator-faithful activation
      (`@@position`/`@@depth` win over an entry's plain fields) and byte-identical
      `extensions` preservation. **The "and plays" half is partial**: an imported card
      produces a real `Agent` with a persona and a published, chunked, retrievable
      `lore` source, and its entries activate through A1.5's fields — but no session has
      been *run* with one, because a live turn needs provider credentials this
      environment does not have. The recorded import-and-play demo does not exist for the
      same reason G4.17's pilot does not; this coding-agent session cannot produce a
      screen recording either.

- [x] A `.pyr` bundle round-trips lossless: export → wipe → import → the INV-10 replay test
      is still green on the imported session.
      → G4.5's `test_replay_from_bundle_alone_is_byte_identical` replays a turn from
      bundle contents with **no database in the loop**, and G4.6's
      `test_previous_format_bundle_upcasts_and_replays` does the same after a format
      upcast, then imports into a second tenant. The literal "wipe" step is not performed
      — replaying from the bundle alone is the stronger property, since it cannot
      accidentally read residual state. **Scope**: replay is proven for a knowledge-only
      turn; a turn whose context held a concealed secret is *correctly* not replayable
      from a sanitised bundle, and `core/portability/replay.py` says why.

- [x] `tests/leak/`: a sanitised export and a participant-mode report provably contain no
      held secret plaintext (exact + fuzzy + embedding scan of the artifacts).
      → G4.7's `test_sanitised_bundle_contains_no_held_secret_plaintext` scans **every
      byte of every file** in the archive (enumerated from the ZIP itself, so a file a
      future task adds is covered the day it appears); G4.10's
      `test_participant_report_contains_no_unheld_secret_content` scans the report and its
      stored row, using a provider double that *repeats whatever it is given* — so a clean
      recap means the secret was never in the prompt. G4.9 adds the same scan for card
      export. All CI-blocking.

- [x] MCP: an effectful third-party call suspends via `EffectfulAction`, survives a
      restart, and never double-executes.
      → G4.12's `test_effectful_mcp_call_survives_restart_without_double_execution` and
      G4.16's `test_restart_mid_delegation_reconciles_and_never_double_dispatches`. The
      record is written *before* the call and the outcome claimed atomically; a stranded
      dispatch is refused rather than retried, and G4.16 reconciles it by read-only
      lookup. **Scope**: "suspends" is proven on the action record rather than through an
      `await_state`, because `AwaitSpec.type` is still `Literal["human_input"]` — the same
      closed-vocabulary gap Phase 3's exit gate recorded under Q8. The restart property
      lives on the action record, which is where it actually belongs.

- [ ] **Dogfood (D15):** one task from this repository's `tasks/` backlog is proposed by
      the EM agent from ingested knowledge, implemented by a delegated coding agent over
      MCP (real branch + PR), reviewed in-session with a record-rendered verdict, and
      human-approved — with one forced restart mid-delegation producing no double-dispatch,
      and zero core diffs attributable to the swdev pack.
      → **Open, and open honestly.** All three of G4.17's named tests pass
      (`test_pilot_work_item_lifecycle_is_record_driven`,
      `test_pilot_restart_left_single_branch_pr_and_action_record`,
      `test_pilot_review_verdict_renders_from_record`), driving the whole loop over the
      shipped components — G4.15 ingesting this repository's own `CLAUDE.md` and `tasks/`,
      the swdev pack's real FSM and rule system read from `packs/swdev/` itself, G4.16's
      delegation with a forced restart, a real `ResolutionRecord`. The **zero-core-diffs**
      clause holds: `tests/packs/` and `tests/architecture/test_packs_independence.py` are
      green and `packs/swdev/` is still pure JSON.
      What did **not** happen is the recorded session with a *real* coding agent, a real
      branch, a real PR, and a real human approving a merge. It needs four things this
      environment does not have and code cannot supply: an MCP-reachable coding agent, a
      sandbox git remote, provider credentials, and a second person.
      `docs/dogfood-pilot.md` is the write-up, and it leads with that sentence rather than
      burying it. This box stays unchecked because the criterion says *recorded pilot
      session*, and the loop being tested is not the same claim.

The first four items prove the platform keeps its promises when content crosses its
boundary. The fifth proves the boundary is wide enough to hold a working team — including,
eventually, the team that builds this. **The machinery for the fifth is built and
exercised; the proof it asks for waits on an environment that can run it.**

## Recurring gaps across this phase, named once

Several tasks share the same two shapes of gap, recorded here so a reader does not have to
collect them from seventeen task files:

1. **No external services.** No MCP server, no mail server, no git remote, no provider
   credentials, no WeasyPrint system libraries. Every affected task ships the port, a
   deliberately honest default adapter (unreachable / log-only / named-error), and tests
   against a double. In each case the composition root — `worker/*_factory.py` — is the
   only file a real deployment changes.
2. **No UI for most of what Phase 4 built.** Export modes and the card loss report have
   surfaces; the import review queue, the report viewer, the MCP registry admin, the
   change feed, and the moderation attention feed do not. Each task's scope note says so.
   The endpoints are real and tested in every case.
