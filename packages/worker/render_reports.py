"""Report rendering job -- the worker-side wiring `core.reporting.render` needs but
can't import itself (composition root: which `BlobStore`). Registered in `worker.main`
under kind `"render_report"`.

Rendering is a separate job from generation on purpose: a report's *content* is decided
once, by the pipeline, and rendering it into another format must not be able to change it.
Two jobs make that structural -- this one reads `content_md` and writes bytes, and has no
path to the pipeline at all.

A format the template never declared is not rendered. A caller asking for one is asking for
an artifact the report's own definition says it does not have, and quietly producing it
would make `output_formats` decorative.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.actions.idempotency import idempotent
from core.reporting.pipeline import get_report
from core.reporting.render import PdfRenderingUnavailableError, render, with_draft_notice
from core.reporting.templates import get_template
from core.tenancy.scope import tenant_scope
from worker.blob_store_factory import get_blob_store


def _render_job_key(**kwargs: Any) -> str:
    # The review state is part of the key because it changes the artifact. A
    # review-required report renders with a DRAFT notice across it; reviewing it is
    # precisely the act that should produce a clean one. Keyed on (report, format)
    # alone, the second render returned the first render's recorded result and the
    # stored artifact stayed stamped DRAFT for good -- with the download gate open, so
    # what a reviewer unlocked was a file marked as not yet reviewed.
    state = "reviewed" if kwargs.get("reviewed") else "draft"
    return f"render_report:{kwargs['report_id']}:{kwargs['output_format']}:{state}"


@idempotent(key_fn=_render_job_key)
async def run_render_job(
    *,
    tenant_id: uuid.UUID,
    report_id: uuid.UUID,
    output_format: str,
    reviewed: bool = False,
) -> dict[str, Any]:
    report = await get_report(tenant_id, report_id)
    if report is None:
        raise ValueError(f"no report {report_id} in this tenant")
    template = get_template(report.template_key)
    if output_format not in template.output_formats:
        raise ValueError(
            f"template {template.key!r} declares formats {list(template.output_formats)}; "
            f"{output_format!r} is not one of them"
        )

    content = with_draft_notice(
        report.content_md,
        reviewed=report.reviewed_at is not None,
        requires_review=template.requires_review,
    )
    try:
        artifact = render(content, output_format, title=template.label_key)
    except PdfRenderingUnavailableError as exc:
        # Surfaced, never substituted: a file that opens wrong days later with no clue why
        # is worse than a job that failed loudly now.
        return {"rendered": False, "format": output_format, "reason": str(exc)}

    blob_key = f"reports/{tenant_id}/{report_id}/{artifact.filename}"
    await get_blob_store().put(blob_key, artifact.data, content_type=artifact.media_type)

    async with tenant_scope(tenant_id) as session:
        row = await get_report_in_session(session, report_id)
        row.artifacts = {
            **dict(row.artifacts),
            output_format: {
                "blob_key": blob_key,
                "media_type": artifact.media_type,
                "bytes": len(artifact.data),
                "draft": template.requires_review and report.reviewed_at is None,
            },
        }

    return {
        "rendered": True,
        "format": output_format,
        "blob_key": blob_key,
        "bytes": len(artifact.data),
    }


async def get_report_in_session(session: Any, report_id: uuid.UUID) -> Any:
    from core.reporting.pipeline import ReportRow

    row = await session.get(ReportRow, report_id)
    if row is None:
        raise ValueError(f"no report {report_id} in this tenant")
    return row


async def handle_render_report(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = uuid.UUID(payload["tenant_id"])
    report_id = uuid.UUID(payload["report_id"])
    # Read here rather than trusting the payload: a report can be reviewed between the
    # render being asked for and the worker picking it up, and the artifact should
    # reflect the state it is rendered in.
    report = await get_report(tenant_id, report_id)
    return await run_render_job(
        tenant_id=tenant_id,
        report_id=report_id,
        output_format=str(payload["output_format"]),
        reviewed=report is not None and report.reviewed_at is not None,
    )
