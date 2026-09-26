# Contributing to Pyrrhula

Thanks for wanting to help. This is a short guide to the two things that are genuinely
non-negotiable here, and then the practical stuff.

## 1. The CLA — agreed by opening a PR

**By opening a pull request you agree to the
[Contributor License Agreement](CLA.md).** Read it first — it is short, adapted from the
Apache Software Foundation's ICLA, and most of it will already be familiar. You keep the
copyright in everything you write; the agreement grants a licence, it is not an
assignment. There is nothing to sign and no account to create.

## 2. Read the invariants before touching the core

`docs/agent-guide.md` lists them, each with a CI test that enforces it. The ones that
bite hardest:

- **Vocabulary.** Nothing under `packages/core/` may contain a domain word — no
  `campaign`, `dice`, `sprint`, `pull_request`. Core is domain-neutral and emits
  `label_key`s; domain words live in workflow packs and vocabulary overlays.
- **Tenancy.** Every tenant-scoped table carries `tenant_id` with RLS `FORCE`, and
  database sessions open only through `tenant_scope()`. A new such table must gain an
  isolation negative test in the same pull request.
- **Secrets.** Concealed plaintext is *absent* from the generation context — exclusion,
  not instruction. The disclosure gate sees gists only and fails closed.
- **No user-authored code.** User logic is JSON Schema, declarative FSMs and CEL. No
  `eval`, no sandboxed Python, no exceptions.

If an invariant seems to be blocking something genuinely needed, say so in the issue. The
answer may be that the core is missing an abstraction. It is never to work around the
lint.

## 3. How to work

One task, one branch, one pull request. Say in the PR what the change is for and how you
know it works.

```bash
uv sync --extra dev
uv run pytest                 # the whole suite
uv run ruff check . && uv run ruff format --check .
uv run mypy packages
```

Say what would prove the change works, and make it falsifiable. If a claim cannot be
tested as written, say so in the pull request rather than quietly reinterpreting it.

### Tests you are expected to extend

These suites gate merges and exist to catch a specific class of mistake. Extend them when
your change touches what they guard:

| Suite | Guards |
|---|---|
| `tests/isolation/` | cross-tenant leakage; RLS actually filtering |
| `tests/architecture/` | the import graph (INV-1) |
| `tests/leak/` | secret plaintext never reaching a context or an export |
| `tests/replay/` | a context manifest replaying from its own record |
| `tests/packs/` | packs staying content, not code |

Several of them **skip themselves** when Postgres or Redis is unreachable, so a green run
locally may mean nothing ran. Start both before you trust a pass:

```bash
docker run -d -p 5432:5432 -e POSTGRES_USER=pyrrhula -e POSTGRES_PASSWORD=pyrrhula \
  -e POSTGRES_DB=pyrrhula pgvector/pgvector:pg16
docker run -d -p 6379:6379 redis:7
uv run alembic upgrade head
```

`tests/packs` and `tests/isolation` also need the workflow packs, fetched from the
repository pinned in `deploy/plugins.json`:

```bash
python scripts/fetch_plugins.py
```

While that repository is private, the fetch needs credentials — either an SSH remote you
can already read, or a token in `PYRRHULA_PLUGINS_TOKEN`. Without them those two suites
stop at collection and say so; the rest of the suite is unaffected.

## 4. Commit messages

Say what changed and **why it is right**, not what the diff already shows. The reasoning
is what a reader needs in six months when they are deciding whether they may change it
back. If a decision has a trade-off, name the trade-off.

## 5. Reporting a security issue

Do not open a public issue. See [SECURITY.md](SECURITY.md).
