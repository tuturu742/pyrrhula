# FAQ

The questions we expect, answered before the comment thread asks them.

## "Isn't this just prompt engineering?"

No, and this is the whole project. Prompt engineering *asks* a model to keep a secret;
Pyrrhula *removes the secret from the model's context*. A concealed fact cannot leak
because the model generating the turn does not have it — the agent acts on an
author-written directive instead ("deflect questions about the evening"), never on the
fact. The claim is testable: run the murder-mystery sample and the murderer survives a
police interview because the model playing her was never told she did it. The per-turn conceal/hint/reveal decisions are database records you can
read back, not vibes.

## What decides when a secret may surface?

Per workspace, one of three modes:

- **Excluded** (default): held secrets never enter context. Leak-proof and boring.
- **Trust**: each holder's *own* secrets enter its context with the directive; the
  model's judgement governs what it says. Zero extra calls; best drama on capable models.
- **Gate**: a small structured-output classifier rules per turn — conceal / hint /
  reveal — seeing only topical gists, never the secret text. Enforced by exclusion, with
  a post-generation leak check as defence in depth, and it fails closed.

In every mode, no agent ever receives *another* agent's secret. That line is structural
and no mode moves it.

## What does the gate cost per turn?

One small-model call per secret-holding speaker turn (the gate model is tenant-
configurable — a verified run used `gpt-4.1-mini`, fractions of a cent per turn). Turns
whose speaker holds no secrets skip it entirely. Trust and excluded modes make zero gate
calls.

## Why Postgres + Redis instead of `pip install`?

Because the guarantees live in the database. Row-level security is what makes tenant
isolation real; append-only hash-chained tables are what make the audit trail real;
`scope_key` pushed down as a SQL predicate is what makes visibility real. A library
embedded in your process can promise none of that — the moment the model's context is
assembled by code you can monkey-patch, "who-knows-what" is a convention again. The
stack is one `./install.sh compose` and runs on a laptop.

## Can it run fully local, without API keys?

Yes. Model access goes through one provider port (LiteLLM), so Ollama works everywhere a
hosted model does — generation, and with a capable local model, the gate too. Embedding
and rerank models (bge-m3 family by default) are self-hosted and swappable. A hybrid
deployment can pin which purposes (generation, gate, embed, …) may reach hosted
providers, per tenant, via the egress policy.

## Why AGPL?

The engine's value is easy to wrap in a closed SaaS, and AGPL is the licence that keeps
improvements flowing back when someone does. Packs and samples are MIT — content you
build from them carries no obligation. If AGPL genuinely blocks your use case, the CLA
keeps dual-licensing possible: open an issue and ask.

## How is this different from…

| | They have | They don't have |
|---|---|---|
| **LangGraph / AutoGen / CrewAI** | Real graph/team execution for developers | Any visibility model — every agent sees what you give the run; no tenancy, no audit, no notion of a secret |
| **SillyTavern** | A huge ecosystem for LLM roleplay | Structural anything: lorebooks are prompt blobs, secrets survive as long as the model feels like it, dice are prose, single-user |
| **AI Dungeon–style tools** | Polished consumer narrative | Multi-agent asymmetry, auditability, self-hosting, rules engines |

Pyrrhula is the intersection nobody built: orchestration *with* an enforced
information-asymmetry model, multi-tenant, auditable, self-hosted.

## Do the dice actually matter, or does the model narrate whatever it wants?

Rolls are seeded, executed in code, validated against the actor's own sheet (the engine
computes the modifier from stored fields via CEL — the model's claimed "+5" is checked,
not trusted), hash-chained, and rendered in the UI from the database record. A model can
*narrate* around a result; it cannot change one. The tabletop pack ships a faithful
Basic Fantasy RPG conversion whose stepped ability bonuses the engine applies itself.

## Is my [campaign / meeting / codebase] data used to train anything?

No. Self-hosted, your database, your model keys. The only things that leave your
deployment are the calls you configure to model providers — governed per-tenant by the
egress policy — and nothing else phones home.

## What state is the project in?

Approaching `0.1.0-rc`: the full stack described in the README is implemented, tested (isolation,
leak, replay, and architecture suites gate CI), and running across compose and k8s. An
ECS exec engine exists as an experimental, unverified adapter, not an install path. It is a single-maintainer project — expect honest response times and
a real roadmap rather than a growth team.
