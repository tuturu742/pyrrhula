"""Document -> entries (plan §6.1, A1.2). Markdown splits on headings (a real content
structure the author already chose); plain text and PDF have none, so both fall back to
the same paragraph-grouping heuristic — "heuristic for txt/pdf" per the task spec. Split
points are just entry boundaries, not stored separately: the author can freely re-split
in the authoring UI later (D1.1) by editing the resulting entries, same as any other edit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# Heuristic entry size for headingless text (plain text/PDF): group paragraphs until this
# many characters, then start a new entry. Not the ~400-token *chunk* target (chunking.py) —
# entries are coarser; chunking happens within each entry afterward.
_HEADINGLESS_ENTRY_CHARS = 2000


class UnsupportedDocumentError(Exception):
    """No usable text could be extracted (e.g. a scanned/image-only PDF) — ingestion
    rejects this with a clear message rather than silently producing zero entries."""


@dataclass(frozen=True)
class ParsedEntry:
    entry_key: str
    title: str
    body_md: str


def _slugify(text: str, *, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or fallback


def _dedupe_keys(keys: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for key in keys:
        if key not in seen:
            seen[key] = 0
            result.append(key)
        else:
            seen[key] += 1
            result.append(f"{key}-{seen[key]}")
    return result


def parse_markdown(text: str) -> list[ParsedEntry]:
    headings = list(_HEADING_RE.finditer(text))
    if not headings:
        return parse_plain_text(text)

    sections: list[tuple[str, str]] = []
    preamble = text[: headings[0].start()].strip()
    if preamble:
        sections.append(("Preamble", preamble))

    for i, match in enumerate(headings):
        title = match.group(2).strip()
        start = match.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[start:end].strip()
        sections.append((title, body))

    keys = _dedupe_keys(
        [_slugify(title, fallback=f"section-{i}") for i, (title, _) in enumerate(sections)]
    )
    return [
        ParsedEntry(entry_key=key, title=title, body_md=body)
        for key, (title, body) in zip(keys, sections, strict=True)
        if body
    ]


def parse_plain_text(text: str) -> list[ParsedEntry]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        raise UnsupportedDocumentError("document contains no extractable text")

    groups: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if current and current_len + len(para) > _HEADINGLESS_ENTRY_CHARS:
            groups.append(current)
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para)
    if current:
        groups.append(current)

    return [
        ParsedEntry(
            entry_key=f"section-{i + 1}",
            title=f"Section {i + 1}",
            body_md="\n\n".join(group),
        )
        for i, group in enumerate(groups)
    ]


def parse_pdf(data: bytes) -> list[ParsedEntry]:
    import io

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except PdfReadError as exc:
        raise UnsupportedDocumentError(f"could not read PDF: {exc}") from exc

    if not text.strip():
        raise UnsupportedDocumentError(
            "PDF contains no extractable text (scanned/image-only PDFs are out of scope)"
        )
    return parse_plain_text(text)
