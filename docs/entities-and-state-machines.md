# Entities and state machines

An **entity** is a record the system keeps about something in the workspace: a character
sheet, a work item, a build, a project status. An **entity schema** says what shape those
records have — and, optionally, what states they move through.

State machines are the part people most often do not know is there, so this page is
mostly about them. They are optional. A schema with no machines is a perfectly good
schema, and the shipped sample bundles carry no schemas of their own at all — a mystery
or a brand workshop has nothing with a lifecycle worth tracking; the machines they meet
come from the workflow pack the workspace pinned.

## What a schema holds

| Part | What it is |
|---|---|
| `fields` | The stored values: type, bounds, tags, whether a column is hoisted for indexing |
| `derived` | Values computed from fields by CEL, never stored |
| `state_machines` | Named machines with states, an initial state, and guarded transitions |
| `views` | Groups and tabs — how a sheet renders. A field no group mentions still renders, in a default group |
| `constraints` | CEL predicates that must hold after any write |

None of this is code. User-authored logic in Pyrrhula is JSON Schema, declarative FSMs
and CEL expressions — there is no `eval`, and no place to put a script.

## A state machine

```json
{
  "key": "health",
  "initial": "healthy",
  "states": [
    { "key": "healthy", "label_key": "status.healthy" },
    { "key": "bloodied", "label_key": "status.bloodied" },
    { "key": "down", "label_key": "status.unconscious", "on_enter": [
      { "kind": "set_field", "field": "hit_points", "value": "0" }
    ]}
  ],
  "transitions": [
    { "from": "healthy", "to": "bloodied", "trigger": "wound",
      "guard": "fields.hit_points <= fields.max_hit_points / 2" },
    { "from": "bloodied", "to": "down", "trigger": "wound",
      "guard": "fields.hit_points <= 0" },
    { "from": "bloodied", "to": "healthy", "trigger": "heal" }
  ]
}
```

- **States** carry a `label_key`, never a label. The word a reader sees comes from the
  workspace's vocabulary overlay, which is what lets the same machine read as "Bloodied"
  at a game table and "Degraded" in an operations workspace.
- **Transitions** are `(from, to, trigger)`. A `guard` is a CEL expression over
  `fields`/`derived`; a transition whose guard is false does not fire, and the caller is
  told it did not fire rather than getting an error. `"from": "*"` means *from any
  state* — the honest way to write "this can happen at any point" without listing every
  source.
- **Effects** run on entering or leaving a state, or on a transition:
  `set_field`, `apply_modifier`, `emit_event`, `invoke_tool`, `transition_other`. They
  are transactional with the transition — a failing effect rolls the whole thing back.

**Nothing here is tied to a ruleset.** `health` above is Basic Fantasy's, but the same
five pieces describe a work item's `lifecycle`, a pull request's review state, a build,
or a support ticket. The shipped packs use identical machinery for all of them.

## The state an entity is in

Every declared machine is in its `initial` state from the moment the entity is created.
That matters more than it sounds: state is what the UI lists entities *by*, so a record
whose states were never written down is a record some screens will not show you.

States change through `POST /entities/{id}/transition` with a workspace id, a machine key,
a trigger and an optional `expected_version` (the idempotency key is minted per call;
only the automation path supplies its own). `GET /entities/{id}/transitions` answers *what can this be moved to
from here* — worth asking before offering a person a button that a guard will refuse.

Every change appends an `entity_state_change` row — append-only, with no UPDATE or DELETE
grant for the app — recording the old state, the new one, what caused it, and which
session and event it belonged to. The sheet's history panel renders from those rows.

## Authoring one

**In the UI:** *Schemas* in the nav → pick a schema, or start one from a template (or
*Start from blank instead*) → the FSM editor
beside the field list. It draws the state graph, marks the initial state, and validates as
you go: a transition naming a state that does not exist, an `initial` that is not
declared, a guard that does not compile against the schema's field types, an effect
pointing at a field or machine that is not there, a state nothing can reach. Saving publishes a new **version** of the schema. An
entity points at the exact version it was created against and keeps it; a new version
changes what the *next* entity is created from, never what an existing one means.

**In a pack:** a JSON file under `<pack>/schemas/`. Pack content is data — it may not
import from `packages/core` (rule 9) — and it is the right home for anything a whole
ruleset shares rather than one workspace.

## Attaching one to a persona

A persona may **act through** an entity: the character it plays, the record it owns. Set
it in *Manage personas* → the persona → *Entity link*, which lists the workspace's
entities. Once linked, that entity's fields are what rules resolve against — a `1d20+STR`
check reads the persona's own STR rather than a default — and its machine states are what
the session shows next to the persona's name.

An agent can also do this for itself mid-session: `entity_create` with
`bind_to_self: true` creates the record and binds it. A persona acts through exactly one
entity; it may create as many others as its work needs.

## Where states show up

- **Workspace → Entities** — every entity, grouped by schema, with its current states.
- **The sheet** (`/workspaces/:id/entities/:entityId`) — fields by view group, the state
  chips, and the change history.
- **The session cast panel** — the live state of everything with a machine running, so
  someone watching a fight sees healthy → bloodied → down without opening sheets.
- **The work items panel** — the same mechanism, read as a status column.

## Taking them with you

Schemas and entities both travel in a `.pyr`, and an entity carries the state each of its
machines was in. See [portability.md](portability.md) — in particular what happens when
the importing workspace already has a schema under the same key.
