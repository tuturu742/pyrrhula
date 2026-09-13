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
  attack or saving throw is, class/race mechanics. The shipped example is the
  [Basic Fantasy table reference](../scripts/handbooks/basic_fantasy_rules.md).
- **lore** — the world and its common knowledge: the region's history, places and roads,
  who rules, what everyone in the tavern already knows. This is what grounds the GM's
  narration and keeps five agents describing the *same* village.
- **misc** — random trivia, famous jokes, in-universe references: drinking songs,
  superstitions, proverbs, what the innkeeper always says. A small budget slice, but it
  is where a table stops sounding generic.

### General / enterprise (`default` — labels: Policy Document / Domain Context / Reference Material)

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
- The four [sample workspaces](https://github.com/tuturu742/pyrrhula-samples) each ship
  all the classes their flow budgets, so an imported sample demonstrates the split
  end to end.

## Levels of lore (who-knows-what, applied to knowledge)

A class decides *what kind* of knowledge a source is. A **scope** decides *who* may read
it. Combine them and lore stops being uniform: not every character knows the same history.

A scope is a named membership set on the workspace (`public`, `role`, or `group`), and
every knowledge entry carries a `scope_key`. An entry reaches a persona's context only if
that persona is entitled to its scope — resolved as a SQL predicate at retrieval time, the
same machinery that gates secrets. Two rules compose:

- The **phase** decides which scopes are *in play* at all (`visibility.scopes`).
- **Membership** decides which persona, within those, actually receives each entry.

So you build as many tiers as the world needs:

| Scope | Members | Example (Gallowfen) |
|---|---|---|
| `workspace_public` | everyone | the guild's tower has blue lights |
| `scholarly_lore` (group) | the GM, the wizard, a cleric with an archive | who bound the thing under the chapel five centuries ago |
| a secret (one holder + the gate) | one persona | what this specific NPC is hiding tonight |

**In the Basic Fantasy sample** (`scripts/basic_fantasy_game.py`) this is wired live. The
common lorebook (Thornwick, the Gallowfen, the bell) is `workspace_public`. A second
lore source — *The Sundering and the Bell*: Archmagister Vaelith Corr, the mages' circle
struck from the rolls, the bell's tolls as a failing ward — is filed under a
`scholarly_lore` group scope whose only members are the GM and **Linnea, the elf
magic-user**. Bram the dwarf fighter and Pip the halfling thief are not members, so that
history never enters their context. Nobody is told to "act ignorant"; the deep lore is
simply absent from the barbarian-shaped character's turn. The stock RPG flow's discussion
and framing phases declare `scholarly_lore` in their scopes, so the band is in play; the
sample seeds the membership.

**The software-development equivalent** is identical: file customer requirements and
business priorities under a `business_context` group scope, put the lead and the PM in it,
and leave a junior engineer out. The junior's implement turns get the engineering
standards (`rules`) and none of the business lore — which is both realistic and what you
want, since a `build` phase budgets `rules` only in the first place. Two independent
filters point the same way: the phase excludes the *class*, and the scope would exclude
the *persona* even where the class is in play.

To author a tier: create a `group` scope (`kind: "group"`, `members.principal_ids: [...]`),
file the restricted entries under its key, and declare the scope in the phases where it
should be readable. Nothing else changes — retrieval does the rest.
