"""Report rendering: `report.content_md` to Markdown, HTML,
EPUB, and PDF.

**Rendering adds format, never content.** Every renderer takes the same `content_md` and
the same redaction stubs, and the acceptance test compares the *extracted text* of each
output. A format that dropped, reworded, or restyled a stub into invisibility would fail
that comparison -- which is the point, because a redaction stub that survives in Markdown
and vanishes in the PDF someone actually hands round is worse than no stub at all.

**The draft watermark is content, not decoration.** An unreviewed report from a
review-required template renders with a visible DRAFT line in *every* format, including
Markdown. Putting it in CSS would make it a property of the stylesheet, and a stylesheet
is exactly what gets replaced.

**PDF needs WeasyPrint, which needs system libraries** (pango, cairo, gobject). Where those
are absent -- as in this development environment -- `render_pdf` raises a named error
rather than producing something PDF-shaped. A renderer that silently emitted HTML with a
`.pdf` name would be a file that opens wrong on someone's machine, days later, with no
clue why. Markdown, HTML, and EPUB are pure-Python and always available.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime

from markdown_it import MarkdownIt

DRAFT_NOTICE = "DRAFT — this report has not been reviewed."

_STYLESHEET = """
body { font-family: Georgia, serif; line-height: 1.5; margin: 2rem; }
h1, h2 { font-family: Helvetica, Arial, sans-serif; }
blockquote { border-left: 3px solid #999; margin-left: 0; padding-left: 1rem;
             color: #555; font-style: italic; }
.draft { border: 2px solid #b00; color: #b00; padding: .5rem; font-weight: bold; }
@page { size: A4; margin: 20mm; }
"""


class PdfRenderingUnavailableError(RuntimeError):
    """WeasyPrint (or its system libraries) is not installed. Named rather than swallowed:
    a caller can tell "this deployment cannot make PDFs" from "this report failed"."""


@dataclass(frozen=True)
class RenderedArtifact:
    format: str
    media_type: str
    data: bytes
    filename: str


def with_draft_notice(content_md: str, *, reviewed: bool, requires_review: bool) -> str:
    """Prepends the draft notice when a review-required report has not been reviewed.

    Applied to the *Markdown*, before any format-specific rendering, so all four outputs
    carry it identically and none of them can be the one that forgets."""
    if reviewed or not requires_review:
        return content_md
    return f"> **{DRAFT_NOTICE}**\n\n{content_md}"


def render_markdown(content_md: str) -> RenderedArtifact:
    return RenderedArtifact(
        format="markdown",
        media_type="text/markdown; charset=utf-8",
        data=content_md.encode(),
        filename="report.md",
    )


def markdown_to_html_fragment(content_md: str) -> str:
    """markdown-it-py in `commonmark` mode: a deterministic, spec-defined conversion, so
    two renders of the same report produce identical bytes. Deterministic output is what
    lets a caller hash an artifact and mean something by it, the same reason `.pyr` bundles
    are byte-stable."""
    return str(MarkdownIt("commonmark").render(content_md))


def render_html(content_md: str, *, title: str) -> RenderedArtifact:
    body = markdown_to_html_fragment(content_md)
    document = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>{_escape(title)}</title><style>{_STYLESHEET}</style></head>"
        f"<body>{body}</body></html>"
    )
    return RenderedArtifact(
        format="html",
        media_type="text/html; charset=utf-8",
        data=document.encode(),
        filename="report.html",
    )


def render_epub(content_md: str, *, title: str) -> RenderedArtifact:
    """A minimal, valid EPUB 3 built with stdlib `zipfile` -- no dependency, because an
    EPUB *is* a ZIP with three known files and a content document, and a library for that
    would be more surface than the format has.

    `mimetype` is written first and **stored uncompressed**, which the spec requires: a
    reader identifies an EPUB by finding that exact byte sequence at a fixed offset, and a
    deflated one produces a file that fails to open with no useful message."""
    xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">'
        f"<head><title>{_escape(title)}</title>"
        f"<style>{_STYLESHEET}</style></head>"
        f"<body>{markdown_to_html_fragment(content_md)}</body></html>"
    )
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        "<head><title>Contents</title></head><body>"
        '<nav epub:type="toc"><ol><li><a href="report.xhtml">'
        f"{_escape(title)}</a></li></ol></nav></body></html>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="id">urn:pyrrhula:report</dc:identifier>'
        f"<dc:title>{_escape(title)}</dc:title><dc:language>en</dc:language>"
        f'<meta property="dcterms:modified">{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}</meta>'
        "</metadata><manifest>"
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        '<item id="content" href="report.xhtml" media-type="application/xhtml+xml"/>'
        '</manifest><spine><itemref idref="content"/></spine></package>'
    )
    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0)),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        for path, body in (
            ("META-INF/container.xml", container),
            ("OEBPS/content.opf", opf),
            ("OEBPS/nav.xhtml", nav),
            ("OEBPS/report.xhtml", xhtml),
        ):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body)

    return RenderedArtifact(
        format="epub",
        media_type="application/epub+zip",
        data=buffer.getvalue(),
        filename="report.epub",
    )


def pdf_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except Exception:  # pragma: no cover -- environment-dependent by nature
        return False
    return True


def render_pdf(content_md: str, *, title: str) -> RenderedArtifact:
    """WeasyPrint over the same HTML `render_html` produces, so the PDF and the HTML are
    the same document twice rather than two documents that happen to agree."""
    if not pdf_available():
        raise PdfRenderingUnavailableError(
            "PDF rendering requires WeasyPrint and its system libraries (pango, cairo, "
            "gobject). This deployment has neither; Markdown, HTML, and EPUB are "
            "unaffected."
        )
    from weasyprint import HTML  # pragma: no cover -- unreachable without the dependency

    html = render_html(content_md, title=title).data.decode()
    return RenderedArtifact(  # pragma: no cover -- see above
        format="pdf",
        media_type="application/pdf",
        # presentational_hints: without it WeasyPrint ignores `<ol start>`, so a document
        # with several numbered lists restarts each at 1 (the launch plan's sections 2-5
        # all came out as "1.").
        data=HTML(string=html).write_pdf(presentational_hints=True),
        filename="report.pdf",
    )


def render(content_md: str, output_format: str, *, title: str) -> RenderedArtifact:
    """One dispatch point, so a caller cannot render a format the template never declared
    by reaching past it to a specific function."""
    if output_format == "markdown":
        return render_markdown(content_md)
    if output_format == "html":
        return render_html(content_md, title=title)
    if output_format == "epub":
        return render_epub(content_md, title=title)
    if output_format == "pdf":
        return render_pdf(content_md, title=title)
    raise ValueError(f"unknown output format {output_format!r}")


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
