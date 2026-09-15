"""The Hollow Crown of Karsh Vale -- a Basic Fantasy RPG one-shot.

Rules content is translated from the **Basic Fantasy Role-Playing Game**, 4th edition
(release 142) by Chris Gonnerman and contributors, distributed under the Creative
Commons Attribution-ShareAlike 4.0 International License. The rules entries here are an
abridged restatement of that text and are redistributed under the same licence; see the
sample's README for the full attribution. The setting (Karsh Vale, the Hollow Crown, the
Tallowmen) is original to this sample and carries no ruleset text.

The point of the sample is not the adventure -- it is what a tabletop session looks like
when the platform, not the prompt, decides who knows what:

* **rules** knowledge is common to everyone, because a rule nobody can look up is not a
  rule (the referee still adjudicates, but nobody is guessing at the maths);
* **lore** is filed in three bands by how widely it is known -- common talk anyone may
  retrieve, guild knowledge only the two characters who earned it can reach, and the
  referee's own history of the Crown that no player character can ever retrieve;
* **misc** carries the tavern songs, the children's rhyme and the running joke, which are
  exactly the texture a session loses when a budget only funds rules and plot.

The bands are group scopes resolved as SQL predicates (INV-4), so a player character does
not "roleplay not knowing" the Crown's secret -- the text cannot reach their context.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CastMember:
    key: str
    name: str
    persona_type: str  # supervisor (the referee) | participant (a player character)
    persona_md: str
    axis_values: dict[str, int] = field(default_factory=dict)
    params: dict[str, object] = field(default_factory=dict)


REFEREE = CastMember(
    key="referee",
    name="The Referee",
    persona_type="supervisor",
    persona_md="""\
You are the Game Master of a Basic Fantasy RPG session, running *The Hollow Crown of
Karsh Vale* for three player characters.

Your job at this table:
- **Frame scenes, never speak for the player characters.** Describe what they see, hear
  and smell; then stop and ask what they do. If you catch yourself writing a player
  character's line or deciding their choice, cut it.
- **Call for rolls by the rules, and say which rule you are invoking.** "Roll 1d20 and
  add your attack bonus and Strength bonus against AC 13", "save vs. Death Ray",
  "roll 1d6 for initiative, high acts first". The rules are in your knowledge; use them
  rather than inventing a resolution.
- **Use the dice tool when a roll matters.** You have a resolution tool; call it and read
  the result. A result you rolled is the record -- never narrate a number you did not
  roll, and never overturn one you did.
- **Keep the spotlight moving.** Address a character by name when it is their moment.
- **Be concrete and quick.** Two or three sentences of scene, then a question. This is a
  table, not a novel.

You alone hold the history of the Crown. Reveal it only as the fiction earns it.""",
    axis_values={"chattiness": 70, "verbosity": 40},
    params={"temperature": 0.6},
)


CAST: tuple[CastMember, ...] = (
    CastMember(
        key="bram",
        name="Bram Oakenshield",
        persona_type="participant",
        persona_md="""\
You play **Bram Oakenshield**, a dwarf Fighter, 1st level.

STR 16 (+2) · INT 9 · WIS 11 · DEX 12 · CON 15 (+1) · CHA 8 (-1)
HP 9 · AC 16 (chainmail and shield) · Attack bonus +1
Saves: Death Ray 8, Magic Wands 5, Paralysis 6, Dragon Breath 10, Spells 9
(dwarven bonuses already applied)
Wielding a war hammer (1d6) and carrying a lantern.

You are a stonemason's son who took a soldier's wage and never went home. You trust
weight, stone and the people who stand where they said they would. You are blunt to the
point of rudeness with anyone who talks in circles, and you have a mason's eye for
worked stone -- you notice tool marks, joins and repairs that others walk past.

Speak in your own voice, briefly. Say what Bram does and let the Referee resolve it.
Never roll dice yourself and never narrate the outcome of your own action -- declare the
attempt and wait.""",
        axis_values={"chattiness": 55, "bravado": 70},
        params={"temperature": 0.8, "presence_penalty": 0.4},
    ),
    CastMember(
        key="linnea",
        name="Linnea Faelor",
        persona_type="participant",
        persona_md="""\
You play **Linnea Faelor**, an elf Magic-User, 1st level.

STR 8 (-1) · INT 17 (+2) · WIS 13 (+1) · DEX 14 (+1) · CON 10 · CHA 13 (+1)
HP 4 · AC 12 (no armour, Dexterity) · Attack bonus +0
Saves: Death Ray 13, Magic Wands 12, Paralysis 12, Dragon Breath 15, Spells 13
(elven bonuses already applied)
Dagger (1d4). **One** spell prepared: *magic missile* -- 1d6+1 damage, always hits.

You trained at a provincial college and were not invited to stay. You are precise,
curious, and privately certain you are the cleverest person in any room, which you try
(not always successfully) to keep out of your voice. You read inscriptions others
dismiss as decoration.

With four hit points and one spell, caution is not cowardice. Speak briefly, in your own
voice, and let the Referee resolve what you attempt.""",
        axis_values={"chattiness": 60, "caution": 75},
        params={"temperature": 0.8, "presence_penalty": 0.3},
    ),
    CastMember(
        key="pip",
        name="Pip Thistlewaite",
        persona_type="participant",
        persona_md="""\
You play **Pip Thistlewaite**, a halfling Thief, 1st level.

STR 9 · INT 12 · WIS 10 · DEX 17 (+2) · CON 12 · CHA 14 (+1)
HP 4 · AC 15 (leather, Dexterity, halfling size) · Attack bonus +0
Saves: Death Ray 9, Magic Wands 6, Paralysis 7, Dragon Breath 11, Spells 10
(halfling bonuses already applied)
Short sword (1d6), sling (1d4), thieves' tools.
Thief abilities at 1st level: Open Locks 25%, Remove Traps 20%, Pick Pockets 30%,
Move Silently 25%, Climb Walls 80%, Hide 10%, Listen 30%.
**Sneak Attack**: +4 to hit and double damage when striking an unaware foe from behind.

You are cheerful, light-fingered and allergic to heroics. You talk your way in before
you climb your way in, and you have never once volunteered to go first -- though you
usually end up there, because you are the only one who can hear what is on the far side
of a door.

Speak briefly, in your own voice. Declare what Pip tries; the Referee rolls.""",
        axis_values={"chattiness": 75, "caution": 60},
        params={"temperature": 0.9, "presence_penalty": 0.4},
    ),
)


# ── the rulebook (class: rules, scope: common to the whole table) ──────────────────
# Abridged from Basic Fantasy RPG r142, CC BY-SA 4.0. See the module docstring.
RULES: tuple[tuple[str, str, str], ...] = (
    (
        "abilities",
        "Ability Scores and Bonuses",
        """\
Every character has six abilities scored 3–18. One table converts a score into the
bonus that modifies rolls:

| Score | Bonus |
|-------|-------|
| 3     | -3 |
| 4–5   | -2 |
| 6–8   | -1 |
| 9–12  | 0  |
| 13–15 | +1 |
| 16–17 | +2 |
| 18    | +3 |

- **Strength** — melee attack and damage. Prime requisite of the Fighter. A penalty never
  reduces damage below 1.
- **Intelligence** — languages known. Prime requisite of the Magic-User.
- **Wisdom** — saves against will-affecting magic. Prime requisite of the Cleric.
- **Dexterity** — missile attacks, Armor Class, Initiative. Prime requisite of the Thief.
- **Constitution** — added to every hit die rolled, never below 1 point per die.
- **Charisma** — reaction rolls, and the number and loyalty of retainers.

A class's prime requisite must be at least 9 to join that class.""",
    ),
    (
        "attack",
        "Attack Rolls",
        """\
To attack, roll **1d20** and add:

- the attacker's **Attack Bonus** for class and level,
- **Strength** bonus for melee, or **Dexterity** bonus for missiles,
- situational modifiers.

If the total is **equal to or greater than the target's Armor Class**, the attack hits
and damage is rolled.

- A natural **1** always misses.
- A natural **20** always hits, if the target can be hit at all — a normal weapon still
  cannot harm a monster that requires silver or magic.
- Attacking **from behind** grants +2 (this does not stack with the Thief's Sneak Attack).

Missile range modifies the roll: **+1** at short range, **+0** at medium, **-2** at long,
and **-5** when firing at a foe engaged within 5 feet.""",
    ),
    (
        "initiative",
        "Initiative and the Combat Round",
        """\
Each round, every character and monster rolls **1d6** for Initiative, adjusted by the
**Dexterity** bonus. **High numbers act first**; equal numbers act simultaneously. The
GM may roll once for a group of identical monsters.

On its number a combatant may move up to its encounter movement and then attack if an
opponent is in range. After attacking it may not move again that round. A combatant may
deliberately wait and act on a later number.

Opponents more than 5 feet apart move freely; within 5 feet they are **engaged**.
A character with a long-reach weapon such as a spear may attack a closing opponent on
that opponent's number, even after losing initiative.""",
    ),
    (
        "saves",
        "Saving Throws",
        """\
A saving throw resists a special attack: roll **d20 against a target number** set by
class and level, and **meet or exceed it**. A natural 20 always succeeds; a natural 1
always fails.

The five categories are **Death Ray or Poison**, **Magic Wands**, **Paralysis or
Petrify**, **Dragon Breath**, and **Spells**. Death Ray serves as the catch-all for
ordinary dungeon hazards.

Saves are normally *not* adjusted by ability bonuses. The exceptions:
- **Poison** saves use the Constitution modifier.
- **Illusion** saves use the Intelligence modifier.
- **Charm** and mind-control saves use the Wisdom modifier.""",
    ),
    (
        "hitpoints",
        "Hit Points, Damage and Death",
        """\
A first-level character rolls a single hit die of the type given by their class, adds
the **Constitution** bonus, and has at least 1 hit point. Each new level rolls another
die and adds Constitution again, minimum 1. After 9th level, classes gain a fixed
number of hit points per level and no longer add Constitution.

Damage reduces the **current** total, never the rolled maximum; healing restores up to
that maximum. At **0 hit points** the character may be dead — the rules are deliberate
that this "may not be the end for the character.\"""",
    ),
    (
        "classes",
        "The Four Classes",
        """\
- **Fighter** — the best attack progression and d8 hit dice. Prime requisite Strength.
- **Cleric** — fights about as well as a Thief, hardier at low levels, d6 hit dice.
  Casts divine spells from **2nd level**, and can **Turn the Undead**. Prime requisite
  Wisdom.
- **Magic-User** — the worst fighter and the least hardy, d4 hit dice, but commands
  arcane spells. Prime requisite Intelligence.
- **Thief** — d4 hit dice, a suite of thief abilities improving by level, and the Sneak
  Attack. Prime requisite Dexterity.

Humans may also take combination classes: a **Fighter/Magic-User** may cast while
wearing armour and rolls d6 hit dice; a **Magic-User/Thief** may cast in leather and
rolls d4.""",
    ),
    (
        "races",
        "Character Races",
        """\
- **Dwarves** — save at **+4** vs. Death Ray or Poison, Magic Wands, Paralysis or
  Petrify, and Spells, and **+3** vs. Dragon Breath. Their stocky build lets them use
  Medium weapons one-handed; Large weapons over four feet (two-handed swords, polearms,
  longbows) are prohibited.
- **Elves** — save at **+1** vs. Paralysis or Petrify and **+2** vs. Magic Wands and
  Spells. Must wield Large weapons two-handed.
- **Halflings** — save as Dwarves (**+4**/**+3**). May not use Large weapons at all and
  must use Medium weapons with both hands.
- **Humans** — the standard: no saving throw bonuses, but they earn a **10% experience
  bonus** and may take combination classes.

Every non-Human race speaks its own language and Common. A character with Intelligence
13+ learns additional languages equal to their Intelligence bonus.""",
    ),
)


# ── lore, in three bands by how widely it is known ───────────────────────────────────
# This is the sample's centrepiece. Band membership is a group scope resolved in SQL on
# every retrieval (INV-4), so "your character doesn't know that" is a fact about the
# query, not an instruction the model may forget or be argued out of.

# Band 1: common talk. Anyone at the table may retrieve this -- it is what you would
# learn in a week at the inn.
COMMON_LORE: tuple[tuple[str, str, str], ...] = (
    (
        "karsh_vale",
        "Karsh Vale",
        """\
A high valley three days east of the Marches, ringed by limestone crags and reached by
one switchback road. Six hundred people, most of them herders, in the town of Ashmere and
four hamlets below it. The Vale is known for three things: hard cheese, harder winters,
and the ruin on the northern shoulder that everyone calls the Hollow Crown.""",
    ),
    (
        "hollow_crown",
        "The Hollow Crown",
        """\
The ruin above Ashmere: a ring of nine limestone towers, seven of them fallen, joined by
a curtain wall that has been quarried for barn stone since anyone can remember. It is
called hollow because the hill beneath it is -- the locals say a dropped stone in the
well takes a slow count of four to strike bottom.

Children dare each other to the gatehouse. Nobody grazes stock there. Nobody can tell you
why not, except that nobody does.""",
    ),
    (
        "the_disappearances",
        "What Brought You Here",
        """\
Since the turn of the season, eleven sheep, two dogs and -- eight days ago -- Marta
Fenn's son Aldo have gone missing from the upper pastures, always on a night with no
moon. There is no blood and no trail. The reeve of Ashmere has posted forty gold pieces
and the use of a cottage for the winter to anyone who ends it.

The only thing anyone agrees on: the losses are all from pastures within sight of the
Hollow Crown.""",
    ),
    (
        "tallowmen_rumour",
        "The Tallowmen (as told in the taproom)",
        """\
Ask in the Ram and Candle after the second cup and someone will tell you about the
tallowmen: figures the colour of old candle fat that come down from the Crown on dark
nights and walk without sound. They are said to be drawn to light, to be unable to cross
running water, and to be nothing at all -- a story to keep children off the crags.

Accounts differ on every detail except one: everyone agrees they leave no footprints.""",
    ),
)

# Band 2: guild knowledge. Filed under `guild_lore`, whose members are the Referee, Bram
# (a stonemason's son) and Linnea (college-trained). Pip has no route to it -- so if the
# party learns what the mason's marks mean, it is because a character who plausibly could
# know told the others, in the fiction.
GUILD_LORE: tuple[tuple[str, str, str], ...] = (
    (
        "masons_marks",
        "Reading the Masons' Marks",
        """\
The Crown's stonework is not one build but three. The lowest courses are dry-laid
limestone, unmarked, older than any guild. Above them sits careful ashlar carrying the
wedge-and-bar mark of the Karsh lodge, which dissolved four centuries ago. The topmost
work is crude infill, mortared in haste, and it is *inward*-facing -- the good face turned
into the hill rather than out at an enemy.

A mason reads that immediately: the last builders were not fortifying against the valley.
They were sealing something in, and they did it quickly.""",
    ),
    (
        "binding_scripts",
        "Binding Scripts and Ward-Cant",
        """\
The college teaches that a binding inscription is distinguishable from a decorative one
by repetition: a ward repeats its operative phrase at every aperture, because a ward is
only as strong as the opening it is written across. Look for the same clause carved at
every door, window and drain of a structure.

Ward-cant of the middle period favoured a closing formula meaning *let the account stay
balanced* -- a bookkeeping metaphor, because the school understood binding as a debt held
open rather than a wall held shut. A debt, unlike a wall, can be paid.""",
    ),
)

# Band 3: the referee's own history. Filed under `referee_lore` with exactly one member,
# so no player character can retrieve a word of it. This is what the session is *for*.
REFEREE_LORE: tuple[tuple[str, str, str], ...] = (
    (
        "truth_of_the_crown",
        "The Truth of the Hollow Crown",
        """\
**Referee only.**

The Crown is not a fortress. It is a lid.

Four hundred years ago the Karsh lodge was paid to seal the shaft beneath the hill, in
which the valley's people had for generations left a yearly tithe -- a lamb, latterly a
portion of the harvest -- for the thing that lived in the water at its bottom. The tithe
was not superstition; it was a contract, and the lodge's ward did not break it. It only
suspended it, in the ward-cant formula: *let the account stay balanced.*

The account has not been balanced for four hundred years. The interest is the tallowmen:
the shaft sends up what it is owed, in the shape of what it has taken. They are drawn to
light because light is how the tithe was once signalled -- a lamp set on the gatehouse
sill on a moonless night.

Aldo Fenn is alive, on a ledge nine feet above the water, and has been for eight days.
He is not bait. He is a **down payment**, and while he is down there the taking has
stopped -- which is why the losses ended the night he vanished, a detail the reeve has
not connected and the party can discover by asking when the last sheep went missing.

**The real decision.** The party can break the ward and free what is below (it will take
the valley), re-seal it and condemn the next generation to the same slow theft, or
*settle the account* -- which requires giving the shaft something it accepts as payment
in Aldo's place. A single lamb will not do; the debt is four centuries deep. What the
players devise is the adventure. There is no correct answer written here on purpose.

If the party simply climbs down and hauls the boy out with no settlement, the taking
resumes the following moonless night, and the Vale will know exactly whom to blame.""",
    ),
)


# ── miscellany: the texture a rules-and-plot budget leaves out ───────────────────────
MISCELLANY: tuple[tuple[str, str, str], ...] = (
    (
        "counting_rhyme",
        "The Ashmere Counting Rhyme",
        """\
Children in the Vale skip to this. Nobody thinks about the words.

> *One for the wall and two for the well,*
> *three for the lamp on the gatehouse sill,*
> *four hundred years and the counting fell,*
> *who pays the ram? The ram pays still.*

There are two more verses. Nobody remembers them, and the adults change the subject
pleasantly if asked.""",
    ),
    (
        "ram_and_candle",
        "The Ram and the Candle",
        """\
The inn's sign shows a ram with a lit candle balanced between its horns, and the story
told to travellers is that an innkeeper's ram once walked home through a blizzard with a
candle still burning on its head. The innkeeper, Sorrel Hake, tells it well and charges
for the second telling.

House custom: the last candle of the evening is never blown out. It is carried outside
and left to burn down on the step. Sorrel will say this is to welcome late travellers.
Her grandmother said it differently and Sorrel does not repeat it.""",
    ),
    (
        "cheese_joke",
        "The Cheese",
        """\
Karsh Vale cheese is famously, aggressively hard. The standing joke in the Marches is
that a Karsh round stopped a crossbow bolt at Coldwater Ford, and the joke in Karsh Vale
is that the bolt was fine but the cheese was ruined.

Anyone who orders it at the Ram and Candle will be handed a small hammer with it,
straight-faced, and the entire taproom will watch.""",
    ),
    (
        "marta_fenn",
        "Marta Fenn Sets Two Places",
        """\
Since Aldo vanished, his mother has set his place at every meal -- bowl, spoon, and the
heel of the loaf, which was his. She does not talk about it and takes badly to being
consoled.

Her neighbours have stopped mentioning it. They have also, without discussing it, started
leaving small things on her step: a twist of salt, a mended stocking, a jar of the good
honey. The Vale grieves by delivery.""",
    ),
)
