# Research & Development Brief: Pyrrhula — a Multi-Tenant, Multi-Agent Orchestration Platform (Tabletop-RPG-First)

## 1. Purpose of this document

This is a brief for a deep-research/planning agent. The goal is to investigate the design
space, survey prior art, and produce a concrete technical development plan (architecture,
stack choices, phased roadmap, risks) for a new platform — working name **Pyrrhula**.

Pyrrhula is a self-hosted or cloud-deployable, **multi-tenant from day one** platform for
running structured multi-agent conversations, where knowledge, process/rules, and
participant state are treated as versioned, structured data rather than prompt text — and
where the conversation itself is driven by an explicit, configurable phase/turn engine
rather than free-form chat. The first and primary target use case is AI-managed tabletop
RPG campaigns; the core engine is designed generically enough that a second target use case
— structured enterprise multi-agent workflows (e.g., multi-perspective strategy discussions,
sequential review/approval loops) — is a first-class citizen of the architecture, not an
afterthought.

The tool is inspired by SillyTavern and similar AI-roleplay chat tools but is designed to fix
specific structural limitations described below. Deliverable of this research: a written
plan covering architecture, comparable/competing tools, technology choices, and a phased
build roadmap.

## 2. Naming and vocabulary conventions

Because a second design goal is enterprise-plausibility and genericity, **the core platform
uses domain-neutral vocabulary**. Domain-specific vocabulary (tabletop RPG terms) is applied
as a "vocabulary overlay" / labeling layer on top of the same underlying objects, so the same
data model serves both the RPG use case and non-gaming use cases without renaming or
restructuring anything at the schema level.

Core (domain-neutral) term → RPG-overlay term:

- **Workspace** → World / Campaign
- **Process Definition** (the phase/turn state machine) → Session Flow / Turn Structure
- **Facilitator Agent** (the agent with elevated authority/visibility over a workspace) → Arbiter (renamed from "Game Master"/"GM")
- **Participant Agent** → Player Character bot / NPC bot
- **Human Participant** → Player
- **Knowledge Source** (a versioned document collection with a type and priority weight) → Rulebook / Lorebook / Miscellany
- **Entity** (a structured, schema-defined record with fields and a state machine) → Character / NPC
- **Entity Schema** (user-defined field/state definitions for a workspace) → Character Sheet Template
- **Deterministic Tool** (a code-executed, non-LLM-generated function callable by agents) → Dice Roller / Coin Flip / Stat Calculator
- **Deliberation Phase** vs. **Action Phase** (process states) → Discussion Phase / Turn Phase

Research on naming should treat this vocabulary mapping as a **configuration layer**
(effectively a set of label overrides plus an optional bundled plugin, see Section 3.7),
not a hardcoded part of the schema. This is what allows the same engine to be repackaged for
a non-gaming enterprise deployment (e.g., "Facilitator" instead of "Arbiter," "Policy
Document" instead of "Rulebook") without touching the data model.

## 3. Functional requirements

### 3.1 Content model (Knowledge Sources)
1. Authoring and management of Knowledge Sources typed as rules, lore, or misc (naming
   overlay: rulebook/lorebook/miscellany), as distinct content types with independent
   structure and metadata — not just prompt blobs.
2. Ability to feed these into agent context via structured retrieval (true RAG/knowledge-base
   ingestion per content type), not just prepended prompt text.
3. Configurable retrieval priority so mechanical rules can outrank lore (and vice versa,
   configurable per workspace) when both are relevant to a query.
4. Any given Knowledge Source can be attached to multiple independent Workspaces, enabling
   reuse across campaigns/teams.
5. Versioning of Knowledge Sources (edit history, diffing, and the ability to fork a
   Workspace from a specific content version) so rules/policies can be iterated on without
   breaking existing active sessions.
6. Rule citation/explainability: when an agent makes a ruling or decision, it should be able
   to reference which specific Knowledge Source entry justified it, for debuggability and
   participant trust.

### 3.2 Agents and orchestration
7. Multiple agents per Workspace, each mapped to a distinct model/provider profile (Ollama,
   OpenAI, Gemini, Anthropic, others), so the platform orchestrates but never hosts models
   itself.
8. Declarative distinction between agents that passively provide information/answers
   (informational role) and agents that must actively adopt a persona and produce
   role-consistent behavior/feedback (participant role) — configurable per agent, per
   Workspace.
9. An explicit, user-configurable **Process Definition** (phase/turn engine) — e.g.,
   Facilitator turn → open discussion phase → participant action/turn phase →
   feedback/resolution loop — supporting branching, repeatable loops (such as "request
   feedback after each encounter"), and phase-specific rules about which agents/humans may
   act, in what order, and what data is visible to whom during that phase.
10. Ability for a human to reply "in place of" any agent, either verbatim or with an optional
    AI rewrite pass that conforms the reply to that agent's established persona/voice.
11. Deterministic mechanical resolution: arbitrary randomized resolution (dice rolls, coin
    tosses, or other configurable random mechanics) executed as real code/tool calls — never
    LLM-generated text — with results defined by and validated against the active
    Workspace's rule system, and logged immutably for auditability.
12. Agents should be able to invoke MCP servers (both external/third-party and ones defined
    within the platform) and, where permitted per Workspace policy, perform live internet
    search.
13. Support for asymmetric knowledge/visibility as a first-class concept of the Process
    Definition and Knowledge Source model — e.g., a Facilitator/Arbiter agent or human may
    know information that Participant-facing agents and other humans must not see; retrieval
    and context assembly must respect per-role, per-phase visibility scoping.
14. Beyond workspace-level asymmetric knowledge (item 13), **individual agents (and human
    participants) can hold their own private secrets** — information known only to that
    specific agent/participant and not automatically visible to other agents, other
    participants, or even the Facilitator by default. This models things like a hidden
    personal motive, a secret allegiance, or private backstory information a given
    character possesses that others in the scene do not.
15. A **human overseer retrieval mechanism**: regardless of in-fiction visibility rules, a
    designated human overseer role must always be able to query and view the full set of
    secrets held by any agent/participant in a Workspace, for moderation, debugging, and
    "director's view" purposes. This overseer visibility must be logged/auditable (so it's
    clear when and by whom a secret was inspected) but must never be blocked by the
    in-fiction visibility rules that apply to other agents/participants.
16. **Agent behavioral configuration beyond prompts**: each agent should be configurable via
    a distinct set of structured parameters/sliders — not just natural-language prompt
    text — governing behavioral tendencies such as: propensity to voluntarily reveal secrets
    they hold; propensity to use secret information for their own advantage (e.g., manipulate
    others, lie, withhold); general disposition/malice level; cooperativeness; risk-aversion;
    and similar behavioral axes relevant to roleplay and negotiation-style interactions. These
    parameters should be a first-class part of the agent's configuration object (so they can
    be inspected, audited, versioned, and adjusted independently of the agent's persona
    prompt), and should be designed generically enough to also apply to non-RPG agents in the
    enterprise use case (e.g., an agent's tendency to escalate disagreements, how readily it
    concedes a position, how much it discloses uncertainty).

### 3.3 Entity/state model (generic, with RPG content as a plugin)
17. A **generic Entity Schema and State Machine Framework** in the core engine: any Workspace
    can define arbitrary typed fields (numeric attributes, categorical states, free text) and
    arbitrary state machines (e.g., health/status transitions, progression/leveling curves)
    attached to an Entity type, without the core engine hardcoding what those fields mean.
18. RPG-specific concepts — attributes, skills, classes/races, spells, health/status state
    machines, XP/progression curves — are **not hardcoded into the core engine**. They are
    delivered as a first-party "RPG Ruleset" content/plugin pack built on top of the generic
    Entity Schema and State Machine Framework, so the same framework can define, e.g., a
    project's workflow states or a support ticket's status lifecycle in a non-gaming
    deployment.
19. Graphical/visual representation of Entity state (sheets, health bars, status trackers,
    progression charts) generated automatically from the active Entity Schema, regardless of
    domain.

### 3.4 Sessions and continuity
20. Ability to resume old conversations, with history repopulation into working context.
21. Workspace state that persists and evolves between active conversations (not purely
    conversational — e.g., time passing, entity state changes outside of active chat),
    supporting asynchronous/play-by-post-style pacing as well as synchronous real-time use.

### 3.5 Editing, import, and export
22. AI-assisted chat-based editing of Knowledge Sources, Entity Schemas, and agent
    persona/character descriptions, with changes applied back to the versioned content model.
23. **Export and import of Knowledge Sources**, in a portable format, so rulebooks/lorebooks/
    miscellany can be backed up, moved between deployments, or shared between tenants/users
    (subject to permissions), and re-imported with version history intact where possible.
24. **Export and import of chat/session history** (full conversation transcripts, including
    Process Definition phase markers and any deterministic-tool results such as dice rolls),
    so a session can be archived, migrated between deployments, or handed off, and later
    re-imported to resume or reference.
25. **Generated reports based on session history**: the ability to produce a human-readable
    summary/report derived from a session's history (e.g., a campaign recap, a session
    log formatted for players, or — in the enterprise use case — a summary of a multi-agent
    discussion's key points and decisions), exportable in shareable formats. This is distinct
    from raw transcript export: it is an AI-assisted synthesis of the history into a
    readable narrative or structured summary document.
26. Export of Knowledge Sources, chat history, and generated reports into human-readable,
    shareable formats (e.g., Markdown/PDF/EPUB) suitable for distributing outside the
    platform.

### 3.6 Platform and multi-tenancy (foundational, not deferred)
27. Clean, modern web UI; container-based (Docker) deployment, single-tenant self-host and
    multi-tenant SaaS-style deployment both supported by the same codebase.
28. Provider-agnostic model integration (Ollama for local models, plus OpenAI, Gemini,
    Anthropic, and others via API), with per-agent model+parameter profiles.
29. **Multi-tenancy is a foundational architectural requirement, not a later-phase add-on.**
    The data model, auth model, and API must support organizations/tenants containing
    multiple Workspaces, with role-based access control (owner/admin/editor/participant/
    viewer, at minimum) scoped per tenant and per Workspace, from the very first schema
    design. Retrofitting multi-tenancy after a single-tenant MVP is explicitly out of scope
    as an approach — research should assume tenant isolation is present in the MVP itself,
    even if the MVP UI only exposes a single tenant's worth of functionality initially.
30. Usage/cost tracking per model, agent, Workspace, and tenant (token counts, estimated
    spend), given that tenants bring their own API keys/credentials across multiple
    providers.
31. A safety/content-moderation layer appropriate for both public multi-user tabletop use and
    enterprise deployment, configurable per tenant.

### 3.7 Vocabulary/theming layer
32. A configuration layer that maps core domain-neutral terms (Workspace, Facilitator,
    Participant Agent, Knowledge Source, Deterministic Tool, Deliberation/Action Phase) to
    domain-specific display labels (World, Arbiter, Player Character, Rulebook/Lorebook, Dice
    Roller, Discussion/Turn Phase), selectable per tenant or per Workspace, so the same
    engine can be presented as an RPG tool or as a generic enterprise agent-orchestration
    tool without code changes.

## 4. Secondary use case: general enterprise multi-agent orchestration

The Process Definition engine, role/phase-scoped knowledge retrieval, generic Entity Schema
framework, and human-in-the-loop override capabilities above are designed to be directly
reusable for structured multi-agent business workflows — for example, multiple agents (or
agents + humans) working through a defined sequence such as brainstorm → critique → revise →
decide, or multiple domain-expert agents debating a marketing strategy or system architecture
proposal, with a human able to steer, override, or approve outputs at defined checkpoints.
Please assess, concretely:

- How much of the architecture in Section 3 is directly reusable as-is via the vocabulary
  overlay (Section 3.7) versus what would require actual schema/logic changes.
- Whether the "Deterministic Tool" concept (dice/coin resolution) generalizes cleanly to
  enterprise equivalents (e.g., a deterministic calculation, a policy-lookup tool, a
  approval-routing tool) or needs a distinct abstraction.
- What enterprise-specific requirements (SSO/SAML, audit log retention policy, data residency,
  fine-grained RBAC beyond the five roles listed above) should be designed in now even if not
  built in the MVP.

## 5. Research questions to answer

1. **Prior art survey**: Beyond SillyTavern/Agnaistic/RisuAI/TavernAI/Chub, what other tools
   (open-source or commercial) address any subset of these requirements — including general
   multi-agent orchestration frameworks (e.g., LangGraph, AutoGen, CrewAI, Microsoft Semantic
   Kernel's agent framework) and any RPG-specific AI GM tools? What should be borrowed, and
   what should be deliberately avoided? Also check for any existing product literally named
   "Pyrrhula" in the AI-agent or tabletop-gaming space specifically (a general namesearch has
   already been done and found only unrelated-industry uses — an ERP company and an AI
   training-data company — but a closer check within this specific niche is worth doing
   before committing).
2. **Multi-tenant architecture**: Propose a system architecture covering: content/knowledge
   model (Knowledge Sources, versioning, priority-weighted RAG, per-tenant isolation), the
   Process Definition/phase engine (is a state-machine/graph library like LangGraph
   appropriate, or should this be custom-built?), deterministic tool execution for dice/
   coin/stat math, role- and phase-scoped context assembly for asymmetric knowledge across
   tenants and Workspaces, model-provider abstraction layer, persistence layer for
   long-running/async sessions, and tenant/org/RBAC data model.
3. **Data model**: Propose schemas for: tenants and roles; Knowledge Sources (rules/lore/misc)
   with versioning and tenant/workspace attachment; Workspaces and their attached Knowledge
   Sources; Agents with model-provider profiles and role type (informational vs.
   participant); Entities with workspace-defined Entity Schemas; session/conversation history
   with Process Definition state; the vocabulary-overlay/theming configuration.
4. **Tech stack recommendation**: Backend framework, database(s) (including vector store
   choice for priority-weighted/scoped RAG with tenant isolation), frontend framework,
   real-time/streaming approach for chat, Docker/deployment packaging for both self-host and
   multi-tenant SaaS modes, and MCP integration approach (client and/or server-hosting within
   the platform).
5. **Deterministic mechanics design**: How should the dice/coin/stat resolution system be
   exposed to agents — as MCP tools, as internal function-calling tools, or both — and how
   should results be validated/logged so they can't be silently overridden by model
   hallucination? How should this same abstraction generalize to non-gaming deterministic
   tools per Section 4?
6. **Rule-vs-lore priority mechanism**: Concrete approaches for weighting/ranking retrieval
   results so mechanical rules can be configured to outrank lore (and how to make this
   configurable per Workspace rather than hardcoded).
7. **Generic Entity Schema framework design**: How to design a state-machine and typed-field
   framework generic enough to express arbitrary RPG systems (D&D-like, PbtA-like, a
   custom coin-flip system) as well as non-gaming entities (e.g., a support ticket lifecycle,
   a project status) without the core engine encoding domain assumptions, while still making
   the first-party RPG plugin pack feel native and low-friction to use.
8. **Phased build roadmap**: A pragmatic MVP-to-v1 roadmap that treats multi-tenancy as
   present from the first working version. What is the smallest useful slice that proves the
   core "Process Definition + priority RAG + deterministic dice + tenant isolation" loop
   end-to-end, and what are the subsequent milestones toward a fully enterprise-capable
   version (SSO, advanced RBAC, audit/compliance features, the generalized non-gaming
   vocabulary overlay)?
9. **Secrets and overseer visibility design**: How to model per-agent/per-participant private
   secrets distinctly from workspace-level asymmetric knowledge (item 13 vs. items 14-15) —
   as a property of the Entity, the Agent, or a separate "Secret" record type with its own
   visibility rules? How should the human-overseer retrieval mechanism be exposed (a
   dedicated overseer UI/API endpoint, a query tool, both) while keeping an auditable log of
   overseer access that is itself protected from being tampered with?
10. **Behavioral parameter framework**: How to design the structured, non-prompt behavioral
    configuration described in item 16 (propensity to share/exploit secrets, malice,
    cooperativeness, risk-aversion, etc.) so that it (a) is model-agnostic (works whether the
    underlying provider is Ollama, OpenAI, Gemini, Anthropic, etc.), (b) reliably influences
    agent behavior rather than being a cosmetic label, and (c) is auditable/versioned like
    other agent configuration. Consider whether this is best implemented as structured
    additions injected into the system prompt in a standardized way, as parameters that
    adjust retrieval/decision-weighting logic outside the prompt, or a hybrid — and how to
    evaluate/tune reliability of each approach empirically. Also consider how these same
    axes generalize to the enterprise use case (e.g., an agent's tendency to escalate,
    concede, or disclose uncertainty in a business discussion).

    Existing prior art precedent: SillyTavern and similar tools already expose simple
    behavioral sliders for things like a character's talkativeness (e.g., how much they
    volunteer unprompted dialogue). This shows the basic pattern — a slider value folded
    into the system prompt as a natural-language instruction — is achievable and already
    used in production today. The open question for Pyrrhula is less "is a slider-driven
    behavioral parameter possible" and more "does that same prompt-injection approach hold
    up for higher-stakes axes like secret-keeping and malice, where a failure is not a
    slightly-off tone but an information leak or an inconsistent character."

    Specifically, research should evaluate whether simple prompt-injected sliders (the
    SillyTavern-style approach) are sufficient for low-stakes axes (talkativeness,
    verbosity, formality) but insufficient for high-stakes axes (secret disclosure, malice,
    deception), and if so, whether a **separate hidden reasoning/deliberation pass** is
    warranted for the latter — i.e., before an agent's user-facing reply is generated, a
    non-visible intermediate step evaluates "given this agent's secrets and behavioral
    parameters, what does this agent choose to reveal or conceal in this specific
    response" as a discrete decision, with the visible reply then constrained to be
    consistent with that decision. This adds latency and cost per turn, so research should
    weigh: reliability gain vs. added latency/cost, whether it's needed for all
    participant agents or only those holding active secrets, and whether it can be scoped
    to only fire on phases/moments where secret-relevant decisions are actually plausible
    (e.g., skip it during purely mechanical dice-resolution phases) rather than running on
    every single turn.
11. **Import/export and reporting pipeline**: Design for portable import/export formats for
    Knowledge Sources, chat/session history (including phase markers and deterministic-tool
    results), and AI-generated reports/summaries derived from session history. Consider
    format choice(s) (e.g., a structured JSON/YAML bundle for full-fidelity re-import vs.
    Markdown/PDF/EPUB for human-readable sharing), versioning compatibility across
    Pyrrhula releases, and how tenant/permission boundaries apply to import/export
    operations (e.g., preventing a report export from leaking another participant's
    private secrets).
12. **Risks and open questions**: Notably around context-window cost/latency at scale with
    multiple simultaneous agents and tenants, keeping deterministic tool results trustworthy,
    the added complexity cost of building multi-tenant and schema-generic from day one versus
    the cost of retrofitting later, managing the complexity of a fully generic entity/schema
    system versus shipping opinionated RPG defaults first via the plugin pack, and the risk
    of behavioral-parameter drift or unreliability across different model providers.

## 6. Constraints

- Must not host or fine-tune models itself; integrates with external providers via API only.
- Must be deployable via Docker/containers for both individual self-hosting and multi-tenant
  hosted deployment.
- Must be multi-tenant-capable from the initial architecture, not retrofitted.
- RPG-specific game mechanics (attributes, skills, classes, spells, health-state machines,
  etc.) must be implemented as content/plugin packs on top of a generic Entity Schema and
  State Machine Framework, not hardcoded into the core engine.

## 7. Deliverable expected from this research

The primary deliverable is a **complete, actionable development plan**, not just an
analysis or set of recommendations. It must include all of the following, at minimum:

1. **Prior-art comparison table** — tools surveyed, what each does well/poorly relative to
   Section 2-4's requirements, and what to borrow vs. avoid.
2. **Proposed multi-tenant architecture** — diagram and description covering every component
   named in Section 5's research questions (content model, Process Definition engine,
   deterministic tools, entity/schema framework, secrets/overseer mechanism, behavioral
   parameter system, import/export/reporting pipeline, model-provider abstraction,
   persistence layer, tenant/RBAC layer).
3. **Data model** — concrete schema sketch (entities, relationships, key fields) for every
   object type named in Section 5, question 3, plus the newly added Secret record type and
   behavioral-parameter structure.
4. **Technology stack recommendation** with justification (backend, frontend, database(s),
   vector store, real-time/streaming layer, container/deployment approach, MCP integration
   approach).
5. **A full phased build roadmap**, broken down into concrete phases/milestones (e.g., MVP →
   v1 → enterprise-hardened, or whatever phase structure the research concludes is
   appropriate), and for **each phase**, a task and sub-task breakdown specific enough to be
   handed to an engineering team or used to scope sprints — i.e., not just "build the
   knowledge base system" but the constituent sub-tasks (schema design, ingestion pipeline,
   priority-weighting logic, per-tenant isolation, UI for authoring, versioning/diffing,
   import/export, etc.) with rough sequencing/dependencies noted between tasks and across
   phases.
6. **Risks and open questions** section covering at least the items listed in Section 5,
   question 12, plus any additional risks identified during research.
7. Any additional sub-structure the research agent deems necessary to make the plan genuinely
   actionable (e.g., a glossary reconciling the domain-neutral and RPG-overlay vocabularies
   from Section 2, or a suggested repo/module layout) should be included as supporting
   material rather than omitted for brevity — completeness is preferred over conciseness for
   this deliverable.
