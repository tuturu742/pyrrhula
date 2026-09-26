"""Acceptance criteria for report rendering: every format carries identical content
and a visible redaction stub, artifacts are scoped to the report's target principal, and a
review-required template blocks downloads until reviewed.

Extraction is written *here*, with stdlib `html.parser` and plain text splitting, rather
than borrowed from the renderer. A comparison that used the renderer's own extractor would
only prove the module agrees with itself.
"""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from html.parser import HTMLParser

import pytest
from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from adapters.permission.role_permission import RolePermissionService
from core.agents.authoring import create_agent
from core.agents.seed import seed_dev_agent
from core.assembler.visibility import seed_default_scopes
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.reporting.pipeline import ReportRow, generate_report, mark_reviewed, render_redaction_stub
from core.reporting.render import (
    DRAFT_NOTICE,
    PdfRenderingUnavailableError,
    pdf_available,
    render,
    render_epub,
    render_pdf,
    with_draft_notice,
)
from core.reporting.templates import ReportTemplate, Step, get_template
from core.sessions.models import SessionEventRow
from core.tenancy.models import Principal, Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from tests.isolation.test_report_pipeline import _ScriptedProvider  # noqa: F401

_PERMISSIONS = RolePermissionService()
_TEXT_FORMATS = ("markdown", "html", "epub")


class _TextExtractor(HTMLParser):
    """Visible text only: tags dropped, entities resolved by the parser. Deliberately naive
    -- the comparison it feeds is about whether the *words* survived a format change, not
    about whether the markup is pretty."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._suppressed = 0
        self._in_body = False

    def handle_starttag(self, tag: str, attrs: object) -> None:
        # `style`/`script` contents are markup machinery, not words on the page; `<title>`
        # is document metadata a reader sees in a tab or an e-reader's shelf, not in the
        # report. Counting either would make this comparison fail on a stylesheet change
        # and pass on a dropped sentence.
        if tag in ("style", "script"):
            self._suppressed += 1
        if tag == "body":
            self._in_body = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script") and self._suppressed:
            self._suppressed -= 1
        if tag == "body":
            self._in_body = False

    def handle_data(self, data: str) -> None:
        if self._in_body and not self._suppressed:
            self.parts.append(data)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def _extract(output_format: str, data: bytes) -> str:
    if output_format == "markdown":
        # Strip the markdown syntax characters that carry no words, so the three formats
        # are compared on content rather than on notation.
        text = re.sub(r"[#>*_`]", " ", data.decode())
        return " ".join(text.split())
    if output_format == "html":
        parser = _TextExtractor()
        parser.feed(data.decode())
        return parser.text
    if output_format == "epub":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xhtml = archive.read("OEBPS/report.xhtml").decode()
        parser = _TextExtractor()
        parser.feed(xhtml)
        return parser.text
    raise AssertionError(f"no extractor for {output_format!r}")


def _phase() -> PhaseSpec:
    return PhaseSpec(
        label_key="turn",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=[],
            scopes=["workspace_public"],
            entity_fields="all",
            secrets="none",
        ),
        budget=BudgetSpec(ratio={}, max_tokens=400),
    )


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _member(tenant_id: uuid.UUID, workspace_id: uuid.UUID, role: str) -> Principal:
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name=role)
        session.add(principal)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal.id,
                role=role,
            )
        )
        await session.flush()
        session.expunge(principal)
        return principal


async def _report_with_redaction(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, viewer: Principal
) -> ReportRow:
    """A sanitised report over a session with prose: the prose is dropped, so the report
    carries a real redaction stub for the formats to preserve."""
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, f"ren-{uuid.uuid4().hex[:6]}", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    async with tenant_scope(tenant_id) as session:
        for seq in range(2):
            session.add(
                SessionEventRow(
                    tenant_id=tenant_id,
                    session_id=sess.id,
                    event_seq=seq,
                    kind="message",
                    payload={"phase": "turn", "content": f"message {seq}"},
                )
            )

    template = ReportTemplate(
        key="sanitised_log",
        label_key="report.sanitised",
        audience_mode="sanitised",
        pipeline=[Step(kind="fact_frame"), Step(kind="render")],
        output_formats=["markdown", "html", "epub"],
    )
    result = await generate_report(
        tenant_id,
        workspace_id,
        sess.id,
        viewer,
        _phase(),
        template,
        from_event_seq=0,
        to_event_seq=10,
        agent=profile,
        provider=_ScriptedProvider("unused"),
        permission_service=_PERMISSIONS,
    )
    assert result.redactions
    async with tenant_scope(tenant_id) as session:
        row = await session.get(ReportRow, result.report_id)
        assert row is not None
        session.expunge(row)
        return row


async def test_all_formats_carry_identical_content_and_visible_stubs(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    viewer = await _member(tenant_id, workspace_id, "participant")
    report = await _report_with_redaction(tenant_id, workspace_id, viewer)

    stub = render_redaction_stub(2)
    assert stub in report.content_md

    extracted = {
        fmt: _extract(fmt, render(report.content_md, fmt, title="report.sanitised").data)
        for fmt in _TEXT_FORMATS
    }

    # Identical text content across every format -- word for word, after notation is
    # stripped. A format that reworded, reordered, or dropped anything fails here.
    reference = extracted["markdown"]
    for fmt, text in extracted.items():
        assert text == reference, (
            f"{fmt} rendered different text content than markdown:\n{text!r}\nvs\n{reference!r}"
        )

    # And the stub survives visibly in each -- the words of it, not a CSS class that a
    # replaced stylesheet would silently drop.
    stub_words = " ".join(re.sub(r"[#>*_`\[\]]", " ", stub).split())
    for fmt, text in extracted.items():
        assert stub_words in text, f"the redaction stub vanished from {fmt}"

    # EPUB is structurally valid: `mimetype` first and stored uncompressed, which is how a
    # reader identifies the file at all.
    epub = render_epub(report.content_md, title="report.sanitised").data
    with zipfile.ZipFile(io.BytesIO(epub)) as archive:
        names = archive.namelist()
        assert names[0] == "mimetype"
        assert archive.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
        assert {"META-INF/container.xml", "OEBPS/content.opf", "OEBPS/nav.xhtml"} <= set(names)

    if pdf_available():  # pragma: no cover -- depends on system libraries
        pdf = render_pdf(report.content_md, title="report.sanitised").data
        assert pdf.startswith(b"%PDF-")
    else:
        with pytest.raises(PdfRenderingUnavailableError, match="WeasyPrint"):
            render_pdf(report.content_md, title="report.sanitised")


def test_the_draft_notice_is_content_not_styling() -> None:
    """A watermark in CSS is a property of the stylesheet, and a stylesheet is exactly what
    gets replaced. The notice goes into the Markdown, so every format carries it."""
    body = "# Report\n\nSomething happened."
    reviewed = with_draft_notice(body, reviewed=True, requires_review=True)
    unreviewed = with_draft_notice(body, reviewed=False, requires_review=True)
    not_required = with_draft_notice(body, reviewed=False, requires_review=False)

    assert DRAFT_NOTICE not in reviewed
    assert DRAFT_NOTICE not in not_required
    assert DRAFT_NOTICE in unreviewed

    for fmt in _TEXT_FORMATS:
        text = _extract(fmt, render(unreviewed, fmt, title="t").data)
        assert DRAFT_NOTICE.split("—")[0].strip() in text, f"the draft notice vanished from {fmt}"


async def test_report_artifacts_are_scoped_to_their_target_principal(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The service-level half of the criterion: a report records *who it was generated
    for*, and that is the fact the HTTP layer authorises against. The route's 403 is
    exercised in ``packages/api/tests/test_reports_flow.py``; what matters here is that the
    fact exists on the row and is not derivable from anything else."""
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    target = await _member(tenant_id, workspace_id, "participant")
    other = await _member(tenant_id, workspace_id, "participant")
    report = await _report_with_redaction(tenant_id, workspace_id, target)

    assert report.generated_for_principal_id == target.id
    assert report.generated_for_principal_id != other.id

    # Two principals with the same role still get different reports: the scoping is by
    # principal, not by role, because visibility is.
    other_report = await _report_with_redaction(tenant_id, workspace_id, other)
    assert other_report.id != report.id
    assert other_report.generated_for_principal_id == other.id


async def test_review_gate_blocks_unreviewed_downloads(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    viewer = await _member(tenant_id, workspace_id, "overseer")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, f"gate-{uuid.uuid4().hex[:6]}", "echo", "echo-1", encryptor=IdentityEncryptor()
    )

    template = get_template("decision_summary")
    assert template.requires_review is True

    result = await generate_report(
        tenant_id,
        workspace_id,
        sess.id,
        viewer,
        _phase(),
        template,
        from_event_seq=0,
        to_event_seq=10,
        agent=profile,
        provider=_ScriptedProvider("A decision was reached."),
        permission_service=_PERMISSIONS,
    )

    async with tenant_scope(tenant_id) as session:
        row = await session.get(ReportRow, result.report_id)
        assert row is not None
        assert row.reviewed_at is None
        session.expunge(row)

    # Unreviewed: it still *renders*, carrying the notice -- a reviewer has to be able to
    # read what they are approving. The download is what the gate blocks (route-level).
    draft = with_draft_notice(row.content_md, reviewed=False, requires_review=True)
    assert DRAFT_NOTICE in draft
    for fmt in ("markdown", "html"):
        assert DRAFT_NOTICE.split("—")[0].strip() in _extract(
            fmt, render(draft, fmt, title=template.label_key).data
        )

    reviewed = await mark_reviewed(tenant_id, result.report_id, viewer.id)
    assert reviewed.reviewed_by == viewer.id
    assert reviewed.reviewed_at is not None

    final = with_draft_notice(reviewed.content_md, reviewed=True, requires_review=True)
    assert DRAFT_NOTICE not in final


def test_a_report_sends_the_connection_key_like_every_other_model_call() -> None:
    """A report is a model call, and it was the only one going out without a key.

    GenerationRequest has carried an api_key field all along; the report path never
    filled it. So a deployment whose provider needs a key got an authentication error
    from a connection that had just run a whole session successfully, with the sealed
    credential sitting unread two fields away on the same agent row. Nothing in the
    error pointed at the report path -- it looked like a bad key.
    """
    import inspect

    from core.reporting import pipeline

    src = inspect.getsource(pipeline)
    assert "api_key=api_key" in src, "the request must carry the key"
    assert "api_key: str | None = None" in src, "generate_report must accept one"


def test_the_worker_decrypts_the_credential_rather_than_passing_the_pointer() -> None:
    """credential_ref points into the secret manager and is never itself a key.
    Resolution belongs to the composition root that holds the encryptor."""
    import inspect

    from worker import reports

    src = inspect.getsource(reports)
    assert "resolve_connection_api_key(" in src
    assert "get_encryptor()" in src


def test_history_summarisation_sends_the_connection_key_too() -> None:
    """The third model call that was going out unauthenticated.

    Summarising a long transcript failed with "Missing Anthropic API Key" on every
    Anthropic-backed persona. Because a summary is an enrichment the caller logs a
    warning and carries on, so nothing broke visibly -- a long session just quietly lost
    its history and the turns got worse for a reason no one could see.
    """
    import inspect

    from core.process import live_session
    from core.sessions import history

    assert "api_key: str | None = None" in inspect.getsource(history.summarise_history)
    assert "api_key=api_key" in inspect.getsource(history)
    assert "resolve_connection_api_key(" in inspect.getsource(live_session)


def test_reviewing_a_report_does_not_reuse_the_draft_render() -> None:
    """The draft notice and the idempotency guard are both right, and wired together one
    defeated the other.

    ``render_report`` was keyed on ``(report, format)``. So: render a review-required
    report, get a PDF stamped DRAFT, review it, ask for the PDF again -- and the second
    call returned the first call's recorded result without rendering anything. The stored
    artifact stayed stamped DRAFT permanently, while the review gate opened the download.
    What a reviewer unlocked was a file saying it had not been reviewed.

    The review state belongs in the key because it changes the artifact.
    """
    from worker.render_reports import _render_job_key

    report_id = uuid.uuid4()
    draft_key = _render_job_key(report_id=report_id, output_format="pdf", reviewed=False)
    reviewed_key = _render_job_key(report_id=report_id, output_format="pdf", reviewed=True)

    assert draft_key != reviewed_key

    # Still idempotent within a state: asking twice for the same thing is one operation.
    assert draft_key == _render_job_key(report_id=report_id, output_format="pdf", reviewed=False)
    # And still per-format.
    assert reviewed_key != _render_job_key(
        report_id=report_id, output_format="markdown", reviewed=True
    )
