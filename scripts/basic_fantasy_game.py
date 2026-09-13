"""Play a full Basic Fantasy RPG game, in-process, on the real engine.

One GM and three players. The players' six abilities and their hit points are rolled with
the engine's own dice against the basic_fantasy rule system -- real ResolutionRecords, real
BFRPG bonus math -- and become character entities. The GM then narrates a three-encounter
adventure; each encounter's attack or save is resolved the same honest way, and the
outcome drives the character's health state machine. The models supply the story around
the dice; the engine supplies the dice and every modifier.

Run it in-pod (it needs DB + resolution + model access), with a DeepSeek key in the
environment. On k8s::

    KEY=...  # your deepseek key
    POD=$(kubectl -n pyrrhula get pod -l app=pyrrhula-api \
        -o jsonpath='{.items[0].metadata.name}')
    kubectl -n pyrrhula exec -i "$POD" -- env DEEPSEEK_KEY="$KEY" \
        python - < scripts/basic_fantasy_game.py

Each run mints a throwaway ``bfrpg-<hex>`` tenant on the tabletop workflow and prints, on
its last line, an org/email/password you can log in with to read the transcript and open
the three characters' sheets. It spends real DeepSeek credit (seven model turns).
"""

import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, "packages")

from sqlalchemy import select, text

from adapters.identity.local.argon2_provider import LocalArgon2IdentityProvider
from api.blob_store_factory import get_blob_store
from api.embedding_provider_factory import get_embedding_provider
from api.encryptor_factory import get_encryptor
from api.job_queue_factory import get_job_queue
from api.mcp_transport_factory import get_mcp_transport
from api.model_provider_factory import get_model_provider
from api.moderation_provider_factory import get_moderation_provider
from api.permission_service_factory import get_permission_service
from core.agents.authoring import create_agent, create_persona
from core.entities.mutation import create as create_entity
from core.knowledge.authoring import (
    attach_source_to_workspace,
    create_source,
    publish_version,
)
from core.process.authoring import list_definitions
from core.process.dsl.validator import validate_raw
from core.process.live_session import run_directed_persona_turn
from core.resolution.consequence import resolve_and_apply
from core.resolution.rule_system import RuleSystemDefinition, get_rule_system
from core.resolution.service import resolve
from core.sessions.models import MessageRow
from core.sessions.notes import post_note
from core.tenancy.models import Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant
from core.workflows.service import set_tenant_workflow

ENC = get_encryptor()
PERM = get_permission_service()
KEY = os.environ["DEEPSEEK_KEY"]
SLUG = f"bfrpg-{str(uuid.uuid4())[:8]}"

# --- the four classes, faithful to the rulebook ------------------------------------
CLASSES = {
    "Fighter": {"hd": 8, "prime": "strength", "atk": 1},
    "Cleric": {"hd": 6, "prime": "wisdom", "atk": 1},
    "Magic-User": {"hd": 4, "prime": "intelligence", "atk": 1},
    "Thief": {"hd": 4, "prime": "dexterity", "atk": 1},
}
# level-1 saving throws by class: [death_ray, magic_wands, paralysis, dragon_breath, spells]
SAVES = {
    "Fighter": [12, 13, 14, 15, 17],
    "Cleric": [11, 12, 14, 16, 15],
    "Magic-User": [13, 14, 13, 16, 15],
    "Thief": [13, 14, 13, 16, 15],
}
ABILITIES = ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]


HANDBOOK_MD = r"""
# Basic Fantasy RPG — table reference

This is a condensed play reference for the Basic Fantasy Role-Playing Game, written for
agents running or playing at the table. It states the mechanics; it is not the rulebook.

## The dice

A d20 resolves attack rolls and saving throws: roll, add modifiers, and if the total
meets or beats the target number it succeeds. A natural 20 always hits or saves; a natural
1 always fails. Percentile rolls (d%) resolve thief skills — roll two d10 for 1–100 and
succeed on a result at or below the skill's percentage. Damage and hit points use d4, d6,
d8, d10, and d12.

## Ability scores

Six abilities, each 3–18, each carrying a bonus by this table:

| Score | 3 | 4–5 | 6–8 | 9–12 | 13–15 | 16–17 | 18 |
|---|---|---|---|---|---|---|---|
| Bonus | −3 | −2 | −1 | 0 | +1 | +2 | +3 |

- **Strength** — melee attack and damage. Prime requisite for Fighters.
- **Intelligence** — bonus languages. Prime requisite for Magic-Users.
- **Wisdom** — some saves vs. magic. Prime requisite for Clerics.
- **Dexterity** — missile attack, Armor Class, initiative. Prime requisite for Thieves.
- **Constitution** — added to every hit die rolled (never below 1 per die).
- **Charisma** — reaction rolls, and the number and loyalty of retainers.

Roll 3d6 in order for each. A class needs its prime requisite at 9 or higher.

## Races

- **Human** — any class, no ability requirements, +10% experience.
- **Dwarf** — Cleric, Fighter, or Thief; needs CON 9, CHA at most 17; darkvision 60';
  detects stonework traps and shifting walls (1–2 on 1d6); saves +4 vs. most categories,
  +3 vs. Dragon Breath; no large weapons.
- **Elf** — any of the four classes (and Fighter/Magic-User or Magic-User/Thief
  combinations); needs INT 9, CON at most 17; darkvision 60'; finds secret doors more
  readily; immune to ghoul paralysis; hit dice never larger than d6; saves +1 vs.
  Paralysis, +2 vs. Wands and Spells.
- **Halfling** — Cleric, Fighter, or Thief; needs DEX 9, STR at most 17; +1 to missile
  attacks; +2 AC vs. larger-than-man-sized foes; +1 initiative; hides very effectively;
  hit dice never larger than d6; saves +4 vs. most categories, +3 vs. Dragon Breath; no
  large weapons.

## Classes

| Class | Hit die | Prime | Notes |
|---|---|---|---|
| Fighter | d8 | STR | Best attack progression; any weapon or armor. |
| Cleric | d6 | WIS | Divine spells (none at level 1); turns undead; no edged weapons. |
| Magic-User | d4 | INT | Arcane spells (one at level 1); no armor; dagger only. |
| Thief | d4 | DEX | Percentile skills (climb, hide, pick locks, ...); backstab; leather only. |

At level 1 a character has one hit die of their class type plus their Constitution bonus,
minimum 1 hit point.

## Combat

- **Armor Class** — higher is better. Unarmored is 11; leather 13; chain 15; plate 17;
  a shield adds 1. Add the Dexterity bonus.
- **Attack** — roll d20, add the character's attack bonus and the relevant ability bonus
  (Strength in melee, Dexterity for missiles). Hit if the total meets or beats the
  target's Armor Class. A first-level character's attack bonus is +1.
- **Damage** — the weapon's die plus the Strength bonus in melee (never below 1). A
  dagger deals 1d4, a sword 1d8, a two-handed sword 1d10.
- **Initiative** — each side rolls 1d6 plus Dexterity bonus each round; higher acts first.
- **Hit points** reaching 0 means the character is dying or dead.

## Saving throws

Roll d20 and meet or beat the target for the category. Level-1 targets:

| Class | Death Ray / Poison | Magic Wands | Paralysis / Petrify | Dragon Breath | Spells |
|---|---|---|---|---|---|
| Cleric | 11 | 12 | 14 | 16 | 15 |
| Fighter | 12 | 13 | 14 | 15 | 17 |
| Magic-User | 13 | 14 | 13 | 16 | 15 |
| Thief | 13 | 14 | 13 | 16 | 15 |

Apply the racial bonuses above (subtract them from the target, or add them to the roll).

## Advancement

Characters earn experience points from defeating monsters and recovering treasure; each
class has its own table of XP required per level. Humans earn a 10% XP bonus.

---

*Based on the [Basic Fantasy Role-Playing Game](https://basicfantasy.org) by Chris
Gonnerman and contributors, © 2006–2025, distributed under the
[Creative Commons Attribution-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-sa/4.0/).
This reference is an original condensation of the game's rules — game mechanics are not
themselves copyrightable — and is likewise offered under CC-BY-SA 4.0. It contains none of
the rulebook's descriptive prose, artwork, or monster/treasure text.*
"""

LORE_MD = r"""
# Thornwick and the Gallowfen — a lorebook

The world every character at this table already knows. Not rules, not secrets — the
common ground a villager, a traveller, or an adventurer would take for granted.

## The region

The road to **Thornwick** runs north through the **Gallowfen**, a wide sour marsh of
black water and leaning pine, on a raised causeway of old dwarf-laid stone. The fen has
swallowed three kings' worth of failed drainage schemes and the occasional careless
traveller; the causeway is the only dry way across, single-file in places, and it floods
at the spring melt. Locals do not leave it after dark.

Thornwick itself is a chapel village of perhaps two hundred souls: a market square, a
smithy, the Drowned Sun inn, terraced barley on the drier slopes, and above it all the
**grey chapel** with its bell tower. The village lives by the road tolls, the barley, and
the pilgrims who come for the chapel's relic-crypt beneath the altar.

## The chapel and its bell

The chapel bell has rung the hours over Thornwick for eleven generations. It is rung
**at dawn, at noon, at dusk, and for the dead** — never otherwise, and everyone for a
day's walk sets their day by it. A bell rung out of turn means alarm: fire, flood, or
worse. A bell gone **silent** is read as an omen; the last time it fell quiet, in the
grandmother's grandmother's day, the story goes that something in the crypt "turned over
in its sleep."

Beneath the chapel is the **crypt**: the old abbots, a handful of local notables, and —
by long rumour — older graves the abbots built over rather than moved.

## Powers, coin, and faith

The land hereabouts answers to the **Margravine at Vessel Keep**, three days south, who
keeps the roads and takes a tenth. Local matters are settled by the **reeve** of
Thornwick and the chapel's **parson**. Coin is the Margravine's silver mark and copper
bit; a labourer earns a few bits a day, a night at the Drowned Sun costs a bit with
breakfast.

Most folk keep the **Threefold** — a quiet faith of hearth, road, and grave, whose
clerics tend shrines and turn away the restless dead. The dwarves of the eastern deeps
keep their own **Stonefathers**; elves are known but rare this far into human country and
draw stares in a place like Thornwick.

## What everyone knows about the dark

- **The dead do not always stay down.** Everyone knows someone who knew someone. Salt on
  a threshold, iron over a door, a cleric's blessing on a new grave — these are ordinary
  precautions, not superstition, in the Gallowfen.
- **Fen-lights** lead travellers off the causeway to drown; you do not follow a light you
  cannot name.
- **Goblins and worse** den in the drowned ruins out in the marsh and raid the barley in
  lean years. The village militia is a dozen men with billhooks and no illusions.
- **Names have weight.** You do not speak the name of a thing in the dark if you would
  rather it did not answer. This is why village folk talk around a danger rather than at
  it — a habit an outsider mistakes for evasiveness.

## The current trouble

Three days ago the Thornwick bell fell silent mid-toll and has not rung since. A boy sent
up the tower came down white and would not speak. The parson has barred the crypt stair.
The reeve has sent to the road-wardens for anyone willing to go down and find out why the
bell that has never stopped in living memory has stopped now — which is the errand that
brings adventurers to a village like this one.
"""

MISC_MD = r"""
# The Gallowfen miscellany

Trivia, sayings, and flavour from the road to Thornwick. None of it is load-bearing —
it is the texture that keeps a table from sounding like anywhere.

## Sayings heard on the causeway

- *"Mind the bell, mind the road, mind your own."* — the Thornwick greeting and warning
  in one.
- *"Dry boots, long life."* — fen wisdom; the marsh kills more by cold and rot than by
  monsters.
- *"Salt the sill and sleep sound."* — grandmother's advice against the restless dead.
- *"A light you can't name, you don't follow."* — every fen child learns this before they
  learn their letters.
- *"The Margravine's tenth and the fen's half."* — what the locals say the road really
  costs.

## The Drowned Sun inn

Thornwick's only inn, named for the way the sun looks going down into the marsh. The
innkeeper, **Old Cray**, waters the ale and denies it to your face. House specialty is
**eel pie** and a peaty barley spirit the locals call **fen-fire** that outsiders regret.
The common room has a warped dartboard, a three-legged dog named **Tithe**, and a rule
posted over the bar: *no weapons drawn, no names spoken after the dusk bell.*

## Drinking songs and tales

- **"The Bell and the Bone"** — a long, grim ballad about a sexton who rang the bell for
  his own funeral. Every verse ends *"and still it would not stop."* Considered bad luck
  to sing to the end; nobody at the Drowned Sun ever has.
- **"Nine Kings' Ditches"** — a bawdy counting song about the nine failed attempts to
  drain the Gallowfen, each king dumber than the last.
- Children skip rope to *"one for the dawn, two for the noon, three for the dusk, four
  for the... "* — and stop there, because the fourth ringing is for the dead and you do
  not finish the rhyme.

## Small true things

- Thornwick barley makes a famously bad bread and a famously good spirit.
- The chapel weathercock is shaped like a heron, not a cock, and points the wrong way in
  a west wind; the village has argued about fixing it for forty years.
- Dwarves passing through always tap the causeway stones with a knuckle and nod — those
  are dwarf-laid stones, and it is a courtesy to the long-dead masons.
- The three-legged inn dog, Tithe, will not go up the chapel lane. Nobody remarks on it
  anymore. They should.

*Based on the Basic Fantasy RPG (CC-BY-SA 4.0) setting conventions; all place-names,
characters, and text here are original to this sample.*
"""


SCHOLARLY_MD = r"""
# The Sundering and the Bell — scholar's lore

Deep history of the Gallowfen, known only to the learned: a magic-user, a cleric of the
Threefold with a good archive, or a scholar who has read the sealed chronicles. A
wasteland fighter or a village halfling would have no way to know any of this.

## The Sundering

Five centuries ago the region was not marsh but farmland, until the working called the
**Sundering** drowned it in a single season. The chronicles blame a pact-gone-wrong: a
circle of mages bound *something* beneath the old shrine at Thornwick to end a plague, and
paid for it with the valley. The **mages' circle was dissolved** afterward and its members
struck from every roll — which is why no common history names them.

## Archmagister Vaelith Corr

The circle was led by **Archmagister Vaelith Corr**, whose name survives only in the
sealed chronicles. Corr did not die in the Sundering; the archives imply Corr *became*
part of the binding — the will that keeps the thing beneath the chapel asleep. A scholar
who knows this understands the true stakes of a silent bell.

## The Bell as ward

The Thornwick bell is not merely a village clock. Its bronze was cast with **Corr's binding
sigils on the inner lip**, and its four daily tollings are a *renewal* of the ward, not a
timekeeping habit. This is why the bell has "never stopped in living memory" — stopping it
is not neglect, it is **release**. A learned character hearing that the bell has fallen
silent for three days knows, with cold certainty, what that means: the ward is failing,
and whatever Corr bound is waking. The villagers only know the bell is quiet and the omen
is bad; a scholar knows the mechanism, the name, and the countdown.
"""




def bonus(score):
    return (
        -3
        if score <= 3
        else -2
        if score <= 5
        else -1
        if score <= 8
        else 0
        if score <= 12
        else 1
        if score <= 15
        else 2
        if score <= 17
        else 3
    )


PARTY = [
    (
        "bram",
        "Bram Oakhelm",
        "Dwarf",
        "Fighter",
        "You are gruff and loyal, and you trust an axe over a plan. You speak plainly, "
        "guard the others, and never back down from a fair fight.",
    ),
    (
        "linnea",
        "Linnea Ash",
        "Elf",
        "Magic-User",
        "You are curious and precise, and you read every room for its magic. You speak "
        "carefully, hoard knowledge, and keep your one spell for when it truly matters.",
    ),
    (
        "pip",
        "Pip Underburrow",
        "Halfling",
        "Thief",
        "You are quick and cheerful, and you find the trap before it finds the party. "
        "You talk fast, scout ahead, and swear every risk was smaller than it looked.",
    ),
]


async def one_roll(tenant_id, rs, rs_id, session_id, seq, check_type, expr, fields, target=None):
    rec = await resolve(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=seq,
        tool_key="bfrpg_dice",
        actor_entity_id=None,
        expression=expr,
        check_type=check_type,
        actor_fields=fields,
        target=target,
        rule_system=rs,
        rule_system_id=rs_id,
        legal_check_types=rs.check_types,
    )
    return rec


async def main():
    # 1. tenant + rpg workflow (materializes basic_fantasy + bfrpg_dice for this tenant)
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=SLUG, tenant_name="Basic Fantasy Table", owner_display_name="Referee"
    )
    await set_tenant_workflow(tenant_id, "rpg")

    # First-person voice is a STYLE rule -> the workspace's conduct_rules (the UI-editable
    # lever), injected untruncated into every turn's system prompt. persona_md is capped at
    # 600 chars upstream, so the voice rule must not live only there.
    async with tenant_scope(tenant_id) as sdb:
        ws_row = await sdb.get(Workspace, workspace_id)
        ws_row.settings = {
            **(ws_row.settings or {}),
            "conduct_rules": (
                'Everyone speaks in the FIRST PERSON, in character, present tense: '
                '"I draw my axe", never "Bram draws his axe", and never narrate yourself '
                "from the outside or describe your own appearance in the third person. Do "
                "not prefix your line with your own name. Players: keep it to a few "
                "sentences -- what you say and what you do -- and never roll dice or call "
                "tools; the Game Master calls for rolls and the table's dice decide. The "
                'Game Master addresses the party as "you", voices monsters and NPCs in the '
                "first person, and never speaks or acts for a player character."
            ),
        }

    email_addr, password = f"referee@{SLUG}.example", "bfrpg-pass-12345"
    try:
        await LocalArgon2IdentityProvider().register_local(
            tenant_id, owner_id, email_addr, password
        )
    except Exception as exc:  # already exists on a re-run
        print("  (login exists)", exc)
    print(f"tenant {SLUG}  ws {workspace_id}")

    rs_row = await get_rule_system(tenant_id, "basic_fantasy")
    assert rs_row is not None, "basic_fantasy did not materialize for this tenant"
    rs = RuleSystemDefinition.from_row(rs_row)
    rs_id = rs_row.id
    print(f"rule system: {rs_row.key} ({len(rs.check_types)} check types)")

    # 1b. the library: three knowledge sources, one per class, so the table has the same
    # split a real workspace does. The CLASS KEY is what the flow's budget allocator keys
    # on -- rules where a check must be correct, lore where narration must be consistent,
    # misc as seasoning -- so filing under the right key is what makes retrieval spend
    # tokens on it at all. Chunk + embed run in the worker; we poll each job.
    library = [
        ("bfrpg_rules", "Basic Fantasy RPG — table reference", "rules", HANDBOOK_MD),
        ("thornwick_lore", "Thornwick and the Gallowfen", "lore", LORE_MD),
        ("gallowfen_misc", "The Gallowfen miscellany", "misc", MISC_MD),
    ]
    for key, name, klass, body in library:
        src = await create_source(
            tenant_id, key, name, klass, owner_principal_id=owner_id, visibility="tenant"
        )
        blob_key = f"knowledge/{tenant_id}/{src.id}/{uuid.uuid4()}-{key}.md"
        await get_blob_store().put(blob_key, body.encode(), content_type="text/markdown")
        job_id = await get_job_queue().enqueue(
            tenant_id,
            "knowledge_ingest",
            {
                "tenant_id": str(tenant_id),
                "knowledge_source_id": str(src.id),
                "blob_key": blob_key,
                "filename": f"{key}.md",
                "class": klass,
                "scope_key": "workspace_public",
            },
        )
        st = None
        for _ in range(120):
            await asyncio.sleep(2)
            async with tenant_scope(tenant_id) as sdb:
                st = (
                    await sdb.execute(
                        text("select status from job where id=:j").bindparams(j=job_id)
                    )
                ).scalar_one_or_none()
            if st in ("done", "failed"):
                break
        async with tenant_scope(tenant_id) as sdb:
            n_entries = (
                await sdb.execute(
                    text(
                        "select count(*) from knowledge_entry where knowledge_source_id=:s"
                    ).bindparams(s=src.id)
                )
            ).scalar_one()
        if n_entries:
            await publish_version(tenant_id, src.id, created_by=owner_id, change_note=name)
            await attach_source_to_workspace(tenant_id, workspace_id, src.id, "workspace_public")
        print(f"  [{klass:5}] {name}: ingest {st}, {n_entries} entries, attached={bool(n_entries)}")

    # 2. a DeepSeek connection, one GM + three players, all on it
    conn = await create_agent(
        tenant_id, "DeepSeek", "deepseek", "deepseek-chat", api_key=KEY, encryptor=ENC
    )
    gm = await create_persona(
        tenant_id,
        workspace_id,
        "gm",
        "Game Master",
        conn.id,
        persona_type="supervisor",
        persona_md=(
            "You are the Game Master of a Basic Fantasy RPG one-shot. You narrate the "
            'world in the second person to the party ("you round the bend and...") and '
            "voice monsters and NPCs in the first person, in-character. You never speak "
            "FOR a player character and never decide a roll's result yourself -- the "
            "table's dice do, and their results are handed to you to narrate. Keep scenes "
            "tight and vivid, name the monster the party faces plainly, and drive the "
            "adventure through its three encounters to a real ending."
        ),
    )
    players = {}
    for key, name, race, klass, md in PARTY:
        p = await create_persona(
            tenant_id,
            workspace_id,
            key,
            name,
            conn.id,
            persona_type="participant",
            persona_md=f"You are {name}, a level-1 {race} {klass}. " + md,
        )
        players[key] = p
    print(f"personas: GM + {', '.join(players)}")

    # every persona needs a workspace role to act / hold entities; the owner needs a
    # steward membership to manage the workspace in the UI (edit scopes, settings, ...) --
    # tenant ownership alone does not grant workspace permissions, exactly as the signup
    # flow's own owner membership reflects.
    async with tenant_scope(tenant_id) as s:
        s.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=owner_id,
                role="steward",
            )
        )
        for p in [gm, *players.values()]:
            s.add(
                WorkspaceMembership(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    principal_id=p.principal_id,
                    role="facilitator",
                )
            )

    # 1c. LEVELS OF LORE. Common lore (above) is workspace_public -- everyone at the table
    # knows it. Deep history is filed under a `scholarly_lore` GROUP scope whose only
    # members are the GM and Linnea (the elf magic-user). Bram the dwarf fighter and Pip
    # the halfling thief are not members, so the Sundering, Archmagister Vaelith Corr, and
    # the bell-as-ward simply never enter their context -- not because we tell them to act
    # ignorant, but because scope membership is resolved in SQL. Same machinery as secrets,
    # one notch softer: static who-knows-what rather than a per-turn gate.
    from core.assembler.models import ScopeRow

    async with tenant_scope(tenant_id) as s:
        s.add(
            ScopeRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                key="scholarly_lore",
                kind="group",
                members={
                    "principal_ids": [
                        str(gm.principal_id),
                        str(players["linnea"].principal_id),
                    ]
                },
            )
        )
    sch = await create_source(
        tenant_id, "gallowfen_deep", "The Sundering and the Bell", "lore",
        owner_principal_id=owner_id, visibility="tenant"
    )
    blob_key = f"knowledge/{tenant_id}/{sch.id}/{uuid.uuid4()}-scholarly.md"
    await get_blob_store().put(blob_key, SCHOLARLY_MD.encode(), content_type="text/markdown")
    job_id = await get_job_queue().enqueue(
        tenant_id,
        "knowledge_ingest",
        {
            "tenant_id": str(tenant_id),
            "knowledge_source_id": str(sch.id),
            "blob_key": blob_key,
            "filename": "gallowfen_deep.md",
            "class": "lore",
            "scope_key": "scholarly_lore",  # <-- the restricted band
        },
    )
    st = None
    for _ in range(120):
        await asyncio.sleep(2)
        async with tenant_scope(tenant_id) as sdb:
            st = (
                await sdb.execute(text("select status from job where id=:j").bindparams(j=job_id))
            ).scalar_one_or_none()
        if st in ("done", "failed"):
            break
    async with tenant_scope(tenant_id) as sdb:
        n_sch = (
            await sdb.execute(
                text(
                    "select count(*) from knowledge_entry where knowledge_source_id=:s"
                ).bindparams(s=sch.id)
            )
        ).scalar_one()
    if n_sch:
        await publish_version(tenant_id, sch.id, created_by=owner_id, change_note="scholarly lore")
        await attach_source_to_workspace(tenant_id, workspace_id, sch.id, "scholarly_lore")
    print(
        f"  [lore*] The Sundering and the Bell: ingest {st}, {n_sch} entries, "
        f"scope=scholarly_lore (GM + Linnea only)"
    )
    # Prove it, per persona: who is entitled to the scholarly_lore band?
    from core.assembler.visibility import scopes_for
    from core.process.dsl.schema import VisibilitySpec as _Vis

    _probe = _Vis(
        knowledge_classes=["lore"],
        scopes=["workspace_public", "scholarly_lore"],
        entity_fields="all",
        secrets="none",
    )
    print("  who knows the deep history (Archmagister Vaelith Corr, the bell as ward)?")
    for pk, p in players.items():
        entitled = await scopes_for(tenant_id, p.principal_id, workspace_id, _probe, None)
        knows = "scholarly_lore" in entitled
        print(f"    {p.name:18} ({pk:7}) -> {'KNOWS it' if knows else 'does not know it'}")

    # 3. CHARACTER CREATION -- real dice against basic_fantasy
    chargen = uuid.uuid4()
    async with tenant_scope(tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO session (id, tenant_id, workspace_id, persona_id, current_phase,"
                " status, state, next_event_seq)"
                " VALUES (:i,:t,:w,:p,'chargen','active','{}'::jsonb,0)"
            ).bindparams(i=chargen, t=tenant_id, w=workspace_id, p=gm.id)
        )
    seq = 0
    characters = {}
    print("\n=== CHARACTER CREATION (3d6 in order, rolled by the engine) ===")
    for key, name, race, klass, _ in PARTY:
        scores = {}
        for ab in ABILITIES:
            rec = await one_roll(
                tenant_id, rs, rs_id, chargen, seq, f"{ab}_check", "3d6", {}, target=1
            )
            seq += 1
            scores[ab] = int(rec.total)  # 3d6, no modifier applied to the roll itself
        # HP: class hit die + CON bonus, min 1
        hp_rec = await one_roll(
            tenant_id,
            rs,
            rs_id,
            chargen,
            seq,
            "constitution_check",
            f"1d{CLASSES[klass]['hd']}",
            {},
            target=1,
        )
        seq += 1
        con_b = bonus(scores["constitution"])
        hp = max(1, int(hp_rec.total) + con_b)
        atk = CLASSES[klass]["atk"]
        ac = 13 + bonus(scores["dexterity"])  # leather + DEX, a sensible level-1 default
        gold_rec = await one_roll(
            tenant_id, rs, rs_id, chargen, seq, "charisma_check", "3d6", {}, target=1
        )
        seq += 1
        sv = SAVES[klass]
        fields = {
            **scores,
            "race": race,
            "char_class": klass,
            "max_hit_points": hp,
            "hit_points": hp,
            "armor_class": ac,
            "attack_bonus": atk,
            "save_death_ray": sv[0],
            "save_magic_wands": sv[1],
            "save_paralysis": sv[2],
            "save_dragon_breath": sv[3],
            "save_spells": sv[4],
            "gold": int(gold_rec.total) * 10,
            "conditions": [],
            "xp": 0,
        }
        ent = await create_entity(
            gm.principal_id,
            tenant_id,
            workspace_id,
            "bfrpg_character",
            f"pc-{key}",
            name,
            fields,
            "workspace_public",
            idempotency_key=f"pc-{key}-{uuid.uuid4()}",
            permission_service=PERM,
        )
        # Materialise the health machine's initial state so every sheet shows it from the
        # start (creation leaves fsm_states empty until the first transition fires).
        async with tenant_scope(tenant_id) as sdb:
            await sdb.execute(
                text(
                    "UPDATE entity SET fsm_states = '{\"health\": \"healthy\"}'::jsonb "
                    "WHERE id = CAST(:e AS uuid)"
                ).bindparams(e=ent["entity_id"])
            )
        characters[key] = {
            "id": ent["entity_id"],
            "name": name,
            "race": race,
            "class": klass,
            "fields": fields,
            "ac": ac,
        }
        line = "  ".join(
            f"{ab[:3].upper()} {scores[ab]:>2}({bonus(scores[ab]):+d})" for ab in ABILITIES
        )
        print(f"{name:16} {race:9} {klass:11} HP {hp:>2}  AC {ac}  ATK {atk:+d}")
        print(f"                 {line}")

    print(
        json.dumps(
            {
                "tenant": SLUG,
                "ws": str(workspace_id),
                "chars": {
                    k: {"id": str(v["id"]), **{x: v[x] for x in ("name", "race", "class", "ac")}}
                    for k, v in characters.items()
                },
            },
            indent=0,
        )
    )
    # 4. THE ADVENTURE -- a directed session, GM + three players, three encounters.
    defs = await list_definitions(tenant_id, workspace_id=workspace_id)
    flow = next((d for d in defs if "open_discussion" in json.dumps(d.definition)), None)
    if flow is None:  # fall back to any conductable flow
        flow = next(
            d
            for d in defs
            if any(
                (ph.get("flags") or []).count("conductable")
                for ph in d.definition.get("phases", [])
            )
        )
    dsl, issues = validate_raw(flow.definition)
    assert dsl is not None and not issues, f"flow invalid: {issues}"

    play = uuid.uuid4()
    roster = [gm.id, *[players[k].id for k in players]]
    async with tenant_scope(tenant_id) as sdb:
        await sdb.execute(
            text(
                "INSERT INTO session (id, tenant_id, workspace_id, persona_id, current_phase,"
                " status, state, turn_policy, process_definition_id, next_event_seq)"
                " VALUES (:i,:t,:w,:p,:ph,'active','{}'::jsonb,'directed',:d,0)"
            ).bindparams(
                i=play, t=tenant_id, w=workspace_id, p=gm.id, ph=dsl.initial_phase, d=flow.id
            )
        )
        for pid in roster:
            await sdb.execute(
                text(
                    "INSERT INTO session_persona (id, tenant_id, session_id, persona_id)"
                    " VALUES (gen_random_uuid(), :t, :s, :p)"
                ).bindparams(t=tenant_id, s=play, p=pid)
            )
    # advance to the conductable discussion phase so directed turns run there
    conductable = next(
        (k for k, ph in dsl.phases.items() if "conductable" in (getattr(ph, "flags", []) or [])),
        dsl.initial_phase,
    )
    async with tenant_scope(tenant_id) as sdb:
        await sdb.execute(
            text("UPDATE session SET current_phase=:ph WHERE id=:i").bindparams(
                ph=conductable, i=play
            )
        )

    async def turn(persona_id):
        await run_directed_persona_turn(
            tenant_id,
            play,
            persona_id,
            dsl,
            model_provider_factory=get_model_provider,
            embedding_provider=get_embedding_provider(),
            encryptor=ENC,
            permission_service=PERM,
            mcp_transport=get_mcp_transport(),
            moderation_provider=get_moderation_provider(),
        )

    async def last_message():
        async with tenant_scope(tenant_id) as sdb:
            row = (
                await sdb.execute(
                    text(
                        "select coalesce(pe.name,'?')||': '||left(m.content_md,600) "
                        "from message m left join persona pe "
                        "on pe.principal_id=m.author_principal_id "
                        "where m.session_id=:s order by m.created_at desc limit 1"
                    ).bindparams(s=play)
                )
            ).scalar_one_or_none()
            return row or "(no message)"

    # agenda: set the premise into the session so the GM has something to open on
    premise = (
        "One-shot: The Bell That Does Not Ring. The village of Thornwick sent for help: "
        "its chapel bell has fallen silent and something is stirring in the crypt beneath it. "
        "Bram Oakhelm (dwarf fighter), Linnea Ash (elf magic-user) and Pip Underburrow "
        "(halfling thief) take the job. Run exactly THREE encounters and bring it to a close: "
        "(1) the approach -- a hazard on the road to Thornwick; (2) the crypt -- a guardian "
        "that must be fought; (3) the bell chamber -- the thing that silenced the bell. "
        "Open by setting the road scene and asking the party what they do."
    )
    async with tenant_scope(tenant_id) as sdb:
        await sdb.execute(
            text("UPDATE session SET agenda_md=:a WHERE id=:i").bindparams(a=premise, i=play)
        )

    print("\n=== THE ADVENTURE: The Bell That Does Not Ring ===")
    combat_events = 0
    # Three encounters, each a real BFRPG exchange that moves a character's health machine.
    # The GM narration for each is SCRIPTED (posted as the GM), so the monster the dice
    # resolve against is the same one the transcript introduced -- a free-narrating GM model
    # and a rigid dice script would tell two different stories and fight. The GM model still
    # runs the opening hook and the epilogue, where free narration belongs.
    encounters = [
        # key: monster, spotlight defender, its to-hit, its AC, damage die, scene, cue
        {
            "monster": "the Bog-Crawler",
            "defender": "bram",
            "mon_atk": 5,
            "dmg_die": "1d8",
            "scene": (
                "**The causeway.** The road to Thornwick narrows to a single file of old "
                "stones over black fen water, and the fog will not lift. Halfway across, "
                "the water on the left *heaves* — and a thing the size of a cart hauls "
                "itself up onto the stones ahead: a **Bog-Crawler**, shell slick with "
                "weed, mandibles working. It rears over {who}, closest in the lead."
            ),
            "cue": "{who}, it's rearing to strike you. What do you do?",
        },
        {
            "monster": "the Grave-Wight",
            "defender": "pip",
            "mon_atk": 6,
            "dmg_die": "1d6",
            "scene": (
                "**The crypt.** You win past the crawler and reach Thornwick by dusk. The "
                "chapel bell hangs dead and silent; a stair behind the altar drops into a "
                "cold crypt, and the singing the reeve's boy heard is louder here. As the "
                "party works at the crypt's inner gate, the lid of the nearest tomb grinds "
                "aside and a **Grave-Wight** rises — grey, withered, eyes two points of "
                "cold light. Its clawed hand sweeps toward {who}, nearest to the tombs."
            ),
            "cue": "{who}, its claw is coming for you. What do you do?",
        },
        {
            "monster": "the Bell-Horror",
            "defender": "bram",
            "mon_atk": 7,
            "dmg_die": "1d10",
            "scene": (
                "**The bell chamber.** Past the crypt a ladder climbs to the tower. There, "
                "the thing that silenced the bell unfolds from the rafters — a "
                "**Bell-Horror** of fused iron and bone, a cracked clapper for a fist. It "
                "fixes on Bram and swings that fist down at him."
            ),
            "cue": "{who}, the blow is coming down on you. What do you do?",
        },
    ]

    async def roll_total(sess_id, seq, ct, expr, fields, target):
        rec = await resolve(
            tenant_id=tenant_id,
            session_id=sess_id,
            event_seq=seq,
            tool_key="bfrpg_dice",
            actor_entity_id=None,
            expression=expr,
            check_type=ct,
            actor_fields=fields,
            target=target,
            rule_system=rs,
            rule_system_id=rs_id,
            legal_check_types=rs.check_types,
        )
        return rec

    async def gm_beat(text_md, record_ids):
        """Post one GM message narrating a resolved exchange, with the ResolutionRecord(s)
        attached -- so the roll renders inline in the transcript (the widget reads
        message.resolution_record_ids), exactly as a model-called dice tool would."""
        seq = await post_note(tenant_id, play, gm.principal_id, text_md)
        async with tenant_scope(tenant_id) as sdb:
            msg = (
                await sdb.execute(
                    select(MessageRow).where(
                        MessageRow.session_id == play, MessageRow.event_seq == seq
                    )
                )
            ).scalar_one()
            msg.resolution_record_ids = [str(r) for r in record_ids]

    # Resolutions live in their own high seq range so they never collide with the message
    # seqs that turns and GM beats consume.
    res_seq = 100000

    # The opening hook: one free GM model turn to set the road and the job.
    await turn(gm.id)
    print("GM open  ", (await last_message())[:420])

    for i, enc in enumerate(encounters, 1):
        monster, defkey = enc["monster"], enc["defender"]
        # Target a PC who is still standing -- a monster cannot menace someone already down.
        if int(characters[defkey]["fields"]["hit_points"]) <= 0:
            defkey = next(
                (k for k in characters if int(characters[k]["fields"]["hit_points"]) > 0),
                defkey,
            )
        defender = characters[defkey]
        who = defender["name"]
        print(f"\n--- Encounter {i}: {monster} (vs {who}) ---")

        # 1) SCRIPTED GM narration -- introduces exactly the monster the dice will resolve,
        # aimed at whoever is actually on their feet.
        scene = enc["scene"].format(who=who) + "\n\n" + enc["cue"].format(who=who)
        await post_note(tenant_id, play, gm.principal_id, scene)
        print("GM       ", enc["scene"].format(who=who)[:200])

        # 2) the spotlight player reacts, in first person (real model turn)
        await turn(players[defkey].id)
        print(f"{defkey:9}", (await last_message())[:300])

        # 3) the monster's attack roll vs the PC's own Armour Class
        atk = await roll_total(
            play, res_seq, "attack_roll", "1d20",
            {"attack_bonus": enc["mon_atk"], "strength": 10}, defender["ac"],
        )
        res_seq += 1
        hit = atk.outcome == "success"
        print(
            f"  DICE  {monster} attack {atk.expression} +{enc['mon_atk']} vs "
            f"AC {defender['ac']}: total={atk.total} -> {'HIT' if hit else 'miss'}"
        )
        if not hit:
            await gm_beat(
                f"{monster} strikes at {defender['name']} (AC {defender['ac']}) — the "
                f"dice come up **{atk.total}**: a **miss**. {defender['name']} twists "
                f"clear, untouched.",
                [atk.id],
            )
            combat_events += 1
            continue

        # 4) it hit: roll damage, lower the PC's hit points, drive the health machine
        cur_hp = int(defender["fields"]["hit_points"])
        dmg_rec = await roll_total(play, res_seq, "constitution_check", enc["dmg_die"], {}, 1)
        dmg = int(dmg_rec.total)
        new_hp = max(0, cur_hp - dmg)
        defender["fields"]["hit_points"] = new_hp
        res = await resolve_and_apply(
            tenant_id=tenant_id, workspace_id=workspace_id, principal_id=gm.principal_id,
            session_id=play, event_seq=res_seq + 1, tool_key="bfrpg_dice",
            actor_entity_id=defender["id"], expression="1d4", check_type="constitution_check",
            machine_key="health", trigger="damage_taken", set_fields={"hit_points": new_hp},
            rule_system=rs, rule_system_id=rs_id, legal_check_types=rs.check_types,
            permission_service=PERM, actor_fields={}, target=1,
        )
        res_seq += 2
        state = res["new_state"]
        fell = {
            "wounded": "reels, wounded but standing",
            "unconscious": "crumples, unconscious",
            "dead": "falls, and does not rise",
        }.get(state, f"is now {state}")
        await gm_beat(
            f"{monster} lands the blow on {defender['name']} — rolled **{atk.total}** vs "
            f"AC {defender['ac']}, a **hit** for **{dmg}** damage ({enc['dmg_die']}). "
            f"{defender['name']} drops from {cur_hp} to **{new_hp}** hit points and "
            f"**{fell}**. Health track: *{state}*.",
            [atk.id, dmg_rec.id],
        )
        print(
            f"  DICE  {enc['dmg_die']} damage = {dmg}; {defender['name']} "
            f"HP {cur_hp} -> {new_hp}; health machine -> {state}"
        )
        combat_events += 1

    # 5. the GM closes the one-shot -- scripted from the real final state, so it can't
    # contradict the dice (a free GM turn here kept trying to call tools and retcon).
    standing = [v["name"] for v in characters.values() if int(v["fields"]["hit_points"]) > 0]
    fallen = [v["name"] for v in characters.values() if int(v["fields"]["hit_points"]) <= 0]
    if standing:
        close = (
            f"The bell's iron throat splits and the last echo dies in the fen. When the "
            f"fog thins, **{', '.join(standing)}** still stand"
            + (f", and drag **{', '.join(fallen)}** back into the light" if fallen else "")
            + ". Thornwick's bell will ring true at dawn. The one-shot ends here."
        )
    else:
        close = (
            "The bell tolls on into the dark with no one left to answer it. The party of "
            "three does not come back out of Thornwick. The one-shot ends here — a grim one."
        )
    await post_note(tenant_id, play, gm.principal_id, close)
    print("\n=== EPILOGUE ===")
    print("GM       ", close[:400])

    # 6. what the record shows
    async with tenant_scope(tenant_id) as sdb:
        n_chars = (
            await sdb.execute(
                text(
                    "select count(*) from entity e join entity_schema s on s.id=e.schema_id "
                    "where s.key='bfrpg_character'"
                )
            )
        ).scalar_one()
        n_res = (
            await sdb.execute(
                text(
                    "select count(*) from resolution_record r "
                    "join rule_system rs on rs.id=r.rule_system_id "
                    "where rs.key='basic_fantasy'"
                )
            )
        ).scalar_one()
        n_msg = (
            await sdb.execute(
                text("select count(*) from message where session_id=:s").bindparams(s=play)
            )
        ).scalar_one()
        transitions = (
            await sdb.execute(
                text("select count(*) from entity_state_change where session_id=:s").bindparams(
                    s=play
                )
            )
        ).scalar_one()
        states = (
            (
                await sdb.execute(
                    text(
                        "select e.name || ': ' || "
                        "coalesce(e.fsm_states->>'health','healthy') from entity e "
                        "join entity_schema s on s.id=e.schema_id where s.key='bfrpg_character' "
                        "order by e.name"
                    )
                )
            )
            .scalars()
            .all()
        )
    print(
        f"\nRECORD: {n_chars} characters, {n_res} basic_fantasy resolution records, "
        f"{n_msg} narrated turns, {transitions} state-machine transitions, "
        f"{combat_events} encounters resolved."
    )
    print("FINAL HEALTH:", "; ".join(states))
    print(f"LOGIN: org={SLUG}  email={email_addr}  password={password}")
    base = "http://pyrrhula.localhost"
    print("CHARACTER SHEETS (health state machine shown on each):")
    for v in characters.values():
        print(f"  {v['name']:16} {base}/workspaces/{workspace_id}/entities/{v['id']}")


asyncio.run(main())
