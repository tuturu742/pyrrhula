"""`ReportTemplate` -- pack content, not code (CLAUDE.md rule 9).

A template says *what kind of report* to produce: who it is for, what steps the pipeline
runs, and what formats it renders to. It is declarative JSON validated by Pydantic, exactly
like a `ProcessDefinition` or an `EntitySchema`, because a pack author writing a new report
type must not be writing code (rule 10).

**`audience_mode` is the load-bearing field.** It is not a rendering preference; it decides
which events the pipeline is allowed to see *before* any model call. A template is
therefore the wrong place to put "and also show X to Y" -- there is one mode per template,
and a report for a different audience is a different report.

Three built-in templates ship here rather than in a pack because every domain needs them
and none of them says anything domain-specific: a recap, a log, and a decision summary. The
*labels* are `label_key`s the overlay resolves (rule 1); the keys themselves are neutral.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AudienceMode = Literal["participant", "overseer", "sanitised"]
StepKind = Literal["fact_frame", "chunk_summarise", "reduce", "render"]
OutputFormat = Literal["markdown", "pdf", "epub", "html"]


def _default_formats() -> list[OutputFormat]:
    return ["markdown"]


class Step(BaseModel):
    """One pipeline step. ``kind`` is a closed vocabulary: a template cannot introduce a
    new step type, because a new step type is new *behaviour*, and behaviour lives in
    ``pipeline.py`` where it can be reviewed and tested. What a template controls is which
    of the known steps run, in what order, with what budget."""

    model_config = ConfigDict(extra="forbid")

    kind: StepKind
    max_tokens: int = 600
    # Only meaningful for `fact_frame`: which record kinds enter the frame. Empty means all
    # three -- a template that wanted none of them would be asking for a summary of prose,
    # which is the thing exists to prevent.
    fact_kinds: list[str] = Field(default_factory=list)

    @field_validator("max_tokens")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"max_tokens must be >= 1, got {value}")
        return value

    @field_validator("fact_kinds")
    @classmethod
    def _known_fact_kinds(cls, value: list[str]) -> list[str]:
        known = {"resolution", "entity_change", "disclosure"}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown fact kind(s) {unknown}; known: {sorted(known)}")
        return value


class ReportTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label_key: str
    # The heading a rendered artifact carries. Nothing resolves `label_key` on the way
    # to a PDF, so every report used to be titled with the key itself ("report.recap").
    # Defaults to the key spelled out ("sanitised_log" -> "Sanitised log").
    title: str = ""
    audience_mode: AudienceMode
    pipeline: list[Step]
    # Render prose as written instead of summarising it: "final_phase" gives the last
    # phase's text without speaker labels (a drafted document, a written deliverable --
    # the summary steps destroy the thing asked for), "all" gives every phase with its
    # speakers (a transcript). Without this a template that omits the summary steps
    # rendered the facts header and nothing else.
    verbatim: str = ""
    # Explicitly typed rather than a bare lambda: mypy cannot narrow `list[str]` to
    # `list[OutputFormat]` through `default_factory`, and the annotation is the honest fix.
    output_formats: list[OutputFormat] = Field(default_factory=_default_formats)
    # A template may require human review before its artifacts are downloadable.
    # Default False: most reports are not sensitive, and a review gate everywhere is a
    # review gate nobody reads.
    requires_review: bool = False

    @model_validator(mode="after")
    def _title_from_key(self) -> ReportTemplate:
        if not self.title.strip():
            self.title = self.key.replace("_", " ").strip().capitalize()
        if self.verbatim not in ("", "final_phase", "all"):
            raise ValueError("verbatim must be '', 'final_phase' or 'all'")
        return self

    @field_validator("pipeline")
    @classmethod
    def _pipeline_shape(cls, value: list[Step]) -> list[Step]:
        kinds = [s.kind for s in value]
        if not kinds:
            raise ValueError("a report template needs at least one pipeline step")
        if "fact_frame" not in kinds:
            raise ValueError(
                "every report pipeline must include a `fact_frame` step -- structured facts "
                "come from records, never from summarised prose, and a pipeline "
                "without one is a pipeline that can only paraphrase"
            )
        if kinds.index("fact_frame") != 0:
            raise ValueError(
                "`fact_frame` must be the first step: the frame is what the narrative is "
                "written *around*, not something reconciled against it afterwards"
            )
        return value


NARRATIVE_RECAP = ReportTemplate(
    key="narrative_recap",
    label_key="report.recap",
    title="Session recap",
    audience_mode="participant",
    pipeline=[
        Step(kind="fact_frame"),
        Step(kind="chunk_summarise", max_tokens=300),
        Step(kind="reduce", max_tokens=800),
        Step(kind="render"),
    ],
    output_formats=["markdown", "pdf"],
)

SESSION_LOG = ReportTemplate(
    key="session_log",
    label_key="report.log",
    title="Session log",
    audience_mode="overseer",
    pipeline=[Step(kind="fact_frame"), Step(kind="render")],
    output_formats=["markdown"],
)

DECISION_SUMMARY = ReportTemplate(
    key="decision_summary",
    label_key="report.decision_summary",
    title="Decision summary",
    audience_mode="overseer",
    # Disclosures only: a decision log is about what was *decided and disclosed*, and
    # padding it with every randomizer call in the session would bury the thing it exists for.
    pipeline=[
        Step(kind="fact_frame", fact_kinds=["disclosure", "entity_change"]),
        Step(kind="reduce", max_tokens=600),
        Step(kind="render"),
    ],
    output_formats=["markdown", "pdf"],
    requires_review=True,
)

COMPOSED_DOCUMENT = ReportTemplate(
    key="composed_document",
    label_key="report.composed_document",
    title="Composed document",
    audience_mode="participant",
    # No chunk_summarise, no reduce: the point is the text the session composed, rendered
    # as written. Every other template here answers "what happened", which is a summary
    # of prose. This one is for a flow whose OUTPUT is the prose -- a drafted document, a
    # written deliverable -- where summarising it destroys the thing being asked for.
    pipeline=[Step(kind="fact_frame"), Step(kind="render")],
    output_formats=["markdown", "pdf"],
    verbatim="final_phase",
)

TRANSCRIPT = ReportTemplate(
    key="transcript",
    label_key="report.transcript",
    title="Transcript",
    audience_mode="participant",
    # What was said, by whom, phase by phase, with no model in between. Every other
    # template reduces the session; until this one there was no way to take the
    # conversation itself away as a document (Hägnaryd sweep, 2026-10-04).
    pipeline=[Step(kind="fact_frame"), Step(kind="render")],
    output_formats=["markdown", "pdf"],
    verbatim="all",
)

BUILT_IN_TEMPLATES: dict[str, ReportTemplate] = {
    t.key: t
    for t in (NARRATIVE_RECAP, SESSION_LOG, DECISION_SUMMARY, COMPOSED_DOCUMENT, TRANSCRIPT)
}


def get_template(key: str) -> ReportTemplate:
    template = BUILT_IN_TEMPLATES.get(key)
    if template is None:
        raise KeyError(f"unknown report template {key!r}; known: {sorted(BUILT_IN_TEMPLATES)}")
    return template
