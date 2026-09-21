# Update the three files that still reference the deleted planning artefacts

- title: Update the three files that still reference the deleted planning artefacts
- description: The planning artefacts were deleted in #10, but three files still point at
them, so the repository now instructs its readers to consult documents that do not
exist. Update all three; change nothing else.

1. CLAUDE.md, "Source-of-truth order" (around line 9). Entries 1, 2 and 4 name
   `pyrrhula-development-plan.md`, `tasks/<phase>/<task>.md` and
   `pyrrhula-research-brief.md`, all of which are gone. This list is what a coding agent
   reads first, so it must not send anyone to a missing file. What remains as authority
   is `docs/agent-guide.md` (conventions and definitions) and the hard rules in CLAUDE.md
   itself; rewrite the section to say that, keeping the surrounding tone.

2. README.md, the repository-map table (around line 188). Remove the two rows for
   `pyrrhula-research-brief.md` and `pyrrhula-development-plan.md`. Leave every other row
   untouched.

3. docs/agent-guide.md, first paragraph (line 3). It opens by saying it "condenses the
   authoritative development plan (`pyrrhula-development-plan.md`, ...)". That plan is no
   longer in the repository, so the sentence needs rewording: this guide IS the
   conventions reference now, not a condensation of something else.

Do not delete any file. Do not add files. Do not touch anything outside these three.

## Brief

<knowledge id="k1" class="lore" source="Why this project exists" entry="Business constraints">
Single maintainer: features that need staffing to operate are out of scope. Self-hosted first — no hosted service to sell, so the install must stay a one-liner. The engine is AGPL and the packs MIT, so anything that would force pack authors to open their content is a design error, not a licensing detail.
</knowledge>

<knowledge id="k2" class="lore" source="Why this project exists" entry="What users actually ask for">
In order of how often it comes up: 'the NPC blurted the twist' (the reason exclusion exists), 'the dice are made up', 'I can't tell why it said that', and 'I don't want my campaign on someone else's server'. Every one of those maps to a structural feature rather than a better prompt — that mapping is the product.
</knowledge>

<knowledge id="k3" class="lore" source="Why this project exists" entry="Who this is for">
Three audiences, in priority order. **Tabletop groups** who want a game master that can hold a secret and dice that cannot be talked out of a result. **Teams** running structured multi-agent working sessions where some facts are genuinely confidential. **Engineering orgs** delegating work to coding agents under review. The first pays the rent for the design; the other two prove the engine is domain-neutral.
</knowledge>

<knowledge id="k4" class="misc" source="Project reference shelf" entry="Decisions worth remembering">
Postgres-only was chosen over a vector database because the isolation guarantees live in RLS and a second store would need its own. CEL was chosen over any embedded scripting because user-authored code is a security stance we do not want to defend. Both decisions get re-proposed roughly twice a year; neither has changed.
</knowledge>

<knowledge id="k5" class="misc" source="Project reference shelf" entry="Team glossary">
**Overlay** — the per-workspace relabelling of core nouns. **Pack** — declarative workflow content (schemas, flo

## Status

Scaffolded by the delegated coding agent. TODO: implement.
