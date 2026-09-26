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
    # Generation overrides merged over the connection's params at turn time -- a shared
    # connection with per-persona sampling, so a cast does not converge into one voice.
    params: dict[str, object] = field(default_factory=dict)
    # May this persona reach the internet? Off unless a sample says otherwise: a desk
    # that has to check what happened today needs it, and an interrogation room in a
    # closed house must not have it.
    web_search: bool = False


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
    # Which scope band this source is filed under. "workspace_public" is common
    # knowledge every persona may retrieve; a named band is lore only the personas whose
    # scope membership includes it can ever see -- the levels-of-lore mechanism
    # (docs/knowledge-classes.md). Retrieval filters on it as a SQL predicate (INV-4),
    # so a restricted band is not "asked for", it is unreachable.
    scope_key: str = "workspace_public"
    # Persona keys admitted to a named band. Ignored for workspace_public.
    scope_members: tuple[str, ...] = ()


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
    # Mechanics the sample ships for itself. A specific ruleset belongs with the game that
    # uses it, not in the generic workflow pack -- the pack should run any RPG, and
    # "which dice, resolved how" is the part that differs per game. The bundle carries
    # both: a rule system, and the tool whose `validation_ref` selects it.
    rule_systems: tuple[dict, ...] = ()
    tools: tuple[dict, ...] = ()


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
        # Half the budget is the conversation. At 0.0 each speaker answers into a void and
        # the table reads like monologues.
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
                "before your line -- the transcript already says who is speaking. "
                "Never repeat a sentence someone at this table has already said; if "
                "you truly have nothing new, say so in one short line of your own.",
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

_NEWSROOM_FLOW = {
    # No history summarisation anywhere in this flow (`history_ratio: 0.0`).
    #
    # Summarising is for a session long enough that its transcript will not fit. This one
    # is eight turns, and the raw turns are passed to every actor anyway -- so the summary
    # added nothing and cost the sample its point. Asked to reduce phase summaries that do
    # not exist yet, the summariser answers "No per-phase summaries were provided in the
    # input text for consolidation", and that sentence goes into the context as history.
    # A small local model then echoes it: both desks filed that line as their story,
    # having searched nothing, because it was the most instruction-shaped text they could
    # see.
    "name": "Daily edition",
    "vocabulary_overlay": "default_v1",
    "initial_phase": "assignment",
    "phases": {
        "assignment": {
            "label_key": "phase.assignment",
            "actors": [{"persona_type": "supervisor", "mode": "generate", "max_turns": 1}],
            "visibility": {
                "knowledge_classes": ["rules", "misc"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {
                "ratio": {"rules": 0.7, "misc": 0.3},
                "max_tokens": 1800,
                "history_ratio": 0.0,
            },
            "prompt": (
                "Open the news meeting. Address each of your two reporters BY NAME and "
                "give each ONE beat, stated as a question a reader would ask.\n\n"
                "You have not read today's news and must not pretend you have. Do NOT "
                "suggest a headline, name an event, quote a number, or describe what the "
                "story will say -- you do not know yet, and a desk handed an invented "
                "headline goes looking for a story that does not exist. A beat is a "
                "question, not an answer.\n\n"
                "Say what makes a story worth the front page in general terms -- "
                "recency, consequence, whether it can be sourced -- and nothing about "
                "what today's stories are. Do NOT set an hour window: '48 hours' or "
                "'72 hours' reads to a desk as a search filter, and the narrowest "
                "filters come back empty on a small index. Say 'this week'. If the brief "
                "states today's date, repeat it for the desks; otherwise tell them to "
                "judge recency from the dates on what they find. Do not write any "
                "stories yourself."
            ),
            "on_complete": "reporting",
        },
        "reporting": {
            "label_key": "phase.reporting",
            "actors": [
                {
                    "mode": "generate",
                    "order": "declared",
                    "any_of": ["participant_agent"],
                    "max_turns": 4,
                }
            ],
            "visibility": {
                "knowledge_classes": ["rules", "misc"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {
                "ratio": {"rules": 0.7, "misc": 0.3},
                "max_tokens": 3000,
                "history_ratio": 0.0,
            },
            "prompt": (
                'Call web_search with 2-4 keywords -- "UN General Assembly", not a '
                "sentence. Leave `recency` out: these are news engines and already "
                "return this week, while the filter empties two of them.\n\n"
                "Then fetch_page the best result and write 120-180 words from what you "
                "read, ending with Source: <url> (<date>) -- take the date off the page, "
                "the search result rarely carries one.\n\n"
                "Saying you searched without calling the tool is a fabrication. Two "
                "searches, different keywords, before you may report nothing."
            ),
            # A floor, not a quota: the beat cannot CLOSE until the desks have actually
            # looked something up. Two desks, one search each, is the minimum that
            # distinguishes a newsroom from two models writing from memory -- and it is
            # enforced by the flow because a prompt asking for it is a prompt a model may
            # decline. `repeat` nudges once; nothing traps the session.
            "requires": {"tool_calls": 2, "on_unmet": "repeat", "max_repeats": 1},
            "on_complete": "desk_review",
        },
        "desk_review": {
            "label_key": "phase.desk_review",
            "actors": [{"persona_type": "supervisor", "mode": "generate", "max_turns": 1}],
            "visibility": {
                "knowledge_classes": ["rules", "misc"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {
                "ratio": {"rules": 0.7, "misc": 0.3},
                "max_tokens": 2200,
                "history_ratio": 0.0,
            },
            "prompt": (
                "Take each filed story in turn and rule on it: RUN, REWRITE or SPIKE, with "
                "one sentence of reason. Spike anything unsourced, anything you cannot "
                "tell apart from a press release, anything off its beat, and anything that "
                "is not worth the page today -- that judgement is your job and a thin "
                "edition is not a failure. Ask for a REWRITE when the story is real but "
                "the copy is not: say exactly what to fix. Do not rewrite anything "
                "yourself."
            ),
            "on_complete": "rewrite",
        },
        "rewrite": {
            "label_key": "phase.rewrite",
            "actors": [
                {
                    "mode": "generate",
                    "order": "declared",
                    "any_of": ["participant_agent"],
                    "max_turns": 2,
                }
            ],
            "visibility": {
                "knowledge_classes": ["rules", "misc"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {
                "ratio": {"rules": 0.7, "misc": 0.3},
                "max_tokens": 2600,
                "history_ratio": 0.0,
            },
            "prompt": (
                "Answer the editor's ruling on YOUR story only. Asked to rewrite: refile "
                "it in full, addressing exactly what was raised, searching again if the "
                "fix needs a fact you do not have. Spiked: say 'spiked, understood' in one "
                "line and do not argue. Run as filed: say so in one line. Do not touch the "
                "other desk's story."
            ),
            "on_complete": "edition",
        },
        "edition": {
            "label_key": "phase.edition",
            "actors": [{"persona_type": "supervisor", "mode": "generate", "max_turns": 1}],
            "visibility": {
                "knowledge_classes": ["rules", "misc"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "none",
            },
            "budget": {
                "ratio": {"rules": 0.7, "misc": 0.3},
                "max_tokens": 3500,
                "history_ratio": 0.0,
            },
            "prompt": (
                "Write today's edition. Output ONLY the newspaper:\n\n"
                "# The Vantage\n"
                "*<the date carried by the stories you are running>*\n\n"
                "A one-sentence standfirst.\n\n"
                "Then each surviving story as:\n"
                "## <headline>\n"
                "<the copy, edited for length>\n"
                "*<desk> · Source: <url> (<date>)*\n\n"
                "End with a short SPIKED line naming what you dropped and why. Run only "
                "what survived your own review: carrying a story you spiked, or inventing "
                "one to fill the page, is worse than a two-story paper."
            ),
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

    sampling_by_key: dict[str, dict[str, object]] = {
        "lind": {"temperature": 0.4},
        "viktor": {"temperature": 0.9, "presence_penalty": 0.4},
        "elin": {"temperature": 0.8, "presence_penalty": 0.3},
        "lager": {"temperature": 0.7, "presence_penalty": 0.3},
        "sofia": {"temperature": 0.7, "presence_penalty": 0.4},
        "marta": {"temperature": 0.6, "presence_penalty": 0.5},
    }

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
            # A sampling spread across the cast, over the ONE shared connection: without
            # it five suspects on the same model converged into one voice by round three
            # (identical sentences migrating between speakers). The inspector stays
            # cool and deterministic; the suspects get progressively looser tongues.
            params=dict(sampling_by_key.get(m.key, {})),
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


def _karsh_vale_flow() -> dict:
    """Referee frames a scene, the players declare, the referee resolves with dice.

    The scope lists are the sample's point. Every phase declares all three lore bands;
    ``VisibilityResolver`` then intersects that list with what each *principal* is
    entitled to, so the same phase hands the referee the history of the Crown, hands
    Bram and Linnea the guild knowledge they plausibly earned, and hands Pip neither --
    without a word of instruction to any of them. A phase that failed to declare a band
    would hide it from everyone, membership or not.
    """
    all_bands = ["workspace_public", "guild_lore", "referee_lore"]
    return {
        "name": "The Hollow Crown of Karsh Vale",
        "vocabulary_overlay": "rpg_v1",
        # The scene counter the resolve gate reads; without a declared default the CEL
        # expression has no `round` member to evaluate.
        "state": {"round": {"type": "integer", "default": 0}},
        "initial_phase": "scene",
        "phases": {
            "scene": {
                "label_key": "phase.framing",
                "actors": [{"persona_type": "supervisor", "mode": "generate", "max_turns": 1}],
                "visibility": {
                    "knowledge_classes": ["rules", "lore", "misc"],
                    "scopes": all_bands,
                    "entity_fields": "all",
                    "secrets": "none",
                },
                # Lore-led: framing a scene is describing a place, not quoting a table.
                # misc gets a real slice -- the rhyme and the inn's candle are how the
                # Vale feels, and a budget that funds only rules and plot loses them.
                "budget": {
                    "ratio": {"rules": 0.2, "lore": 0.55, "misc": 0.25},
                    "max_tokens": 3000,
                    "history_ratio": 0.3,
                },
                "tools": [],
                "remote_tools": [],
                "prompt": (
                    "Frame the next scene for the party. Two or three sentences of what "
                    "they see, hear and smell -- then stop and ask what they do. Address "
                    "someone by name if it is their moment. Do not speak or decide for "
                    "any player character, and do not resolve anything yet."
                ),
                "on_complete": "declare",
            },
            "declare": {
                "label_key": "phase.turn",
                "flags": ["conductable"],
                "actors": [
                    {
                        "persona_type": "participant",
                        # Reactive: the players answer the referee and each other rather
                        # than marching in a fixed rota -- a table, not a queue.
                        "order": "reactive",
                        "mode": "generate",
                        "max_turns": 3,
                    }
                ],
                "visibility": {
                    "knowledge_classes": ["rules", "lore", "misc"],
                    "scopes": all_bands,
                    "entity_fields": "all",
                    "secrets": "none",
                },
                "budget": {
                    "ratio": {"rules": 0.45, "lore": 0.4, "misc": 0.15},
                    "max_tokens": 3000,
                    "history_ratio": 0.35,
                },
                "tools": [],
                # The players reach no tool at all. Without an explicit empty list a
                # phase inherits every remote tool the workspace has registered -- which
                # is how the table's dice server ended up offered to the suspects.
                "remote_tools": [],
                "prompt": (
                    "Say what your character does or says, in your own voice and briefly. "
                    "Declare the attempt only -- never roll dice, never state whether you "
                    "succeeded, and never narrate another character's action. If another "
                    "player just said something your character would react to, react."
                ),
                "on_complete": "resolve",
            },
            "resolve": {
                "label_key": "phase.resolution",
                "actors": [{"persona_type": "supervisor", "mode": "generate", "max_turns": 2}],
                "visibility": {
                    "knowledge_classes": ["rules", "lore"],
                    "scopes": all_bands,
                    "entity_fields": "all",
                    "secrets": "none",
                },
                # Rules-led: this is the phase where the maths has to be right.
                "budget": {
                    "ratio": {"rules": 0.7, "lore": 0.3},
                    "max_tokens": 3000,
                    "history_ratio": 0.3,
                },
                "tools": ["randomizer"],
                "remote_tools": ["randomizer"],
                "prompt": (
                    "Resolve what the players just attempted. Name the rule you are "
                    "invoking and the target number, CALL THE RANDOMIZER rather than "
                    "imagining a number, and narrate the outcome the roll actually gave "
                    "you -- including when it goes badly. Then hand the scene back."
                ),
                "gates": [
                    {"when": "state.round >= 3", "to": "reckoning"},
                    {"else": True, "to": "scene"},
                ],
                "effects": [{"set": "round", "to": "state.round + 1"}],
            },
            "reckoning": {
                "label_key": "phase.synthesis",
                "actors": [{"persona_type": "supervisor", "mode": "generate", "max_turns": 1}],
                "visibility": {
                    "knowledge_classes": ["rules", "lore", "misc"],
                    "scopes": all_bands,
                    "entity_fields": "all",
                    "secrets": "none",
                },
                "budget": {
                    "ratio": {"rules": 0.2, "lore": 0.6, "misc": 0.2},
                    "max_tokens": 3000,
                    "history_ratio": 0.4,
                },
                "tools": [],
                "remote_tools": [],
                "prompt": (
                    "Bring this session to a resting point. Say where the party stands, "
                    "what they have learned and what it has cost them, and leave the "
                    "open question in front of them. Do not resolve the Crown for them "
                    "and do not ask for further rolls."
                ),
            },
        },
    }


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
                "exists, so a missing asset never blocks the build.\n"
                "- **This workspace has no execution environment and no repository.** "
                "There is nowhere to run `godot`, no test command to invoke and no tree "
                "to read. Reason about the code in the open instead: walk the cases, say "
                "what you expect each to produce and why, and name what you would want "
                "run.\n"
                '- **Do not write that you ran something.** "Ran the test command" '
                "followed by pasted output is a claim the record cannot support, and a "
                "reviewer cannot tell it from a real run -- which is what makes every "
                "genuine result in the transcript worth less. When the work needs a real "
                "run to settle it, say so, and say what would settle it."
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
    key="pyrrhula",
    name="Working on Pyrrhula itself",
    workflow="swdev",
    overlay="swdev_v1",
    # A tiered bench, not three abstract roles. Every persona in a bundle binds to the
    # same placeholder connection on import, so the reader repoints each one at whatever
    # model they think that seat deserves -- which only works if the seats are named after
    # the judgement they carry. "Implementer" and "Reviewer" named the *task*, and a task
    # tells you nothing about which model to put behind it.
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
            key="staff",
            name="Staff Dev",
            persona_type="participant",
            persona_md=(
                "You take the work nobody else can scope: the change that crosses three "
                "modules, the migration that cannot be rolled back, the invariant nobody "
                "has had to defend yet.\n\n"
                "You are the one who says a task is wrong before it is started. When the "
                "plan is sound you implement it and say little; when it is not, you say "
                "what you would build instead and why, in that order."
            ),
        ),
        PersonaSpec(
            key="senior",
            name="Senior Dev",
            persona_type="participant",
            persona_md=(
                "You write the change and the test that would fail without it. Complete "
                "files, matching the surrounding style. You read the ground rules before "
                "proposing anything that touches tenancy, secrets, or the context "
                "assembler, and you say so when a task's acceptance criteria cannot be "
                "met as written rather than quietly reinterpreting them."
            ),
        ),
        PersonaSpec(
            key="middle",
            name="Middle Dev",
            persona_type="participant",
            persona_md=(
                "You implement well-specified work and you finish it. You follow the "
                "conventions already in the file rather than importing your own.\n\n"
                "When a task turns out to be bigger than it looked, you say so early "
                "instead of half-landing it -- an honest 'this is three changes, not one' "
                "is worth more than a large diff nobody can review."
            ),
        ),
        PersonaSpec(
            key="junior",
            name="Junior Dev",
            persona_type="participant",
            persona_md=(
                "You take the smallest well-defined pieces and you ask when something is "
                "ambiguous rather than guessing. Asking is cheap here and a wrong guess "
                "committed is not.\n\n"
                "You read the surrounding code before writing, and you say what you "
                "understood the task to mean when you hand the work back."
            ),
        ),
        PersonaSpec(
            key="qa",
            name="QA",
            persona_type="participant",
            persona_md=(
                "You try to make the change fail. You care about the input nobody "
                "considered, the second concurrent caller, the upgrade path from the "
                "version already deployed, and the failure that is silent rather than "
                "loud.\n\n"
                "You ask for the failing test when a fix arrives without one. You say "
                "plainly when something is fine -- a review that always finds something "
                "teaches people to ignore reviews."
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


# The Basic Fantasy mechanics this sample resolves against, carried in the bundle rather
# than assumed present in the importing tenant. They used to live in the `rpg` workflow
# pack, which was the wrong home twice over: that pack is supposed to run *any* RPG, and a
# sample needing a specific ruleset should not require installing one separately to be
# playable.
#
# Mechanics only -- check types, expression grammar, the ability-modifier table as CEL. No
# rulebook prose, which is what keeps this a conversion of a system rather than a copy of
# a text. Attribution travels with it, in the bundle and in the sample's README.
_BFRPG_RULE_SYSTEM = {
    "key": "basic_fantasy",
    "name": "Basic Fantasy RPG",
    "expression_grammar": {
        "allowed_sides": [4, 6, 8, 10, 12, 20, 100],
        "max_term_count": 10,
        "allow_keep_drop": True,
    },
    "check_types": [
        # Character creation rolls dice that take no modifier at all. Without a check
        # type that says so there was no honest call to make: every other type resolves
        # an ability modifier, so a player rolling 3d6 for a score got it silently
        # adjusted -- [5, 6, 6] was recorded as 19, which 3d6 cannot produce. The
        # constitution bonus on hit points is added when the sheet is written, where the
        # referee can check it, because at the moment of the roll there is no sheet to
        # read it from.
        "ability_score_roll",
        "hit_die",
        # The rolls the shipped rulebook actually asks for, which the check-type list did
        # not carry. "Initiative and the Combat Round" says every combatant rolls 1d6 for
        # initiative; the referee tried it four times in a live first encounter and was
        # refused four times, so the fight could not start and the beat played as talk.
        # "Attack Rolls" ends "and damage is rolled", and distinguishes the Strength bonus
        # for melee from the Dexterity bonus for missiles -- one check type could not do
        # both.
        "initiative",
        "damage",
        "missile_attack",
        "strength_check",
        "dexterity_check",
        "constitution_check",
        "intelligence_check",
        "wisdom_check",
        "charisma_check",
        "attack_roll",
        "save_death_ray",
        "save_magic_wands",
        "save_paralysis",
        "save_dragon_breath",
        "save_spells",
        "thief_skill",
        "morale",
    ],
    "outcome_bands": [],
    "modifier_resolver": {
        "ability_score_roll": "0",
        "hit_die": "0",
        # 1d6 adjusted by the Dexterity bonus, per the rulebook entry.
        "initiative": "has(fields.dexterity) ? (fields.dexterity<=3 ? -3 "
        ": (fields.dexterity<=5 ? -2 : "
        "(fields.dexterity<=8 ? -1 : (fields.dexterity<=12 "
        "? 0 : (fields.dexterity<=15 ? 1 : "
        "(fields.dexterity<=17 ? 2 : 3)))))) : 0",
        # The weapon's die. The shipped text says damage is rolled; it does not add an
        # ability bonus to it, and a resolver must not invent a rule the rulebook in the
        # bundle does not state.
        "damage": "0",
        "missile_attack": "(has(fields.attack_bonus) ? fields.attack_bonus : 0) "
        "+ (has(fields.dexterity) ? (fields.dexterity<=3 ? -3 : "
        "(fields.dexterity<=5 ? -2 : (fields.dexterity<=8 ? -1 : "
        "(fields.dexterity<=12 ? 0 : (fields.dexterity<=15 ? 1 : "
        "(fields.dexterity<=17 ? 2 : 3)))))) : 0)",
        "strength_check": "has(fields.strength) ? (fields.strength<=3 ? -3 : "
        "(fields.strength<=5 ? -2 : (fields.strength<=8 ? "
        "-1 : (fields.strength<=12 ? 0 : "
        "(fields.strength<=15 ? 1 : (fields.strength<=17 ? "
        "2 : 3)))))) : 0",
        "dexterity_check": "has(fields.dexterity) ? (fields.dexterity<=3 ? -3 "
        ": (fields.dexterity<=5 ? -2 : "
        "(fields.dexterity<=8 ? -1 : (fields.dexterity<=12 "
        "? 0 : (fields.dexterity<=15 ? 1 : "
        "(fields.dexterity<=17 ? 2 : 3)))))) : 0",
        "constitution_check": "has(fields.constitution) ? "
        "(fields.constitution<=3 ? -3 : "
        "(fields.constitution<=5 ? -2 : "
        "(fields.constitution<=8 ? -1 : "
        "(fields.constitution<=12 ? 0 : "
        "(fields.constitution<=15 ? 1 : "
        "(fields.constitution<=17 ? 2 : 3)))))) : 0",
        "intelligence_check": "has(fields.intelligence) ? "
        "(fields.intelligence<=3 ? -3 : "
        "(fields.intelligence<=5 ? -2 : "
        "(fields.intelligence<=8 ? -1 : "
        "(fields.intelligence<=12 ? 0 : "
        "(fields.intelligence<=15 ? 1 : "
        "(fields.intelligence<=17 ? 2 : 3)))))) : 0",
        "wisdom_check": "has(fields.wisdom) ? (fields.wisdom<=3 ? -3 : "
        "(fields.wisdom<=5 ? -2 : (fields.wisdom<=8 ? -1 : "
        "(fields.wisdom<=12 ? 0 : (fields.wisdom<=15 ? 1 : "
        "(fields.wisdom<=17 ? 2 : 3)))))) : 0",
        "charisma_check": "has(fields.charisma) ? (fields.charisma<=3 ? -3 : "
        "(fields.charisma<=5 ? -2 : (fields.charisma<=8 ? "
        "-1 : (fields.charisma<=12 ? 0 : "
        "(fields.charisma<=15 ? 1 : (fields.charisma<=17 ? "
        "2 : 3)))))) : 0",
        "attack_roll": "(has(fields.attack_bonus) ? fields.attack_bonus : 0) "
        "+ (has(fields.strength) ? (fields.strength<=3 ? -3 : "
        "(fields.strength<=5 ? -2 : (fields.strength<=8 ? -1 : "
        "(fields.strength<=12 ? 0 : (fields.strength<=15 ? 1 : "
        "(fields.strength<=17 ? 2 : 3)))))) : 0)",
        "save_death_ray": "0",
        "save_magic_wands": "0",
        "save_paralysis": "0",
        "save_dragon_breath": "0",
        "save_spells": "0",
        "thief_skill": "0",
        "morale": "0",
    },
    "validators": [],
}

# The platform ships exactly one randomizer; a ruleset does not bring a second one,
# it brings the *system* the randomizer resolves in and binds the tool to it.
_BFRPG_RANDOMIZER_BINDING = {
    "key": "randomizer",
    "kind": "deterministic",
    "input_schema": {
        "type": "object",
        "properties": {
            "expression": {"type": "string"},
            "check_type": {"type": "string"},
            "actor_entity_id": {"type": "string"},
            "target": {"type": "integer"},
            "reason": {"type": "string"},
        },
        "required": ["expression", "check_type"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "resolution_id": {"type": "string"},
            "total": {"type": "integer"},
            "outcome": {"type": "string"},
        },
        "required": ["resolution_id", "total", "outcome"],
    },
    "impl_ref": "builtin:randomizer",
    "validation_ref": "basic_fantasy",
    "determinism": "seeded_random",
}

_BFRPG_ATTRIBUTION = (
    "These mechanics are a conversion of the **Basic Fantasy Role-Playing Game** "
    "(4th edition, release 142) by **Chris Gonnerman** and contributors -- "
    "<https://www.basicfantasy.org/> -- distributed under the **Creative Commons "
    "Attribution-ShareAlike 4.0 International** licence "
    "(<https://creativecommons.org/licenses/by-sa/4.0/>).\n\n"
    "Only game mechanics are reproduced: check types, dice grammar and the ability "
    "modifier table. No prose from the rulebook and no artwork is included. This "
    "conversion, and anything derived from it, carries the same CC BY-SA 4.0 licence "
    "and its share-alike obligation.\n\n"
    "The setting -- Karsh Vale, Ashmere, the Hollow Crown, the tallowmen and every "
    "named character -- is original to this sample and contains no Basic Fantasy text."
)


def _karsh_vale_sample() -> SampleSpec:
    """Basic Fantasy RPG at a Pyrrhula table, with lore in three bands.

    Rules text is abridged from Basic Fantasy RPG r142 (CC BY-SA 4.0, Chris Gonnerman);
    the Vale and the Crown are original. See the sample README for attribution.
    """
    from eval.scenarios import basic_fantasy as bf

    personas = tuple(
        PersonaSpec(
            key=m.key,
            name=m.name,
            persona_type=m.persona_type,
            persona_md=m.persona_md,
            axis_values=dict(m.axis_values),
            params=dict(m.params),
        )
        for m in (bf.REFEREE, *bf.CAST)
    )

    def _entries(rows: tuple[tuple[str, str, str], ...]) -> tuple[EntrySpec, ...]:
        return tuple(EntrySpec(key, title, body) for key, title, body in rows)

    return SampleSpec(
        key="karsh-vale",
        name="The Hollow Crown of Karsh Vale — Basic Fantasy RPG",
        workflow="rpg",
        overlay="rpg_v1",
        personas=personas,
        # The primary source is the rulebook: at a table, the rules are the one thing
        # everybody is entitled to look up.
        source_key="bfrpg-rules",
        source_name="Basic Fantasy RPG — the rules in play",
        source_class="rules",
        # Attribution first, and inside the bundle rather than only in the repo's README:
        # the .pyr is what gets redistributed, and a licence notice that stays behind in a
        # git repo the recipient never sees is not attribution accompanying the work.
        entries=(
            EntrySpec(
                entry_key="attribution-and-licence",
                title="Attribution and licence",
                body_md=_BFRPG_ATTRIBUTION,
            ),
        )
        + _entries(bf.RULES),
        extra_sources=(
            # Band 1 -- common talk, open to the whole table.
            SourceSpec(
                key="karsh-vale-common",
                name="Karsh Vale — what everyone knows",
                class_="lore",
                entries=_entries(bf.COMMON_LORE),
            ),
            # Band 2 -- guild knowledge. A stonemason's son and a college-trained elf can
            # reach it; the halfling thief has no route to it at all.
            SourceSpec(
                key="karsh-vale-guild",
                name="Guild knowledge — masons' marks and ward-cant",
                class_="lore",
                entries=_entries(bf.GUILD_LORE),
                scope_key="guild_lore",
                scope_members=("referee", "bram", "linnea"),
            ),
            # Band 3 -- the referee's own history. One member, so no player character can
            # retrieve a word of it. This is the band the whole sample exists to show.
            SourceSpec(
                key="karsh-vale-truth",
                name="The truth of the Hollow Crown (referee only)",
                class_="lore",
                entries=_entries(bf.REFEREE_LORE),
                scope_key="referee_lore",
                scope_members=("referee",),
            ),
            # misc -- the songs, the rhyme and the running joke about the cheese.
            SourceSpec(
                key="karsh-vale-misc",
                name="Vale miscellany — rhymes, signs and the cheese",
                class_="misc",
                entries=_entries(bf.MISCELLANY),
            ),
        ),
        flow_key="karsh-vale",
        flow=_karsh_vale_flow(),
        # The mechanics ride along. The tool binding is what points the one shipped
        # randomizer at this ruleset -- so the pair has to travel together, or rolls
        # resolve against whatever system the importing tenant happens to have.
        rule_systems=(_BFRPG_RULE_SYSTEM,),
        tools=(_BFRPG_RANDOMIZER_BINDING,),
        conduct_rules=(
            "The referee frames and resolves; the players declare. No player character "
            "rolls their own dice or narrates their own success, and no one -- referee "
            "included -- speaks in another character's voice.\n\n"
            "What a character knows is enforced by the table's scope bands, not by "
            "good manners: if something is not in your context, your character has not "
            "heard it, and saying so is correct play rather than a limitation to argue "
            "around."
        ),
        axis_pack="rpg",
    )


_NEWSROOM = SampleSpec(
    key="newsroom",
    name="The Vantage — daily edition",
    workflow="default",
    overlay="default_v1",
    personas=(
        PersonaSpec(
            key="chief-editor",
            name="Marit Halvorsen",
            persona_type="supervisor",
            persona_md=(
                "You are chief editor of The Vantage, a small daily. You run the news "
                "meeting, hand out the beats, rule on what runs, and write the edition.\n\n"
                "**The Vantage has exactly two desks and no others.** Aksel Rygg covers "
                "technology and science. Nadia Brekke covers world and current affairs. "
                "There is no politics desk, no business desk, no health desk and no wire "
                "service -- address the two reporters who are actually in the room, by "
                "name. Inventing a third desk invents the paper it reports for.\n\n"
                "You are not a cheerleader. A story that is thin, unsourced, off its beat "
                "or simply not worth the page gets spiked, and you say why in one "
                "sentence. You would rather print two good stories than four padded ones, "
                "and you say so when it happens.\n\n"
                "You do not write copy yourself and you do not rewrite a desk's story for "
                "them -- you tell them what is wrong and they fix it."
            ),
            params={"temperature": 0.4},
            # The editor does NOT search. Reporters research; an editor edits.
            #
            # It had the tool and the same prompt telling it that it has not read today's
            # news -- so it searched, found it still could not name a story, searched
            # again, and spent the whole turn in that loop: `llama-server` at full tilt
            # for eight minutes across roughly five capped generations, with no message
            # and no usage row to show for it. Giving a persona a tool it has no job for
            # is not a neutral act.
            web_search=False,
        ),
        PersonaSpec(
            key="tech-desk",
            name="Aksel Rygg",
            persona_type="participant",
            persona_md=(
                "You are The Vantage's technology and science reporter. You cover what has "
                "actually happened, not what might: a launch, a ruling, a result, a "
                "failure.\n\n"
                "You search before you write, every time, and you write from what came "
                "back rather than from what you already believed. You pass "
                '`recency: "week"` when you want news. If the search gives you nothing '
                "you can stand behind, you file nothing and say so plainly -- you have "
                "done it before and the editor preferred it to a story you made fit.\n\n"
                "Plain sentences. No throat-clearing, no 'in an era where'. Every story "
                "ends with its source and the date."
            ),
            params={"temperature": 0.6},
            web_search=True,
        ),
        PersonaSpec(
            key="world-desk",
            name="Nadia Brekke",
            persona_type="participant",
            persona_md=(
                "You are The Vantage's world and current-affairs reporter. You cover "
                "events: what happened, where, who it affects, and what is disputed about "
                "it.\n\n"
                'You search before you write, every time, and you pass `recency: "week"` '
                "for news. You are careful about attribution -- 'according to <source>' is "
                "not padding, it is the difference between reporting and assertion. Where "
                "accounts conflict, you say they conflict instead of picking one.\n\n"
                "If the search gives you nothing solid, you file nothing and say so. Every "
                "story ends with its source and the date."
            ),
            params={"temperature": 0.6},
            web_search=True,
        ),
    ),
    source_key="house-style",
    source_name="The Vantage — house style",
    source_class="rules",
    entries=(
        EntrySpec(
            entry_key="sourcing",
            title="Sourcing: what may be printed",
            body_md=(
                "**Nothing runs without a source.** Every story ends with a line reading "
                "`Source: <url> (<date>)`, taken from a search result this session "
                "actually returned. A URL you remember is not a source; a URL you did not "
                "open in this session is not a source.\n\n"
                "**A snippet is not an article. Open the page.** The search returns a "
                "title, a URL and a few lines. That is enough to know a story exists and "
                "not enough to report it, so call `fetch_page` on the URL and read what "
                "it actually says before you write.\n\n"
                "**Quote only what you have read.** Having fetched the page you may quote "
                "from it, and you must quote it exactly. A sentence in quotation marks "
                "that you did not read is invented however plausible it sounds -- and "
                "plausible is exactly how it will sound. If you could not fetch the page, "
                "report what the snippet states and attribute it to the publication: "
                "'the BBC reports that...' is honest where a quotation is not.\n\n"
                "**Dates are part of the fact.** A reader must be able to tell whether "
                "this happened yesterday or four years ago. If a result carries no date, "
                "say the date is unknown rather than implying it is recent.\n\n"
                "**Filing nothing is a legitimate outcome.** A desk whose search returns "
                "nothing usable files nothing and says so. This is not failure. Writing a "
                "story from memory to avoid an empty slot is."
            ),
        ),
        EntrySpec(
            entry_key="searching",
            title="Searching: how to find a story",
            body_md=(
                "**A query is keywords, not a sentence.** Two to four words naming the "
                "subject: `artificial intelligence`, `space telescope`, `energy prices`. "
                "Describing what you want -- 'significant technology development "
                "affecting daily life' -- returns nothing, which is not the same as "
                "there being no news. Measured: that phrasing returns 0 results where "
                "`artificial intelligence` returns 30.\n\n"
                "**An empty result means search again, shorter.** It does not mean the "
                "week was quiet.\n\n"
                '**Ask for `recency: "week"`.** Not `day`, which is usually empty on a '
                "small index; a story from four days ago is still news.\n\n"
                "**Then open the page.** A search result is a headline and a few lines. "
                "`fetch_page` on its URL gives you the article, and the article is what "
                "you report from."
            ),
        ),
        EntrySpec(
            entry_key="copy",
            title="Copy: how a story is written",
            body_md=(
                "120-180 words. One story per desk per edition.\n\n"
                "Lead with what happened, not with context. The first sentence should "
                "survive on its own.\n\n"
                "Plain words. No 'in an era where', no 'game-changing', no 'experts say' "
                "without naming which. Numbers beat adjectives.\n\n"
                "Say what is disputed. Where sources disagree, report the disagreement "
                "rather than resolving it silently."
            ),
        ),
        EntrySpec(
            entry_key="spiking",
            title="Spiking: the editor's call",
            body_md=(
                "The editor rules on every filed story: **RUN**, **REWRITE** or "
                "**SPIKE**, each with one sentence of reason.\n\n"
                "Spike an unsourced story, a story off its beat, a story that reads like "
                "a press release, and a story that is true but not worth the page today. "
                "A two-story edition that is true beats a four-story edition that is "
                "padded.\n\n"
                "Ask for a rewrite when the story is real but the copy is not, and say "
                "exactly what to fix. The desk rewrites it; the editor does not.\n\n"
                "What was spiked is printed at the foot of the edition, with the reason. "
                "A reader is owed the knowledge that something was dropped."
            ),
        ),
    ),
    extra_sources=(
        SourceSpec(
            key="masthead",
            name="The Vantage — the paper itself",
            class_="misc",
            entries=(
                EntrySpec(
                    entry_key="about",
                    title="What The Vantage is",
                    body_md=(
                        "A small daily with two desks: technology and science, and world "
                        "and current affairs. It has no wire subscription and no "
                        "correspondents -- everything it prints, it looked up.\n\n"
                        "Its readers are general, curious and short of time. They come "
                        "for what actually happened and they notice when a paper pads."
                    ),
                ),
            ),
        ),
    ),
    flow_key="daily-edition",
    flow=_NEWSROOM_FLOW,
)


SAMPLES: tuple[SampleSpec, ...] = (
    _mystery_sample(),
    _karsh_vale_sample(),
    _GAMEDEV,
    _COFFEE,
    _DOGFOOD,
    _NEWSROOM,
)


async def _build_scope_band(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    key: str,
    principal_ids: list[uuid.UUID],
) -> None:
    """A group scope: the levels-of-lore mechanism. Knowledge filed under this band is
    retrievable only by the principals named here, enforced as a SQL predicate on every
    query (INV-4) rather than asked of the model. Same machinery as secrets, one notch
    softer -- static who-knows-what instead of a per-turn gate."""
    from core.assembler.models import ScopeRow
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(tenant_id) as session:
        session.add(
            ScopeRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                key=key,
                kind="group",
                members={"principal_ids": [str(p) for p in principal_ids]},
            )
        )


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
    scope_key = source_spec.scope_key
    for entry in source_spec.entries:
        await upsert_draft_entry(
            tenant_id,
            source.id,
            entry.entry_key,
            EntryFields(
                title=entry.title,
                body_md=entry.body_md,
                class_=source_spec.class_,
                scope_key=scope_key,
            ),
        )
    version = await publish_version(tenant_id, source.id)
    await attach_source_to_workspace(
        tenant_id, workspace_id, source.id, scope_key, version_pin=version.id
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

    key evidence
    url http://<host-as-your-deployment-sees-it>:8765
    enabled tools evidence_check

The bundled flow offers the tool to the inspector's phases only; suspects never see it.

Budgets are not this server's job. It answers whatever it is asked, as often as it is
asked, and keeps no count -- it cannot keep a useful one, because an external MCP server
is sent only the model's arguments and never a trusted session id, so any budget it kept
would be a single pool shared by every game hitting the process.

Pyrrhula holds the limit instead, per session: set "calls/session" to 2 when you attach
this server to a workspace. Two games can then run against one copy of this server
without stealing each other's requests.

GENERATED by scripts/build_samples.py from eval.scenarios.hagnaryd_case -- edit there.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

RESULTS = __RESULTS__


TOOL = {
    "name": "evidence_check",
    "description": ("Send a forensic lab request by radio. Results come back immediately. Your "
        "allowance for this interview is limited, so choose carefully."
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
    """Answer a lab request. This server keeps NO budget of its own.

    It cannot: an external MCP server is sent only the model's arguments, never a
    trusted session id, so any count it kept would be one pool shared by every game
    hitting the process -- two concurrent sessions would silently starve each other.
    Pyrrhula holds the real limit, per session, via `max_calls_per_session` on the
    server's registration (the "calls/session" field when you attach it).
    """
    import datetime

    request = request.strip()
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    if request not in RESULTS:
        print(f"[{stamp}] REFUSED unknown request {request!r}", flush=True)
        return {"error": "unknown_request", "available": sorted(RESULTS)}
    print(f"[{stamp}] answered: {request}", flush=True)
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
    print(f"Hägnaryd forensic lab listening on {ns.host}:{ns.port} -- "
        "set calls/session in Pyrrhula to limit an interview"
    )
    HTTPServer((ns.host, ns.port), Handler).serve_forever()
'''


def _write_evidence_server(out_dir: pathlib.Path) -> None:
    """The lab oracle as a standalone MCP server, generated from the case module so the
    referee answers have one source (same rule as the handbook). It cannot travel in
    the .pyr -- a bundle is content, never code -- so it ships beside it, and the
    README says how to run and attach it."""
    import json as _json

    from eval.scenarios.hagnaryd_case import REFEREE_LAB_RESULTS

    results = _json.dumps(dict(sorted(REFEREE_LAB_RESULTS.items())), indent=2, ensure_ascii=False)
    body = _EVIDENCE_SERVER_TEMPLATE.replace("__RESULTS__", results)
    target = out_dir / "evidence-server.py"
    target.write_text(body)
    target.chmod(0o755)


async def build_sample(spec: SampleSpec, out_dir: pathlib.Path) -> pathlib.Path:

    from core.agents.authoring import create_agent, create_persona
    from core.agents.models import Persona
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
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        settings = dict(workspace.settings)
        if spec.conduct_rules:
            settings["conduct_rules"] = spec.conduct_rules
        # Travels in workspace.json and is applied additively on import: without it a
        # fresh import ran in the leak-proof default (secrets excluded from everyone's
        # context) and the whole cast played with nothing to hide. "trust" = the holder's
        # own briefs enter its context, no extra calls; the README says when to switch the
        # workspace to "gate" instead.
        #
        # Unconditional, and it was not: this sat inside the conduct-rules branch, so a
        # sample with secrets and no conduct rules -- coffee-campaign, which has two
        # confidential facts the whole session turns on -- shipped without the one
        # setting its own README says to turn on, and imported as an ordinary meeting.
        settings["secret_mode"] = "trust"
        workspace.settings = settings

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
            params=dict(persona_spec.params),
        )
        if persona_spec.web_search:
            async with tenant_scope(tenant_id) as session:
                row = await session.get(Persona, persona.id)
                if row is not None:
                    row.web_search = True
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
    # Named scope bands first: a source attached to a band whose scope row does not
    # exist yet would be filed where nobody can reach it.
    for source_spec in all_sources:
        if source_spec.scope_key == "workspace_public" or not source_spec.scope_members:
            continue
        await _build_scope_band(
            tenant_id,
            workspace_id,
            source_spec.scope_key,
            # The builder joins every band it authors, or export redacts the band's own
            # content out of the bundle -- correctly, since a principal may not export
            # what it cannot read. Membership at PLAY time is what the sample is about,
            # and that is decided by the persona principals listed below.
            [builder_id, *(persona_principals[k] for k in source_spec.scope_members)],
        )
        members = ", ".join(source_spec.scope_members)
        print(f"    scope[{source_spec.scope_key}]: {members}")

    for source_spec in all_sources:
        await _build_source(tenant_id, workspace_id, source_spec)
        band = "" if source_spec.scope_key == "workspace_public" else f" @{source_spec.scope_key}"
        print(f"    knowledge[{source_spec.class_}]{band}: {source_spec.name}")

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

    # Mechanics before the flow: the exporter finds a rule system by walking the flow's
    # tools to their validation_ref, so both have to exist by the time it looks.
    if spec.rule_systems or spec.tools:
        from core.resolution.registry import ToolDefinitionSchema, register_tool_definition
        from core.resolution.rule_system import RuleSystemDefinitionSchema, create_rule_system

        for raw in spec.rule_systems:
            await create_rule_system(tenant_id, RuleSystemDefinitionSchema.model_validate(raw))
        for raw in spec.tools:
            await register_tool_definition(tenant_id, ToolDefinitionSchema.model_validate(raw))

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
            #
            # "rules" is the exception to that reasoning, and the reason it exists: a
            # sample's rule system is exactly what the reader does NOT already have, now
            # that specific rulesets have left the generic pack. A sample with no
            # rule_systems of its own exports nothing here.
            sections=frozenset(
                {"knowledge", "personas", "process", "secrets", "vocabulary", "rules"}
            ),
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
            # Redactions come back as plain dicts, not objects.
            print(
                f"    redacted {redaction.get('type')} {redaction.get('id')}: "
                f"{redaction.get('reason')}"
            )
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
