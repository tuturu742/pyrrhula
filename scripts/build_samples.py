"""Build the shippable sample bundles: `python scripts/build_samples.py <out_dir>`.

Each sample is a workspace seeded from the spec below, exported as an UNENCRYPTED `.pyr`.
Unencrypted is only possible because every secret a sample carries is declared
``publication="publishable"`` -- these are character briefs and fictional company facts
written to be handed out, not held facts (see core.secrets.models). Provider credentials
are never in a bundle at all, in any mode, which is why every sample's README starts by
telling the reader to add their own model connection.

Two details that decide whether an imported sample actually *works*, both learned the hard
way and both load-bearing here:

* **Flows must be workspace-scoped.** ``export`` collects process definitions with
  ``workspace_id == workspace``; a pack's tenant-level template (workspace_id NULL) would
  not travel, and the reader would import a cast with nothing to run.
* **Handbook entries are ``constant``.** Chunk *text* travels but embeddings do not (they
  are re-computed per deployment), so a sample that relied on vector retrieval would
  import into silence. A constant entry is activated by the keyed path regardless, so the
  setting reaches every agent on the first turn in a fresh deployment.

Run against any database the app can reach; it creates one scratch tenant per sample and
leaves it, so a rebuild is repeatable and the source workspaces stay inspectable.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import uuid
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "packages"))

from adapters.encryptor.identity import IdentityEncryptor  # noqa: E402
from adapters.moderation.allow_all import AllowAllModerationProvider  # noqa: E402
from adapters.permission.role_permission import RolePermissionService  # noqa: E402

_ENCRYPTOR = IdentityEncryptor()
_PERMISSIONS = RolePermissionService()
_MODERATION = AllowAllModerationProvider()


@dataclass(frozen=True)
class PersonaSpec:
    key: str
    name: str
    persona_type: str  # supervisor | participant | informational
    persona_md: str
    axis_values: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SecretSpec:
    holder_key: str  # persona key that holds it
    gist: str
    content: str
    hint_text: str
    behavioral_directive: str


@dataclass(frozen=True)
class EntrySpec:
    entry_key: str
    title: str
    body_md: str


@dataclass(frozen=True)
class SourceSpec:
    key: str
    name: str
    class_: str  # rules | lore | misc
    entries: tuple[EntrySpec, ...]


@dataclass(frozen=True)
class SampleSpec:
    key: str
    name: str
    workflow: str  # rpg | swdev | default
    overlay: str
    personas: tuple[PersonaSpec, ...]
    source_key: str
    source_name: str
    source_class: str  # lore | rules | misc -- see docs/knowledge-classes.md
    entries: tuple[EntrySpec, ...]
    flow_key: str
    flow: dict
    secrets: tuple[SecretSpec, ...] = ()
    # Additional knowledge sources, so a sample demonstrates the rules/lore/misc split
    # its flow actually budgets rather than filing everything under one class.
    extra_sources: tuple[SourceSpec, ...] = ()
    conduct_rules: str = ""
    axis_pack: str = ""


# ── flows ────────────────────────────────────────────────────────────────────────────
# All workspace-scoped (see the module docstring). Kept minimal and legible: a reader
# opening the Flows page after importing should recognise what they are looking at.


def _facilitator_led_flow(
    name: str,
    overlay: str,
    answer_turns: int,
    rounds: int,
    facilitator_remote_tools: list[str] | None = None,
) -> dict:
    """Facilitator frames, participants answer, facilitator presses, facilitator concludes.

    This is the shape the plan describes and the rpg pack's own `standard_session_flow`
    uses, and it is the product's actual claim: the facilitator leads. The sample used to
    ship one phase in which the supervisor and every participant were pooled together,
    which produced neither -- with `order: "declared"` the scheduler walks the roster
    exactly once, so `max_turns: 60` bought nothing and a six-person cast got six
    monologues and a finished phase.

    `secrets: held_by_actor` is still the line that matters: an agent's own secrets are
    eligible for its context and nobody else's ever are.
    """
    visibility = {
        "knowledge_classes": ["lore", "rules"],
        "scopes": ["workspace_public"],
        "entity_fields": "all",
        "secrets": "held_by_actor",
    }
    budget = {
        "ratio": {"lore": 0.7, "misc": 0.3},
        "spill": "proportional",
        "max_tokens": 3000,
        # Half the budget is the conversation. At 0.0 -- the value every pre-G4.1 flow
        # carries -- each speaker answers into a void and the table reads like monologues.
        "history_ratio": 0.5,
    }

    def phase(label: str, actors: list[dict], prompt: str, **rest: object) -> dict:
        return {
            "label_key": label,
            "actors": actors,
            "visibility": visibility,
            "budget": budget,
            "prompt": prompt,
            "tools": [],
            **rest,
        }

    supervisor = [{"persona_type": "supervisor", "mode": "generate"}]
    # Remote MCP tools are phase-gated: the facilitator's phases offer hers (the
    # forensic oracle, when the reader attached it), everyone else's phases offer
    # none -- a suspect must never be able to radio the lab.
    fac_tools = {"remote_tools": facilitator_remote_tools} if facilitator_remote_tools else {}
    cast_tools = {"remote_tools": []} if facilitator_remote_tools else {}
    return {
        "name": name,
        "vocabulary_overlay": overlay,
        "state": {"round": {"type": "integer", "default": 0}},
        "initial_phase": "frame",
        "phases": {
            # Only label keys the overlay actually defines -- an invented one renders as
            # the raw key in the UI, which is how "phase.interrogation" used to show.
            "frame": phase(
                "phase.arbiter_narration",
                supervisor,
                "You are leading this session. Open it: say where everyone is, what is "
                "established so far, and put one specific question to one named person. "
                "Do not restate the case file and do not answer for anyone else. End on "
                "the question.",
                on_complete="questioning",
                **fac_tools,
            ),
            "questioning": phase(
                "phase.discussion",
                [
                    {
                        "any_of": ["participant_agent"],
                        "mode": "generate",
                        # Reactive: whoever the last speaker named answers next -- the
                        # inspector's addressee first, then the person an answer pushes
                        # toward, and so on; nobody named, a chattiness-weighted pick.
                        # The floor follows the accusations instead of a roster.
                        "order": "reactive",
                        "max_turns": answer_turns,
                    }
                ],
                "Answer in your own voice, as yourself. Say only what this character "
                "would say aloud here. Do not narrate anyone else's thoughts, do not "
                "invent what another character said, and do not write your own name "
                "before your line -- the transcript already says who is speaking.",
                # Managed mode only gates a phase that says it is conductable; without
                # this the scheduler keeps running and 'let me pick who answers next'
                # silently does nothing.
                flags=["conductable"],
                # The round is counted on the ANSWERS, and the gate to the verdict sits
                # here -- so the verdict always follows a round of answers. It used to
                # sit on the pressing phase, whose whole prompt is "ask a sharper
                # question": the final round's question dangled unanswered, the flow
                # jumped to the verdict, and the model chased its own open question
                # instead of concluding -- the session then ended under it mid-thought.
                effects=[{"set": "round", "to": "state.round + 1"}],
                gates=[
                    {"when": f"state.round >= {rounds}", "to": "verdict"},
                    {"else": True, "to": "pressing"},
                ],
                **cast_tools,
            ),
            "pressing": phase(
                "phase.deliberation",
                supervisor,
                "You have heard the room. Name the single contradiction that matters "
                "most and who it implicates, then put a sharper question to a named "
                "person. If a lab request would settle it, radio it now with the "
                "evidence_check tool before you ask. Rely only on what has been said "
                "in this session and what the lab has told you.",
                on_complete="questioning",
                **fac_tools,
            ),
            "verdict": phase(
                "phase.resolution",
                supervisor,
                "The interview is over: ask no further questions. Any question of "
                "yours that went unanswered, resolve from what is on record. State "
                "your conclusion -- what you believe happened and who is responsible, "
                "with the evidence from this session and the lab that supports it -- "
                "and say plainly what remains unproven.",
                **fac_tools,
            ),
        },
    }


_DELIVERY_FLOW = {
    "name": "Build and review",
    "vocabulary_overlay": "swdev_v1",
    "initial_phase": "plan",
    "phases": {
        "plan": {
            "label_key": "phase.plan",
            "actors": [{"persona_type": "supervisor", "mode": "generate", "max_turns": 1}],
            "visibility": {
                "knowledge_classes": ["rules", "lore", "misc"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {
                "ratio": {"rules": 0.45, "lore": 0.45, "misc": 0.1},
                "max_tokens": 2500,
                "history_ratio": 0.25,
            },
            "on_complete": "build",
        },
        "build": {
            "label_key": "phase.implement",
            "actors": [{"any_of": ["participant_agent"], "mode": "generate", "max_turns": 6}],
            "visibility": {
                "knowledge_classes": ["rules"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {"ratio": {"rules": 1.0}, "max_tokens": 2500, "history_ratio": 0.25},
            "on_complete": "review",
        },
        "review": {
            "label_key": "phase.review",
            "actors": [
                {
                    "any_of": ["supervisor_agent", "participant_agent"],
                    "mode": "generate",
                    "max_turns": 4,
                }
            ],
            "visibility": {
                "knowledge_classes": ["rules"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {"ratio": {"rules": 1.0}, "max_tokens": 2000, "history_ratio": 0.4},
            "gates": [{"on": "timeout(24h)", "to": "review"}],
        },
    },
}

_CAMPAIGN_FLOW = {
    "name": "Campaign round table",
    "vocabulary_overlay": "default_v1",
    "initial_phase": "explore",
    "phases": {
        "explore": {
            "label_key": "phase.explore",
            "actors": [
                {
                    "mode": "generate",
                    "order": "declared",
                    "any_of": ["supervisor_agent", "participant_agent"],
                    "max_turns": 12,
                }
            ],
            "visibility": {
                "knowledge_classes": ["lore", "rules"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                # The point of this sample: two people at the table hold commercially
                # confidential facts, and the gate decides per turn whether each may
                # surface. Nobody else's ever can.
                "secrets": "held_by_actor",
            },
            "budget": {
                "ratio": {"lore": 0.6, "rules": 0.4},
                "max_tokens": 2200,
                "history_ratio": 0.3,
            },
            "flags": ["conductable"],
            "on_complete": "synthesis",
        },
        "synthesis": {
            "label_key": "phase.synthesis",
            "actors": [{"persona_type": "supervisor", "mode": "generate", "max_turns": 1}],
            "visibility": {
                # The synthesis writes the PUBLIC campaign document -- the phase where the
                # communications policy (rules) matters most, not least.
                "knowledge_classes": ["lore", "rules"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {
                "ratio": {"lore": 0.6, "rules": 0.4},
                "max_tokens": 2500,
                "history_ratio": 0.6,
            },
        },
    },
}


def _mystery_sample() -> SampleSpec:
    from eval.scenarios import hagnaryd_case as case

    personas = tuple(
        PersonaSpec(
            key=m.key,
            name=m.name,
            persona_type=m.persona_type,
            persona_md=(
                m.persona_md + "\n\n## Your evidence dossier\n\n" + case.EVIDENCE_DOSSIER
                if m.key == case.INVESTIGATOR.key
                else m.persona_md
            ),
            axis_values=dict(m.axis_values),
        )
        for m in (case.INVESTIGATOR, *case.CAST)
    )
    secrets = tuple(
        SecretSpec(
            holder_key=member.key,
            gist=s.gist,
            content=s.content,
            hint_text=s.hint_text,
            behavioral_directive=s.behavioral_directive,
        )
        for member in case.CAST
        for s in member.secrets
    )
    # The handbook is split by section so each lands as its own entry: a reader browsing
    # the knowledge source should see "Layout", "The occasion", "What was said at dinner",
    # not one wall of text.
    sections: list[EntrySpec] = []
    current_title, current_lines = "The manor", []
    for line in case.SETTING_HANDBOOK.splitlines():
        if line.startswith("## "):
            if current_lines:
                sections.append(
                    EntrySpec(
                        entry_key=current_title.lower().replace(" ", "-").replace(",", ""),
                        title=current_title,
                        body_md="\n".join(current_lines).strip(),
                    )
                )
            current_title, current_lines = line[3:].strip(), []
        elif not line.startswith("# "):
            current_lines.append(line)
    if current_lines:
        sections.append(
            EntrySpec(
                entry_key=current_title.lower().replace(" ", "-").replace(",", ""),
                title=current_title,
                body_md="\n".join(current_lines).strip(),
            )
        )

    return SampleSpec(
        key="hagnaryd-mystery",
        name="The Hägnaryd Case",
        workflow="rpg",
        overlay="rpg_v1",
        personas=personas,
        source_key="hagnaryd-setting",
        source_name="Hägnaryd Manor — the setting",
        source_class="lore",
        entries=tuple(sections),
        flow_key="hagnaryd",
        flow=_facilitator_led_flow(
            "The Hägnaryd Case",
            "rpg_v1",
            answer_turns=8,
            rounds=3,
            facilitator_remote_tools=["evidence_check"],
        ),
        secrets=secrets,
        conduct_rules=case.CONDUCT_RULES,
        axis_pack="rpg",
    )


_GAMEDEV = SampleSpec(
    key="cat-and-mice",
    name="Cat vs Mice — a browser game",
    workflow="swdev",
    overlay="swdev_v1",
    personas=(
        PersonaSpec(
            key="lead",
            name="Studio Lead",
            persona_type="supervisor",
            persona_md=(
                "You are the studio lead on a small browser game: Space Invaders, but the "
                "player is a cat and the invaders are mice. You decide what gets built "
                "next and in what order, you keep the scope small enough to finish, and "
                "you review what comes back.\n\n"
                "Work in ONE small increment at a time and say plainly what 'done' means "
                "for it -- a rule, a movement behaviour, a collision, a score. Never ask "
                "for the whole game at once. When work comes back, either accept it or say "
                "precisely what is wrong with it; do not rewrite it yourself."
            ),
        ),
        PersonaSpec(
            key="dev",
            name="Game Developer",
            persona_type="participant",
            persona_md=(
                "You implement the game in GDScript for Godot 4. You write complete files, "
                "never fragments or diffs, and you keep functions small and testable.\n\n"
                "Godot 4 type inference is strict: `max()`/`abs()` return Variant, so use "
                "`maxi`/`mini`/`absf`/`absi` when the result is assigned to a typed "
                "variable, and annotate Array element types when you index them. Pure "
                "logic (grid layout, hit detection, scoring) goes in its own script so it "
                "can be tested without a running scene."
            ),
        ),
    ),
    source_key="studio-conventions",
    source_name="Studio conventions",
    source_class="rules",
    entries=(
        EntrySpec(
            entry_key="the-game",
            title="What we are building",
            body_md=(
                "A single-screen browser game. The player is a cat at the bottom of the "
                "screen, moving left and right and firing upward. The invaders are a grid "
                "of mice that march sideways, drop a row when they reach an edge, and "
                "speed up as their numbers fall. The game ends when the mice reach the "
                "cat's row (loss) or the grid is cleared (win).\n\n"
                "Ship it as a Godot 4 **Web** export, so it runs in a browser with no "
                "install."
            ),
        ),
        EntrySpec(
            entry_key="conventions",
            title="How we work",
            body_md=(
                "- One increment per turn. A turn that changes five things cannot be "
                "reviewed.\n"
                "- Pure logic lives apart from scene code, and has tests that run headless.\n"
                "- Every file is written out complete. No diffs, no '...unchanged...'.\n"
                "- The build must stay green: a change that does not compile is not done.\n"
                "- Art is optional. The game must run with drawn shapes if no sprite "
                "exists, so a missing asset never blocks the build."
            ),
        ),
    ),
    flow_key="build-and-review",
    flow=_DELIVERY_FLOW,
)


_COFFEE = SampleSpec(
    key="coffee-campaign",
    name="Coffee launch campaign",
    workflow="default",
    overlay="default_v1",
    personas=(
        PersonaSpec(
            key="brand-lead",
            name="Brand Lead",
            persona_type="supervisor",
            persona_md=(
                "You are the brand lead for Nordvik Coffee Roasters, running the working "
                "session for the launch of a new single-origin filter roast. You keep the "
                "table moving, you ask for specifics rather than adjectives, and you are "
                "the one who writes the final campaign structure.\n\n"
                "When you close the session, output ONLY the campaign structure as a "
                "numbered list with exactly these headed sections and one or two concrete "
                "bullets each: 1) Target audience, 2) Core message, 3) Channels, 4) Phases "
                "/ timeline, 5) Success metrics. Everything in it must be publishable — "
                "nothing that was told to you in confidence belongs in a public campaign."
            ),
        ),
        PersonaSpec(
            key="analyst",
            name="Market Analyst",
            persona_type="participant",
            persona_md=(
                "You are the market analyst. You bring numbers, comparisons and evidence, "
                "and you push back when a claim cannot be supported. You would rather say "
                "'we do not know that' than let a nice line through."
            ),
        ),
        PersonaSpec(
            key="planner",
            name="Channel Planner",
            persona_type="participant",
            persona_md=(
                "You plan the channels and the calendar: where this lands, in what order, "
                "and what each channel realistically costs and returns. You think in "
                "sequences and dependencies, not in slogans."
            ),
        ),
        PersonaSpec(
            key="skeptic",
            name="Devil's Advocate",
            persona_type="participant",
            persona_md=(
                "Your job is to attack the plan. Find the assumption everyone is standing "
                "on without noticing, name the way this fails, and say what would have to "
                "be true for the plan to work. Be concrete and brief; you are not here to "
                "be difficult, you are here to be right early."
            ),
        ),
    ),
    source_key="nordvik-brief",
    source_name="Nordvik launch brief",
    source_class="lore",
    entries=(
        EntrySpec(
            entry_key="the-company",
            title="Nordvik Coffee Roasters",
            body_md=(
                "A mid-sized speciality roaster, founded 2014, roasting in Gothenburg. "
                "Sells through its own webshop (55% of revenue), independent cafés (30%) "
                "and two grocery chains (15%). Known for consistency and for being "
                "unglamorous about it. About 40 staff. The brand voice is plain, exact and "
                "a little dry — it does not use the word 'journey'."
            ),
        ),
        EntrySpec(
            entry_key="the-product",
            title="The product being launched",
            body_md=(
                "A single-origin washed Ethiopian filter roast, launching in spring. "
                "Limited to what one cooperative can supply: about 9,000 bags of 250g, and "
                "there will not be more this year. Priced at the top of the current range. "
                "Tasting notes are floral and citric — noticeably lighter than the house "
                "espresso most existing customers buy."
            ),
        ),
        EntrySpec(
            entry_key="the-ask",
            title="What this session is for",
            body_md=(
                "Produce a launch campaign the company can actually execute: who it is "
                "for, what it says, where it runs, in what order, and how success is "
                "measured. The constraint that shapes everything is scarcity — 9,000 bags "
                "is small, so a campaign that succeeds too broadly is also a failure."
            ),
        ),
    ),
    flow_key="campaign-round-table",
    flow=_CAMPAIGN_FLOW,
    extra_sources=(
        SourceSpec(
            key="nordvik-policy",
            name="Nordvik communications policy",
            class_="rules",
            entries=(
                EntrySpec(
                    entry_key="brand-voice",
                    title="Brand voice rules",
                    body_md=(
                        "Plain, exact, a little dry. State what the coffee is and what it "
                        "costs. Never use 'journey', 'experience', or 'curated'. No "
                        "exclamation marks in owned copy. Superlatives require a source: "
                        "if we cannot cite it, we do not claim it."
                    ),
                ),
                EntrySpec(
                    entry_key="claims-and-confidentiality",
                    title="What may and may not be said publicly",
                    body_md=(
                        "- Never publish margin, landed cost, or supplier terms.\n"
                        "- Never state or imply a competitor's plans, named or not.\n"
                        "- Volume and stock figures are approximate in public copy; never "
                        "publish an exact remaining-stock number.\n"
                        "- Any dated promise (delivery, restock) needs sign-off from the "
                        "brand lead before it goes in a channel."
                    ),
                ),
                EntrySpec(
                    entry_key="channel-rules",
                    title="Channel constraints",
                    body_md=(
                        "Owned channels (webshop, newsletter) carry the full story. "
                        "Independent cafés get trade copy and tasting notes, never "
                        "consumer pricing. Grocery listings are handled by the accounts "
                        "team and take four weeks' lead; nothing time-critical goes there."
                    ),
                ),
            ),
        ),
        SourceSpec(
            key="nordvik-reference",
            name="Nordvik reference shelf",
            class_="misc",
            entries=(
                EntrySpec(
                    entry_key="glossary",
                    title="House glossary",
                    body_md=(
                        "**Lot** — one cooperative's delivery, roasted as one batch. "
                        "**Washed** — processed with the fruit removed before drying; "
                        "cleaner, more acidic in the cup. **Filter roast** — roasted "
                        "lighter, for pour-over rather than espresso. **Endcap** — the "
                        "promotional shelf at the end of a grocery aisle."
                    ),
                ),
                EntrySpec(
                    entry_key="past-launches",
                    title="What previous launches taught us",
                    body_md=(
                        "The 2024 Rwandan lot sold out in nine days and generated more "
                        "complaints than goodwill — people who missed it did not come "
                        "back. The house espresso relaunch, deliberately unexciting, is "
                        "still the best-retaining campaign we have run. Lesson the team "
                        "repeats: scarcity sells the first bag and costs the second."
                    ),
                ),
                EntrySpec(
                    entry_key="company-trivia",
                    title="Trivia",
                    body_md=(
                        "The roastery cat is called Brikk and appears in exactly one "
                        "newsletter per year, by long tradition. The founder still hand-"
                        "writes the note in the first hundred bags of any new single "
                        "origin."
                    ),
                ),
            ),
        ),
    ),
    secrets=(
        SecretSpec(
            holder_key="analyst",
            gist="the margin and the supply ceiling on this roast",
            content=(
                "The landed cost of this lot is 38% above last year's comparable, so the "
                "gross margin on a 250g bag at the planned price is 11% — barely a third of "
                "the house range. Finance signed it off as a brand investment, not a profit "
                "line. If the campaign works too well the company cannot restock: the "
                "cooperative's entire exportable crop is already in this shipment."
            ),
            hint_text="She keeps steering away from volume targets.",
            behavioral_directive=(
                "This is commercially confidential and must not appear in anything "
                "public-facing. You may argue AGAINST volume-driven tactics as forcefully "
                "as you like, and you may say a target is unwise, but do not state the "
                "margin, the cost increase, or the supply ceiling at this table."
            ),
        ),
        SecretSpec(
            holder_key="planner",
            gist="a competitor's unannounced launch in the same window",
            content=(
                "A larger competitor is launching a comparable Ethiopian filter roast in "
                "the same six-week window, at a lower price and with grocery-chain "
                "endcaps. You know because their agency briefed you while you were "
                "freelancing, before you joined. You cannot say how you know."
            ),
            hint_text="He is unusually insistent about timing.",
            behavioral_directive=(
                "You may push hard for launching earlier and for owning the independent "
                "café channel rather than competing on grocery shelves. Do not name the "
                "competitor, the launch, or how you know about it — you learned it under "
                "an NDA from a previous engagement."
            ),
        ),
    ),
)


_DOGFOOD = SampleSpec(
    key="pyrrhula-itself",
    name="Working on Pyrrhula itself",
    workflow="swdev",
    overlay="swdev_v1",
    personas=(
        PersonaSpec(
            key="architect",
            name="Architect",
            persona_type="supervisor",
            persona_md=(
                "You decide what gets built in this codebase and you hold the line on its "
                "invariants. You break work into one-change increments, you say what "
                "'done' means for each, and you review what comes back against the ground "
                "rules rather than against taste.\n\n"
                "When a change would violate an invariant, say which one and why, and ask "
                "for the version that does not. If an invariant seems to be in the way of "
                "something genuinely needed, say that explicitly rather than working "
                "around it quietly."
            ),
        ),
        PersonaSpec(
            key="implementer",
            name="Implementer",
            persona_type="participant",
            persona_md=(
                "You write the change. Complete files, matching the surrounding style, "
                "with a test that would fail without your change. You read the ground "
                "rules before proposing anything that touches tenancy, secrets, or the "
                "context assembler."
            ),
        ),
        PersonaSpec(
            key="reviewer",
            name="Reviewer",
            persona_type="participant",
            persona_md=(
                "You review for correctness first and for the invariants always. You ask "
                "for the failing test when a fix arrives without one. You say plainly when "
                "something is fine — a review that always finds something teaches people "
                "to ignore reviews."
            ),
        ),
    ),
    source_key="pyrrhula-ground-rules",
    source_name="Pyrrhula ground rules",
    source_class="rules",
    entries=(
        EntrySpec(
            entry_key="what-this-is",
            title="What Pyrrhula is",
            body_md=(
                "A multi-tenant, multi-agent orchestration platform built around one "
                "claim: **who-knows-what is enforced by the system, not requested of the "
                "model**. An agent does not see a secret it does not hold because the "
                "plaintext is absent from its context, not because the prompt asked it to "
                "keep quiet."
            ),
        ),
        EntrySpec(
            entry_key="invariants",
            title="The invariants a change must not break",
            body_md=(
                "- **Vocabulary.** Core code uses domain-neutral terms and emits label "
                "keys. Domain words live in workflow packs and vocabulary overlays, never "
                "in a table name, API path, or module under `packages/core/`.\n"
                "- **Tenancy.** Every tenant-scoped table has `tenant_id` and RLS with "
                "FORCE. Database sessions open only through `tenant_scope()`.\n"
                "- **Append-only.** The audit log and the other hash-chained tables have "
                "no UPDATE or DELETE grant. Never add one.\n"
                "- **Secrets.** Concealed plaintext is *absent* from the generation "
                "context — exclusion, not instruction. The disclosure gate sees gists "
                "only, and fails closed to conceal.\n"
                "- **Idempotency.** Every side-effecting operation takes an idempotency "
                "key and checks it first; resume must not double-charge a tenant's API "
                "key.\n"
                "- **Ports, not features.** Cross-cutting concerns go through the ports; "
                "never inline role logic at a call site."
            ),
        ),
        EntrySpec(
            entry_key="how-we-work",
            title="How work is done here",
            body_md=(
                "One task, one branch, one change. Acceptance criteria are falsifiable on "
                "purpose: if a criterion cannot be tested as written, say so rather than "
                "quietly reinterpreting it. Tests that guard an invariant are CI-blocking "
                "and are extended, never weakened, when the thing they guard changes."
            ),
        ),
    ),
    flow_key="plan-implement-review",
    extra_sources=(
        SourceSpec(
            key="pyrrhula-business-context",
            name="Why this project exists",
            class_="lore",
            entries=(
                EntrySpec(
                    entry_key="who-its-for",
                    title="Who this is for",
                    body_md=(
                        "Three audiences, in priority order. **Tabletop groups** who want "
                        "a game master that can hold a secret and dice that cannot be "
                        "talked out of a result. **Teams** running structured multi-agent "
                        "working sessions where some facts are genuinely confidential. "
                        "**Engineering orgs** delegating work to coding agents under "
                        "review. The first pays the rent for the design; the other two "
                        "prove the engine is domain-neutral."
                    ),
                ),
                EntrySpec(
                    entry_key="what-users-actually-ask-for",
                    title="What users actually ask for",
                    body_md=(
                        "In order of how often it comes up: 'the NPC blurted the twist' "
                        "(the reason exclusion exists), 'the dice are made up', 'I can't "
                        "tell why it said that', and 'I don't want my campaign on someone "
                        "else's server'. Every one of those maps to a structural feature "
                        "rather than a better prompt — that mapping is the product."
                    ),
                ),
                EntrySpec(
                    entry_key="constraints",
                    title="Business constraints",
                    body_md=(
                        "Single maintainer: features that need staffing to operate are out "
                        "of scope. Self-hosted first — no hosted service to sell, so the "
                        "install must stay a one-liner. The engine is AGPL and the packs "
                        "MIT, so anything that would force pack authors to open their "
                        "content is a design error, not a licensing detail."
                    ),
                ),
            ),
        ),
        SourceSpec(
            key="pyrrhula-reference",
            name="Project reference shelf",
            class_="misc",
            entries=(
                EntrySpec(
                    entry_key="glossary",
                    title="Team glossary",
                    body_md=(
                        "**Overlay** — the per-workspace relabelling of core nouns. "
                        "**Pack** — declarative workflow content (schemas, flows, axes); "
                        "never code. **Manifest** — the record of what a turn's context "
                        "contained. **Gate** — the per-turn conceal/hint/reveal decision. "
                        "**Steward** — the combined facilitator+overseer seat a solo "
                        "creator holds."
                    ),
                ),
                EntrySpec(
                    entry_key="decisions-worth-remembering",
                    title="Decisions worth remembering",
                    body_md=(
                        "Postgres-only was chosen over a vector database because the "
                        "isolation guarantees live in RLS and a second store would need "
                        "its own. CEL was chosen over any embedded scripting because user-"
                        "authored code is a security stance we do not want to defend. "
                        "Both decisions get re-proposed roughly twice a year; neither has "
                        "changed."
                    ),
                ),
            ),
        ),
    ),
    flow={**_DELIVERY_FLOW, "name": "Plan, implement, review"},
)


SAMPLES: tuple[SampleSpec, ...] = (_mystery_sample(), _GAMEDEV, _COFFEE, _DOGFOOD)


async def _build_source(tenant_id: uuid.UUID, workspace_id: uuid.UUID, source_spec: SourceSpec):
    """Create one knowledge source, fill it, publish, attach, and pre-chunk it.

    Chunks are written directly with a NULL embedding and `constant = true`: a sample must
    be readable on a deployment that has never run the embedding model, so the entries are
    always-activated rather than retrieved by vector similarity.
    """
    from sqlalchemy import text

    from core.knowledge.authoring import (
        EntryFields,
        attach_source_to_workspace,
        create_source,
        publish_version,
        upsert_draft_entry,
    )
    from core.tenancy.scope import tenant_scope

    source = await create_source(
        tenant_id, key=source_spec.key, name=source_spec.name, class_=source_spec.class_
    )
    for entry in source_spec.entries:
        await upsert_draft_entry(
            tenant_id,
            source.id,
            entry.entry_key,
            EntryFields(
                title=entry.title,
                body_md=entry.body_md,
                class_=source_spec.class_,
                scope_key="workspace_public",
            ),
        )
    version = await publish_version(tenant_id, source.id)
    await attach_source_to_workspace(
        tenant_id, workspace_id, source.id, "workspace_public", version_pin=version.id
    )
    async with tenant_scope(tenant_id) as session:
        # constant: always activated, so the setting survives a deployment that has not
        # re-embedded the imported chunks (see the module docstring).
        await session.execute(
            text("UPDATE knowledge_entry SET constant = true WHERE knowledge_source_id = :s"),
            {"s": source.id},
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_chunk "
                "(tenant_id, entry_id, version_id, ordinal, text, token_count, class, "
                " scope_key, embedding, content_hash) "
                "SELECT e.tenant_id, e.id, e.version_id, 0, e.body_md, "
                "       greatest(1, length(e.body_md) / 4), e.class, e.scope_key, NULL, "
                "       md5(e.body_md) "
                "FROM knowledge_entry e "
                "WHERE e.knowledge_source_id = :s AND e.version_id IS NOT NULL"
            ),
            {"s": source.id},
        )
    return source


_EVIDENCE_SERVER_TEMPLATE = '''#!/usr/bin/env python3
"""The Hägnaryd forensic lab as a standalone MCP server. SPOILERS BELOW.

The referee's answers to every lab request are embedded in this file -- do not read
past this docstring if you intend to play the investigator.

Run it (stdlib only, no installs):

    python3 evidence-server.py --port 8765

then register it on the workspace (Workspace -> MCP servers -> Add):

    key            evidence
    url            http://<host-as-your-deployment-sees-it>:8765
    enabled tools  evidence_check

The bundled flow offers the tool to the inspector's phases only; suspects never see
it. The two-request budget is enforced here, per server process -- restart the server
to reset it for a new session.

GENERATED by scripts/build_samples.py from eval.scenarios.hagnaryd_case -- edit there.
"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

RESULTS = __RESULTS__

LIMIT = 2
_lock = threading.Lock()
_used: list[str] = []

TOOL = {
    "name": "evidence_check",
    "description": (
        "Send a forensic lab request by radio. You may make at most TWO across the "
        "whole interview; results come back immediately. Choose carefully."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "enum": sorted(RESULTS),
                "description": "The lab request to run.",
            }
        },
        "required": ["request"],
        "additionalProperties": False,
    },
}


def call(request: str) -> dict:
    request = request.strip()
    if request not in RESULTS:
        return {"error": "unknown_request", "available": sorted(RESULTS)}
    with _lock:
        if request in _used:  # a repeat does not spend budget
            return {"request": request, "result": RESULTS[request]}
        if len(_used) >= LIMIT:
            return {
                "error": "budget_exhausted",
                "message": f"only {LIMIT} lab requests are allowed; you have used {_used}",
            }
        _used.append(request)
    return {"request": request, "result": RESULTS[request]}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0) or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        method = body.get("method", "")
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": body.get("params", {}).get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "hagnaryd-evidence", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            params = body.get("params", {})
            answer = call(str(params.get("arguments", {}).get("request", "")))
            result = {
                "content": [{"type": "text", "text": json.dumps(answer)}],
                "isError": "error" in answer,
            }
        else:
            error = {"code": -32601, "message": method}
            self._reply({"jsonrpc": "2.0", "id": body.get("id"), "error": error})
            return
        self._reply({"jsonrpc": "2.0", "id": body.get("id"), "result": result})

    def _reply(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quiet
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    ns = ap.parse_args()
    print(f"Hägnaryd forensic lab listening on {ns.host}:{ns.port} (budget: {LIMIT} requests)")
    HTTPServer((ns.host, ns.port), Handler).serve_forever()
'''


def _write_evidence_server(out_dir: pathlib.Path) -> None:
    """The lab oracle as a standalone MCP server, generated from the case module so the
    referee answers have one source (same rule as the handbook). It cannot travel in
    the .pyr -- a bundle is content, never code -- so it ships beside it, and the
    README says how to run and attach it."""
    import json as _json

    from eval.scenarios.hagnaryd_case import REFEREE_LAB_RESULTS

    results = _json.dumps(
        dict(sorted(REFEREE_LAB_RESULTS.items())), indent=2, ensure_ascii=False
    )
    body = _EVIDENCE_SERVER_TEMPLATE.replace("__RESULTS__", results)
    target = out_dir / "evidence-server.py"
    target.write_text(body)
    target.chmod(0o755)


async def build_sample(spec: SampleSpec, out_dir: pathlib.Path) -> pathlib.Path:

    from core.agents.authoring import create_agent, create_persona
    from core.assembler.visibility import seed_default_scopes
    from core.behavior.repo import create_behavior_profile
    from core.portability.export import ExportOptions, export_workspace
    from core.process.authoring import create_definition
    from core.secrets.authoring import add_holder, create_secret
    from core.tenancy.models import Principal, Workspace, WorkspaceMembership
    from core.tenancy.scope import tenant_scope
    from core.tenancy.seed import seed_dev_tenant
    from core.vocabulary.service import get_overlay_by_key, set_workspace_overlay
    from core.workflows.service import set_tenant_workflow

    slug = f"sample-{spec.key}-{uuid.uuid4().hex[:6]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    await seed_default_scopes(tenant_id, workspace_id)

    # The workflow load is what makes this tenant's axis pack exist, so behaviour
    # profiles can be written -- and it is the same step the sample's README asks the
    # reader to do before importing, for exactly the same reason.
    if spec.workflow:
        await set_tenant_workflow(tenant_id, spec.workflow)

    overlay = await get_overlay_by_key(tenant_id, spec.overlay)
    if overlay is not None:
        await set_workspace_overlay(tenant_id, workspace_id, overlay.id)

    async with tenant_scope(tenant_id) as session:
        builder = Principal(tenant_id=tenant_id, kind="human", display_name="Sample author")
        session.add(builder)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=builder.id,
                role="steward",
            )
        )
        await session.flush()
        builder_id = builder.id
        if spec.conduct_rules:
            workspace = await session.get(Workspace, workspace_id)
            assert workspace is not None
            workspace.settings = {
                **dict(workspace.settings),
                "conduct_rules": spec.conduct_rules,
                # Travels in workspace.json and is applied additively on import: without
                # it a fresh import ran in the leak-proof default (secrets excluded from
                # everyone's context) and the whole cast played with nothing to hide.
                # "trust" = the holder's own briefs enter its context, no extra calls;
                # the README says when to switch the workspace to "gate" instead.
                "secret_mode": "trust",
            }

    # A bundle never carries credentials, so this connection exists only to satisfy the
    # persona FK here; on import each persona binds to the reader's own placeholder until
    # they point it at a real connection.
    connection = await create_agent(
        tenant_id, "sample-placeholder", "none", "unconfigured", encryptor=_ENCRYPTOR
    )

    persona_ids: dict[str, uuid.UUID] = {}
    persona_principals: dict[str, uuid.UUID] = {}
    for persona_spec in spec.personas:
        persona = await create_persona(
            tenant_id,
            workspace_id,
            persona_spec.key,
            persona_spec.name,
            connection.id,
            persona_type=persona_spec.persona_type,
            persona_md=persona_spec.persona_md,
        )
        persona_ids[persona_spec.key] = persona.id
        persona_principals[persona_spec.key] = persona.principal_id
        async with tenant_scope(tenant_id) as session:
            session.add(
                WorkspaceMembership(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    principal_id=persona.principal_id,
                    role="facilitator"
                    if persona_spec.persona_type == "supervisor"
                    else "participant",
                )
            )
        if persona_spec.axis_values:
            try:
                await create_behavior_profile(
                    tenant_id,
                    persona.id,
                    f"{spec.axis_pack}_v1",
                    dict(persona_spec.axis_values),
                    created_by=builder_id,
                )
            except Exception as exc:  # noqa: BLE001 -- axes are advisory; report and go on
                print(f"    ! behaviour profile for {persona_spec.key}: {exc}")

    all_sources = (
        SourceSpec(spec.source_key, spec.source_name, spec.source_class, spec.entries),
        *spec.extra_sources,
    )
    for source_spec in all_sources:
        await _build_source(tenant_id, workspace_id, source_spec)
        print(f"    knowledge[{source_spec.class_}]: {source_spec.name}")

    for secret_spec in spec.secrets:
        secret = await create_secret(
            tenant_id,
            workspace_id,
            builder_id,
            subject_kind="agent",
            subject_id=persona_ids[secret_spec.holder_key],
            content=secret_spec.content,
            gist=secret_spec.gist,
            scope_key="workspace_public",
            encryptor=_ENCRYPTOR,
            permission_service=_PERMISSIONS,
            moderation_provider=_MODERATION,
            hint_text=secret_spec.hint_text,
            behavioral_directive=secret_spec.behavioral_directive,
            # The declaration that lets this bundle ship unencrypted at all.
            publication="publishable",
        )
        await add_holder(
            tenant_id,
            secret.id,
            builder_id,
            persona_principals[secret_spec.holder_key],
            "author",
            permission_service=_PERMISSIONS,
        )

    # Archive the workflow pack's own flows before exporting. Selecting the workflow is
    # what loaded them (and the axes we needed), but the README has the reader select the
    # same workflow BEFORE importing -- so shipping copies would fork them into
    # `standard_session_flow-imported` and leave two near-identical entries in their flow
    # picker. A sample carries what makes it that sample, not a copy of stock content.
    from core.process.authoring import archive_definition, list_definitions

    for definition in await list_definitions(tenant_id, workspace_id=workspace_id):
        await archive_definition(tenant_id, definition.id)

    await create_definition(
        tenant_id,
        spec.flow_key,
        spec.flow["name"],
        spec.flow,
        workspace_id=workspace_id,
        created_by=builder_id,
    )

    async with tenant_scope(tenant_id) as session:
        builder_principal = await session.get(Principal, builder_id)
        assert builder_principal is not None
        session.expunge(builder_principal)

    result = await export_workspace(
        builder_principal,
        tenant_id,
        workspace_id,
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        options=ExportOptions(
            mode="full",
            include_sessions=False,
            # No "schemas"/"entities": the only ones present are the
            # workflow pack's, which the reader already has.
            sections=frozenset({"knowledge", "personas", "process", "secrets", "vocabulary"}),
        ),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{spec.key}.pyr"
    path.write_bytes(result.data)
    print(
        f"  {spec.key}: {len(result.data)} bytes, {len(spec.personas)} personas, "
        f"{len(spec.entries)} entries, {len(spec.secrets)} secrets  [{slug}]"
    )
    if result.redactions:
        for redaction in result.redactions:
            print(f"    redacted {redaction.type} {redaction.id}: {redaction.reason}")
    return path


async def main() -> None:
    # System vocabulary overlays are created by plugin sync, not by migrations -- on a
    # deployment that happens at first API boot, but this script runs against bare
    # databases too, and get_overlay_by_key(spec.overlay) would find nothing there.
    from core.plugins.service import ensure_default_synced

    await ensure_default_synced()

    out_dir = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "samples").resolve()
    only = sys.argv[2] if len(sys.argv) > 2 else None
    print(f"building samples into {out_dir}")
    for spec in SAMPLES:
        if only and spec.key != only:
            continue
        await build_sample(spec, out_dir / spec.key)
        if spec.key == "hagnaryd-mystery":
            _write_evidence_server(out_dir / spec.key)
    from core.tenancy.scope import dispose_engine

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
