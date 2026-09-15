# Working a whole phase (or track) sequentially — one branch, one PR

`tasks/README.md`'s default is **one task = one branch = one PR**, and that's still right
for parallel, per-task work. This document is the alternative mode: a single agent (or
session) assigned an entire phase, or an entire track within a phase (e.g. Phase 2's Track
E, `E2.1`–`E2.12`), works every task in that track **sequentially, on one branch, with one
commit per task, and opens exactly one PR at the end.** Use this mode when a human
explicitly asks for it ("work through the whole track", "don't open a PR per step"); default
to the per-task mode otherwise.

This document describes the process that shipped Phase 2 Track E (`E2.1`–`E2.12`,
PR #4) — not a hypothetical, the actual sequence, kept here so the next phase repeats it
instead of rediscovering it.

## 1. One branch, named for the track

Branch once, off `master`, named for the phase/track (e.g.
`feat/phase-2-secrets-behaviour-gate`). Every task in the track lands on this branch as its
own commit. No per-task branches, no per-task PRs — the PR happens once, at the end,
covering the whole track.

## 2. Strict task order — do not jump to "critical path" tasks

Work tasks in the order their own `Depends on:` fields and numeric IDs imply
(`E2.1 → E2.2 → … → E2.12`), even if the development plan's own dependency graph shows a
shorter critical path through the middle (e.g. `E2.1 → E2.5 → E2.6 → E2.8`). Jumping ahead
to "the important tasks first" leaves gaps that are awkward to backfill once later tasks
have already been built against assumptions the skipped ones would have settled, and it
makes the eventual PR's commit history harder to follow than the task numbering it's
supposed to mirror. If a later task's implementation reveals that an earlier, already-done
task needs a change, fix it there and amend forward — don't reorder the branch's commits
around it.

## 3. Per-task loop

For each task, in order:

1. **Read the task file.** Confirm every task named in its `Depends on:` is `done`. Read
   its acceptance criteria closely — they name the exact test functions that must exist and
   pass; they are the definition of done, not a suggestion.
2. **Design against the hard rules first**, before writing any code: vocabulary neutrality
   (no domain words in `core/`), INV-1 (only `core/assembler/` and `core/overseer/` import
   `core/knowledge/repo` or `core/secrets/repo`), INV-2/INV-4 (`assemble()`'s frozen
   signature, `scope_key` pushed down as a SQL predicate), tenancy/RLS shape, append-only
   grants, idempotency keys, the `usage_record.purpose` taxonomy (`generation | gate |
   rerank | embed | report | rewrite | delegation` — **no other values, do not invent
   one**), and the ports-not-features pattern. See CLAUDE.md's "Hard rules" section for the
   full list — it is not optional reading.
3. **Implement**: models/migration → apply the migration against a real local database →
   repo/service functions → API routes if the task calls for them → frontend if the task
   calls for it.
4. **Write tests matching the acceptance criteria's literal names.** If a criterion is
   untestable as written (missing infrastructure, an external dependency this environment
   doesn't have), say so directly in the task file's scope note — never quietly reinterpret
   it into something easier to satisfy.
5. **Format, lint, and type-check the changed files**: `ruff format`, `ruff check`,
   `mypy --strict <changed files>`.
6. **Run the full test suite**, not just the new tests, before moving on. A task that
   passes its own tests but breaks an earlier one is not done.
7. **Update the task file itself**: set `Status: done` (or `done (partial — see scope
   note)` when something was deliberately deferred), tick every subtask checkbox that's
   actually complete, and add a scope-note blockquote at the top naming exactly what was
   deferred and why (see §4).
8. **Commit** — one commit per task, git-log style: an imperative one-line summary
   (`E2.10: Overseer service -- permission-gated, atomically-audited plaintext read path`),
   a body explaining what changed and *why* (design decisions, invariants preserved,
   what the next task will build on), and a `Co-Authored-By:` trailer. No PR yet.
9. Move to the next task.

## 4. "Build the tested piece, defer full-pipeline wiring"

Tasks routinely depend on infrastructure that doesn't exist yet in an earlier phase: a live
turn loop, a nightly job runner, an MCP transport server. Building that missing
infrastructure early, just to satisfy one task, is usually the wrong call — it's someone
else's task, later, and building it out of order risks guessing its shape wrong. The
pattern used throughout Track E instead: build the piece itself fully, with real tests, and
call it from wherever it will eventually be called *once that caller exists* — documented,
not hidden. Concretely, this looked like:

> **These examples are history, not status.** Every gap listed below has since been
> closed: the disclosure gate and the post-generation check run on live turns
> (`core/process/live_session.py`), the eval harness has produced real numbers against a
> live model (`eval-results/`), and the MCP server exists (`packages/api/mcp_server/`,
> `tests/isolation/test_mcp_server.py`). They are kept because the *habit* they
> illustrate is the point — say what is not wired, rather than implying it is.
> `validate_overseer_requirement` is the one that is still genuinely uncalled.

- `core.secrets.gate.run_disclosure_gate` and `core.secrets.leak_check
  .run_post_generation_check` (E2.5, E2.7) are complete, tested, callable functions with no
  live turn loop invoking them yet (that's a later task in a different track).
- E2.8's eval harness (scenarios, three-arm matrix, metrics, blind-judge payload
  construction) is real and tested against scripted providers; it has never produced real
  numbers against live providers, because this environment has no provider credentials to
  run it with. That's a data-collection gap, not a missing-code one — say so, don't fake it.
- E2.9's capability enforcement and E2.10's `validate_overseer_requirement` are complete
  and tested, with no live call site yet because the workspace-activation/behavior-profile
  UI flows they'd plug into don't exist yet either.
- E2.12's MCP tool handler and token model are complete and tested; the MCP transport
  server that will dispatch a real tool call into it is explicitly a later phase's task
  (the plan says as much — check for that kind of explicit statement before assuming a gap
  is yours to fill).

Every one of these got a scope-note blockquote at the top of its task file, naming the gap
and pointing at whichever future task closes it. Never leave this undocumented — a silent
gap reads as an oversight; a named one reads as a decision.

## 5. A recurring architectural pattern: INV-1 sibling modules

INV-1 restricts `core.secrets.repo`/`core.knowledge.repo` imports to
`core/assembler/`/`core/overseer/`. Something outside that allowlist regularly needs to
*write* secret- or knowledge-derived data (authoring a secret, recording a disclosure
decision, recording a reveal) without being handed the read-path import. The repeated
answer: a sibling module (`core.secrets.authoring`, `core.secrets.decisions`) that owns
just that write surface and is freely importable, leaving `repo.py` itself minimal and
INV-1-restricted. Reach for this pattern before considering an INV-1 exemption — there
should never be one.

## 6. End-of-track verification, before opening the PR

Run every one of these across the *whole* branch, not just the last task's own diff:

- `uv run pytest -q` — the complete backend suite, against a real database, not just the
  new tests.
- `uv run ruff format --check` / `ruff check` across the repo.
- `uv run mypy packages tests` (no `--strict` flag, whole tree) — **not** just
  `mypy --strict` on individual changed files. A single-file `--strict` run can miss
  cross-file issues (a test double whose method signature doesn't structurally match a
  generic `Protocol`, a function typed to take an invariant `list`/`dict` where a covariant
  `Sequence`/`Mapping` was actually needed) that only surface once mypy checks the whole
  call graph together. Track E's own PR caught exactly this class of bug this way, in files
  from two tasks earlier, after each had already passed its own per-file `--strict` check.
- If that whole-tree mypy run surfaces errors in files your track's commits never
  touched, check via `git log --oneline master..HEAD -- <file>` (or diff against `master`)
  whether the error pre-dates the branch. If it does, it is not yours to fix — leave it and
  say so in the PR body. If your track's own commits introduced or modified the file, fix
  it before opening the PR.
- Frontend, if touched: `pnpm typecheck`, `pnpm lint`, `pnpm build`, and `pnpm test`. If the
  track is the first to need a frontend test for one of its own named acceptance criteria
  and no test runner exists in `web/` yet, adding one (vitest + Testing Library, in Track
  E's case) is in scope for that task — note it in the scope note, don't treat it as a
  separate follow-up.
- Update the phase's `EXIT-GATE.md` honestly: check off what's genuinely demonstrated by a
  passing test in this branch, leave the rest open, and write *why* each open box is open
  (blocked on external credentials, blocked on a later phase's infrastructure, etc.) instead
  of leaving it a bare unchecked box with no explanation.

## 7. One PR, at the end, for the whole track

Title it for the phase and track (`Phase 2, Track E: Secrets, Behaviour, and the Disclosure
Gate (E2.1-E2.12)`). Body: one bullet per task with a one-line summary, a "scope notes"
section aggregating every deferred item from every task file (don't make a reviewer open
twelve task files to find them), the exit-gate status if the track closes or updates one,
and a test plan section listing the actual commands run in §6 with their results — not just
checkboxes with no evidence.

## 8. Before merging: wait for CI to actually finish

`gh pr view <n> --json mergeStateStatus,mergeable` can read `MERGEABLE`/`UNSTABLE` while a
check is still `IN_PROGRESS` — and a job can appear on the check list *after* the ones you
first saw all complete (Track E's `build` job started only after `test` finished). Poll
`gh pr view <n> --json statusCheckRollup` until every named check shows
`"status":"COMPLETED"`, not just until the ones you happened to see first go green, before
merging.

If the push itself is rejected with something like *"refusing to allow an OAuth App to
create or update workflow `.github/workflows/...`"*, the `gh`/git credential's token is
missing the `workflow` scope (needed whenever a branch touches a workflow file, e.g. adding
an eval-nightly job). This needs the human's action — ask them to run
`gh auth refresh -h github.com -s workflow` (suggest it via the `!` prefix so it runs in
their session), then retry the push. Don't try to work around it by stripping the workflow
file from the branch or force-pushing around the check.

## 9. No Docker in this environment? Standing up local Postgres + Redis

CLAUDE.md's stack assumes PostgreSQL 16 + Redis, normally via `docker compose` (see
`docker/compose.selfhost.yml`). If Docker isn't available in your sandbox, install Postgres
(with the pgvector extension) and Redis directly and create the two roles the migrations
and app expect:

- Migration/admin role: `pyrrhula` / `pyrrhula` (matches `core.config.Settings
  .database_url`'s own default, `postgresql+asyncpg://pyrrhula:pyrrhula@localhost:5432
  /pyrrhula`).
- App role (RLS-enforced): `pyrrhula_app`, password via `PYRRHULA_APP_DB_PASSWORD` — use
  `pyrrhula_app_ci` locally, matching what `.github/workflows/ci.yml` uses (not
  `pyrrhula_app_dev`, which is `packages/core/config.py`'s default (compose has none — it requires the value) and won't match
  a hand-created role unless you set it to that instead).

Then export, for every `pytest`/`uvicorn`/`alembic` invocation:

```
PYRRHULA_DATABASE_URL=postgresql+asyncpg://pyrrhula:pyrrhula@localhost:5432/pyrrhula
PYRRHULA_APP_DATABASE_URL=postgresql+asyncpg://pyrrhula_app:pyrrhula_app_ci@localhost:5432/pyrrhula
PYRRHULA_APP_DB_PASSWORD=pyrrhula_app_ci
PYRRHULA_REDIS_URL=redis://localhost:6379/0
```

Note that these do not persist between separate tool invocations in every agent harness —
if a command you run in a fresh shell can't see them, prefix the command with the exports
again rather than assuming a prior `export`/`.bashrc` edit took effect.
