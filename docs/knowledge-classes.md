# Knowledge classes: rules, lore, misc

Every knowledge source carries a class — `rules`, `lore`, or `misc`. The class is not a
folder label: it is what the **budget allocator** keys on. A flow's phases declare which
classes are visible and how the context token budget splits between them
(`"ratio": {"rules": 0.75, "lore": 0.25}`), so the class you file a source under decides
*when* and *how much* of it reaches a model's context. File a rulebook as the wrong class
and it will politely never be retrieved.

The engine's three class keys are fixed vocabulary; what they *mean* is workflow content,
relabeled per workspace by the vocabulary overlay. The intent behind each:

| Class | What belongs in it | When flows typically weight it |
|---|---|---|
| `rules` | How things are **done** here — procedures, constraints, mechanics. Normative text the table must follow. | Action/resolution phases; checks and rulings. |
| `lore` | What is **true** here — the world, the history, the domain, the people. Shared background everyone at the table is entitled to know. | Framing and discussion phases; narration. |
| `misc` | What gives the place **texture** — trivia, sayings, in-universe references, glossaries. True but never load-bearing. | A thin slice of discussion phases; flavor. |

A useful test when filing a source: *if the model contradicts this text, is that an error
(rules), a retcon (lore), or just a missed flourish (misc)?*

## Per workflow

### Tabletop RPG (`rpg` — labels: Rulebook / Lorebook / Miscellany)

- **rules** — the game system: how dice are rolled, how characters are created, what an
  attack or saving throw is, class/race mechanics. The shipped example is the Basic
  Fantasy rules carried inside the karsh-vale sample bundle.
- **lore** — the world and its common knowledge: the region's history, places and roads,
  who rules, what everyone in the tavern already knows. This is what grounds the GM's
  narration and keeps five agents describing the *same* village.
- **misc** — random trivia, famous jokes, in-universe references: drinking songs,
  superstitions, proverbs, what the innkeeper always says. A small budget slice, but it
  is where a table stops sounding generic.

### Default (`default` — labels: Policy Document / Domain Context / Reference Material)

- **rules** — policies and constraints the discussion must respect: brand voice rules,
  confidentiality policy, approval thresholds, "we never promise dates in public copy."
- **lore** — the business itself: who the company is, the product, the customers, the
  market situation, what this working session is for.
- **misc** — reference material: glossaries, past-campaign trivia, house anecdotes.

### Software development (`swdev` — labels: Engineering Standards / Business Context / Reference)

- **rules** — how code is written here: style guides, required and forbidden libraries,
  review criteria, architectural invariants, CI expectations. If a reviewer would block a
  PR over it, it is `rules`.
- **lore** — the **non-technical** side: what the product is for, who the customers are,
  business constraints and priorities, why this backlog exists. Engineer agents read the
  code for technical truth; `lore` is where they learn what the code is *for*.
- **misc** — reference: team glossary, historical decisions worth remembering but not
  enforcing, links-shaped trivia.

## Practical notes

- Class keys are `rules` / `lore` / `misc` at the API and in `.pyr` bundles; overlays only
  change the *displayed* name. The class column is a free string in the schema, but a
  class no flow budgets is a class no retrieval will ever spend tokens on — stick to the
  three unless your own flow declares more.
- Budgets are per phase: the stock RPG flow gives framing turns `lore 0.6 / rules 0.3 /
  misc 0.1`, discussion `lore 0.8 / misc 0.2`, and action/resolution turns
  `rules 0.75 / lore 0.25`. Author flows with the same intent: rules where correctness
  matters, lore where narration does, misc as seasoning.
- **A phase's `max_tokens` is a floor, not the whole answer.** A flow is portable and the
  model is chosen later, so a frontier model would otherwise be handed the same budget as
  a local one. Where the platform recognises the model, knowledge gets a capped share of
  its context window instead, never less than the flow asked for. Where it does not — an
  OpenAI-compatible endpoint with an unfamiliar model name, say — the flow's number stands.
  Set `knowledge_token_budget` on the connection to decide it yourself.
- **Always-on (`constant`) entries take at most a share of a class's slice**, so attaching
  a handbook beside them is not pointless. Room search does not spend comes back to them,
  and they are offered in the author's `insertion_order`. A workspace's **Knowledge
  budget** card shows, per phase and class, what each gets and which always-on entries
  fit — worth a look after importing anything large.
- **A phase can name its own share.** `budget.constant_share` (a fraction above 0 and at
  most 1) overrides the platform's default for that phase alone. Phases differ in kind: a
  briefing whose whole job is to put one fixed text in front of everyone can ask for most
  of its slice, while a resolution phase wants room to look things up. Omit it and the
  phase inherits the default, which is what every already-authored flow does.
- **A `rules` entry with no activation keys gets them from its own title when published**,
  so an ingested handbook answers to what a turn *names* rather than only to what it
  resembles. The entry editor says when keys were derived; edit or clear them freely.
- **Lore and misc entries are not keyed for you, and should be keyed by hand.** A reference
  work's section titles *are* the names of the things they govern ("Goblin", "Saving
  Throws"), so deriving from them works. A setting's entry titles are editorial labels
  ("What Brought You Here", "Marta Fenn Sets Two Places") that no turn ever says aloud.
  Measured on a real session: hand-written lore keys fired 27 times in 16 turns where
  title-derived ones fired 5. Key a lore entry with the words a scene would use — the
  place, the person, the rumour, the thing.
- The [sample workspaces](https://github.com/tuturu742/pyrrhula-samples) that carry
  knowledge ship the classes their flow budgets, so an imported sample demonstrates the split
  end to end.

## Levels of lore (who-knows-what, applied to knowledge)

A class decides *what kind* of knowledge a source is. A **scope** decides *who* may read
it. Combine them and lore stops being uniform: not every character knows the same history.

A scope is a named membership set on the workspace (`public`, `role`, `group`, or a
per-principal `private`), and
every knowledge entry carries a `scope_key`. An entry reaches a persona's context only if
that persona is entitled to its scope — resolved as a SQL predicate at retrieval time, the
same machinery that gates secrets. Two rules compose:

- The **phase** decides which scopes are *in play* at all (`visibility.scopes`).
- **Membership** decides which persona, within those, actually receives each entry.

So you build as many tiers as the world needs:

| Scope | Members | Example (karsh-vale) |
|---|---|---|
| `workspace_public` | everyone | the Vale, the ruin, the disappearances, the tavern rumour |
| `guild_lore` (group) | the referee, Bram, Linnea | how to read masons' marks; a binding inscription against a decorative one |
| `referee_lore` (group) | the referee alone | what the Hollow Crown actually is |
| a secret (one holder + the gate) | one persona | what this specific NPC is hiding tonight |

**In the karsh-vale sample** (the `.pyr` bundle in the samples repository) this is wired
live. The common lorebook — *Karsh Vale, what everyone knows* — is `workspace_public`. A
second source, *Guild knowledge: masons' marks and ward-cant*, is filed under a
`guild_lore` group scope whose members are the referee, **Bram** (a stonemason's son) and
**Linnea** (college-trained); Pip the halfling thief is not a member, so that lore never
enters his context. A third, *The truth of the Hollow Crown*, sits in `referee_lore`,
which only the referee holds. Nobody is told to "act ignorant"; the lore is simply absent
from Pip's turn. The bundle ships its own flow, whose four phases all declare the three
bands, and the bundle carries the memberships.

**The software-development equivalent** is identical: file customer requirements and
business priorities under a `business_context` group scope, put the lead and the PM in it,
and leave a junior engineer out. The junior's implement turns get the engineering
standards (`rules`) and none of the business lore — which is both realistic and what you
want, since an `implement` phase budgets `rules` only in the first place. Two independent
filters point the same way: the phase excludes the *class*, and the scope would exclude
the *persona* even where the class is in play.

To author a tier: create a `group` scope (`kind: "group"`, `members.principal_ids: [...]`),
file the restricted entries under its key, and declare the scope in the phases where it
should be readable. Nothing else changes — retrieval does the rest.
