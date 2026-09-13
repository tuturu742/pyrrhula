# Dogfood pilot write-up (G4.17, D15)

**Status: the loop is built and tested; the pilot session was not run.**

That sentence is the write-up's most important one, so it goes first. G4.17 asks for a
*recorded pilot session* — this repository ingested as knowledge, an Engineering Manager
agent proposing the next task from the backlog, an engineer agent's work delegated to a
real coding agent over MCP producing a real branch and a real pull request, a second agent
reviewing in-session, and a human Engineering Director approving the merge — plus tests
over that session's artifacts.

The tests exist and are real (`tests/packs/test_dogfood_pilot.py`). The session does not.

## Why the session was not run

Four things it needs are absent from the environment this phase was built in, and none of
them is something code can supply:

1. **No coding agent reachable over MCP.** G4.12 ships `UnreachableMcpTransport`
   deliberately — there is no MCP server here, and a transport that pretended otherwise
   would be untested code in the place where "it looked like it worked" is most expensive.
2. **No sandbox git remote.** The pilot's own design decision is a fork/sandbox remote
   rather than the live repository; neither is reachable, and the delegated agent is what
   would push to it anyway.
3. **No provider credentials.** The EM's proposal, the engineer's plan, and the reviewer's
   critique are all model calls. `model_profile.credential_ref` points into a secret
   manager that holds nothing in this deployment.
4. **No second human.** The Director's merge approval is a person's decision. It is the
   one step in the loop that cannot be automated by design, which is precisely why it is
   in the loop.

This is the same class of gap E2.8's eval harness recorded: the machinery is complete and
exercised, and the *data collection* did not happen. Saying so is the point. A pilot
write-up describing a session that did not occur would be the worst artefact this project
could produce, given that the project's entire claim is that records beat prose.

## What was exercised, and how

`tests/packs/test_dogfood_pilot.py` drives the loop over the shipped components. The only
substitution is the external coding agent, replaced by a double that models the far side's
*state* — which branches exist, with which PR reference and CI status — rather than merely
echoing. That distinction matters: it is what lets the restart test be meaningful, because
the double does the work and *then* drops the connection, leaving something real for
reconciliation to find.

| Pilot stage | What actually ran |
|---|---|
| workspace setup | G4.15 ingests `CLAUDE.md` → `rules` and `tasks/**` → `lore` at a pinned SHA |
| swdev pack | `work_item` schema and `checklist_v1` rule system read from `packs/swdev/` itself, not copied |
| triage | the EM's backlog is the ingested `tasks/` entry, asserted retrievable |
| plan | `refine` → `start` through the pack's real FSM |
| implement | G4.16 `delegate_work_item`: brief from `assemble()`, `EffectfulAction`, branch derived from the idempotency key |
| forced restart | the record is set dispatched-but-unresolved; the resume reconciles |
| review | `checklist_eval` through the real resolution service, writing a real `ResolutionRecord` |
| merge | the Director's approval recorded as a `session_event`; the transition cites it |

All three named acceptance tests pass:

* `test_pilot_work_item_lifecycle_is_record_driven` — the work item walks
  backlog→ready→in_progress→in_review→approved→merged→done, and **every** recorded FSM
  state change carries a `cause` and a `cause_ref` pointing at a resolution record, an
  action record, or a recorded human decision. Nothing moved because a model said so.
* `test_pilot_restart_left_single_branch_pr_and_action_record` — one dispatch, one branch,
  one action record, outcome `reconciled`.
* `test_pilot_review_verdict_renders_from_record` — the verdict renders from the
  `ResolutionRecord` by id; the coding agent's summary claims a red build and a merge that
  never happened, and changes nothing.

## What the loop taught us anyway

Three things surfaced while wiring it, all of them decisions rather than bugs:

**An overseer cannot write entity state, and that is correct.** The Director holds
`overseer`, which by E2.10's model carries no `entity:mutate` — an overseer sees and
decides. The pilot's merge approval therefore records a `session_event` and the transition
cites it, rather than the Director mutating the work item directly. Granting the overseer
write permission would have been one less indirection and a quiet change to the oversight
model; the indirection is the honest shape.

**The delegation brief should retrieve against the work item, not against delegation.**
The first implementation used a fixed `"delegate work item <id>"` query, which retrieved
nothing useful — a brief exists so the agent knows what it is doing. It now queries with
the work item's own name and string fields.

**`AwaitSpec.type` is still `Literal["human_input"]`**, so the `implement` suspend is
modelled as a human await rather than a delegation await. This is the same Q8-adjacent
gap Phase 3's exit gate recorded: the limit is the *await vocabulary*, not the delegation
machinery, and G4.16's restart property is proven directly on the action record, which is
where it actually lives.

## What a real pilot would still test that this does not

Named so the next person does not have to infer them:

* Whether the EM's proposal is *good* — whether retrieval briefs it well enough to pick a
  sensible next task, which is the §15.9 code-aware-chunking trigger's real evidence.
* Whether the delegated agent's output is usable, and whether the review rubric catches
  what a human reviewer would.
* Whether the process templates chafe in practice — where a phase boundary falls in the
  wrong place once real people are waiting on each other.
* Real cost. `usage_record` rows for delegated calls carry zero token counts unless the
  coding agent reports them (see G4.16's scope note), so cost attribution for delegated
  work is untested against real numbers.

## Zero core diffs attributable to the swdev pack

INV-9's claim held through this task: `tests/packs/test_pack_matrix.py` and
`tests/architecture/test_packs_independence.py` stay green, and `packs/swdev/` remains
pure JSON with no `.py` files. The pilot reads the pack's schema and rule system from
`packs/swdev/` directly rather than from a copy in the test, so a pack that drifted would
fail the pilot rather than pass it.
