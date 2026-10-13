# Participants, ballots and private audiences

A flow can give each persona at the table its own variables, deal some of them out in
secret, run sealed votes, and keep a phase's conversation to the participants it is meant
for. Nothing here knows what a value means: a hidden-role game, an anonymous review panel
and a breakout room are the same parts with different values.

All of it is declared in the flow: JSON, with CEL wherever something has to be decided. The
[Mafia sample](https://github.com/tuturu742/pyrrhula-samples) is a full flow built from
these parts, and the examples below come from it.

## Settings: what the person starting a session chooses

```json
"settings": {
  "mafia_count": {"type": "integer", "default": 3, "min": 1, "max": 4,
                  "description": "how many players are dealt mafia"}
}
```

`state:` is what the flow computes as it runs. A setting is fixed at session start: a seat
count, a discussion length, how many of a role to deal. `POST /sessions` takes
`"settings": {"mafia_count": 2}`, checks it against the type and range, and records it on the
session. Any number in a flow that says `{"setting": "mafia_count"}` reads it, and CEL reads
it as `settings.mafia_count`.

## Participants and their variables

```json
"participants": {
  "vars": {
    "group":  {"type": "string",  "default": "town"},
    "role":   {"type": "string",  "default": "none"},
    "status": {"type": "string",  "default": "alive"}
  },
  "deal": [
    {"var": "group", "value": "mafia",   "count": {"setting": "mafia_count"}},
    {"var": "role",  "value": "don",     "count": 1, "where": "p.group == 'mafia'"},
    {"var": "role",  "value": "sheriff", "count": 1, "where": "p.group == 'town'"}
  ],
  "hidden": ["group", "role"],
  "team_var": "group",
  "team_values": ["mafia"],
  "seat_order": "drawn"
}
```

| Key | What it does |
|---|---|
| `vars` | One set per participant (every persona on the roster except the supervisor), typed like `state:` |
| `deal` | At session start, a recorded draw gives `count` participants `value`; everyone else keeps the default. Deals run in order, each from participants an earlier deal of the same var did not pick |
| `deal[].where` | CEL over `p` and the values dealt so far: narrows the draw. Deal a team first, then one of its members a role |
| `hidden` | Vars that only the participant itself and the supervisor see |
| `team_var` / `team_values` | Participants who share one of these values see each other's hidden vars: a team that knows its own members |
| `reveal_var` | A boolean var; once it is true for someone, their hidden vars are shown to everyone |
| `seat_order` | `drawn` (a recorded shuffle) or `roster` |

A hidden var is **excluded** from the context of everyone who may not see it. Models are not
told about it and asked to ignore it; it is left out of what they are given. The leak test
suite holds history, recaps and vote results to the same rule.

## Who acts: seat order and `where`

```json
"actors": [{"persona_type": "participant", "mode": "generate",
            "order": "seated", "laps": {"setting": "day_laps"}, "rotate_by": "state.day",
            "where": "p.status == 'alive'"}]
```

`order: seated` walks the participants in seat order, `laps` times, starting `rotate_by`
seats further on each time the phase is entered. `where` (CEL over `p`, `state`,
`settings`) decides who may act at all. It is re-checked at every turn, so someone removed
mid-phase loses their remaining turns.

## An open floor: raised hands

```json
"actors": [{"persona_type": "participant", "mode": "generate",
            "order": "hands", "max_turns_each": {"setting": "turns_each"},
            "where": "p.status == 'alive'"}]
```

`order: hands` runs a discussion instead of a speech round:

1. Everyone `where` admits speaks once, in seat order. Flows without participants use declared order.
2. Then the floor goes to whoever the last message named, by whole word, so "Sage" is
   not named by "message". An accused participant answers straight away.
3. Nobody named: the floor goes to the hand raised longest ago. A speaker raises a hand
   by calling the `raise_hand` tool during a turn. The hand stays up until their next
   turn. Not calling it is how a speaker passes.
4. Nobody named and no hand up ends the entry.

`answer_named: false` turns step 2 off, so the floor moves on raised hands alone. Use it
for a small private group whose members name each other in every line, where answering
every name becomes a loop of agreement.

`max_turns_each` caps one participant's turns in the phase, the opening one included. The
`raise_hand` tool tells each speaker how many turns they have left. `max_turns` caps the
entry's total. The previous speaker never follows themselves while anyone else wants the
floor. Raised hands are recorded per turn (`floor_request`, append-only), so the order of
a discussion is decided from the record, never from a model's words.

## Who hears it: audiences

```json
"audience": {"members": "p.group == 'mafia'"}
```

What is said in the phase reaches only the participants `members` admits, plus the speaker
and the supervisor. For everyone else those messages are absent from history: a private
channel. `"members": "false"` makes a phase's words readable by the supervisor alone. That
is useful for the one-line reason a voter gives with a sealed vote.

## Sealed ballots

```json
"ballot": {
  "targets": "p.id != voter.id && p.status == 'alive'",
  "tie": "none",
  "reveal": "votes",
  "on_chosen": [{"set": "status", "to": "'out'"}],
  "on_tied":   [{"set": "tied",   "to": "true"}]
}
```

A phase with a `ballot` is a vote. Each actor gets a `ballot` tool and casts one vote for a
participant `targets` admits. No context shows a ballot before the phase ends. The engine
then counts them, records the result, and applies `on_chosen` to whoever has the most votes,
in the same transaction as the phase change. A model that answers in words without voting
is asked once more within its turn; after that it counts as an abstention.

| Key | Values |
|---|---|
| `tie` | `none`: a tie chooses nobody. `draw`: a recorded draw settles it |
| `reveal` | `votes`: every ballot becomes public. `outcome`: only who was chosen, for a vote whose voters must stay unknown. `voters`: the result reaches only those who voted (a private check, below) |
| `on_chosen` | Effects on the chosen participant: `to` is CEL with `p` bound to them |
| `on_tied` | Effects on each participant of a tie that chose nobody. Mark them, and let a following phase hold a run-off with `targets` limited to them |

Results render from the record (the vote, who was chosen, what changed), never from a
model's retelling. A change to a hidden var is shown only to the supervisor, the participant
changed, and the voters of a private vote.

## Private checks

```json
"ballot": {
  "targets": "p.id != voter.id && p.status == 'alive'",
  "reveal": "voters",
  "disclose": {"is_mafia": "p.group == 'mafia'"}
}
```

With `reveal: voters`, nothing about the vote becomes public. `disclose` computes named facts
about the chosen participant from the record, with every var available, hidden or not. The
whole result, facts included, reaches only the participants who voted. **The supervisor does
not see it either**: a facilitating model that knows a private result can only leak it. The
Sheriff asks "is this one mafia?", and the engine answers to the Sheriff alone.

## Effects when a phase ends

```json
"participant_effects": [
  {"set": "status", "to": "'out'", "where": "p.shot"},
  {"set": "shot",   "to": "false", "where": "p.shot"}
]
```

Sets a var on every participant `where` admits (everyone, when it is absent) as the phase
ends. These run before the phase's own vote closes, so a reset here never undoes what the
vote sets. Clear last round's marks, then let the vote mark this round's.

Gates read participants as a list:
`size(participants.filter(p, p.group == 'mafia' && p.status == 'alive')) == 0`.

## Telling models where they are

Long sessions replay a lot of history. Two engine options keep it readable. Both are the
same in every flow, so a flow sets a default and `POST /sessions` may override it with
`"options"`.

| Option | Values |
|---|---|
| `phase_markers` | `transcript`: a phase's `marker` line ("Night {night}: the mafia talk", reading state) appears in the replayed history wherever that phase began. `current`: only the running phase's marker, as context. `none` (default) |
| `recap` | `on`: when a phase ends, each viewer's history gets a recap of it written from the record, under that viewer's visibility: votes, rolls, changes, what it was told, who spoke, knowledge first shown, its own tool calls. Empty sections are left out. With a trimmed history, the recaps of the dropped phases stay. Default off |

In the Mafia A/B runs, markers cut the models' "who is still in, which phase is this"
errors from 6.2% to 0.8%. Recaps showed no measurable gain there, which is why they are
off unless a flow or session asks for them.
