"""An in-memory index of the shipped documentation: pages split into sections by heading,
scored lexically. Built once per process on first use.

Why not ``core.knowledge.ingestion.parsers.parse_markdown``: it is not fence-aware (several
pages carry ``#`` lines inside shell blocks), and it drops the heading level and path,
which is what lets an answer cite "configuration › Settings in the product". Changing it
would also change the entry keys of every rulebook already ingested through it.
"""

from __future__ import annotations

import functools
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_MARKER = "configuration.md"
_EXTRA_PAGES = ("README.md", "FAQ.md")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_./-]*")
# A term a line *defines* -- a table row or list item that opens with it in backticks --
# outranks a line that merely mentions it, so a setting's own reference row wins.
_DEFINED_RE = re.compile(r"^(?:\|\s*|[-*]\s+)?`([^`]+)`", re.M)
_SNIPPET_CHARS = 240


def docs_root() -> Path | None:
    """The in-image copy first, else the repository checkout; ``None`` when neither
    exists (the tools then say so instead of failing)."""
    for candidate in (
        Path("/app/docs"),
        Path(__file__).resolve().parents[3] / "docs",
    ):
        if (candidate / _MARKER).is_file():
            return candidate
    return None


def page_files(root: Path) -> list[tuple[str, Path]]:
    """``(page key, path)`` for every page: the top-level ``*.md``, ``builders/*.md``, and
    README/FAQ from the image copy (inside ``root``) or the checkout (beside it)."""
    pages: list[tuple[str, Path]] = []
    extra_names = {name.lower() for name in _EXTRA_PAGES}
    for path in sorted(root.glob("*.md")):
        if path.name.lower() in extra_names:
            continue
        pages.append((path.stem, path))
    for path in sorted(root.glob("builders/*.md")):
        pages.append((f"builders/{path.stem}", path))
    for name in _EXTRA_PAGES:
        for path in (root / name, root.parent / name):
            if path.is_file():
                pages.append((name[: -len(".md")].lower(), path))
                break
    return pages


@dataclass(frozen=True)
class Section:
    page: str
    title: str
    heading_path: tuple[str, ...]
    level: int
    anchor: str
    body: str


def _anchor(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9 -]", "", heading.lower().replace("`", ""))
    return re.sub(r"\s+", "-", slug.strip())


def split_sections(text: str, page: str) -> list[Section]:
    """Split a page on its headings, ignoring anything inside a fenced code block. The
    text before the first heading is a level-0 preamble (dropped when empty)."""
    lines = text.splitlines()
    title = page
    for line in lines:
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) == 1:
            title = m.group(2).strip()
            break

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    cursor = _Cursor(level=0, path=(title,), anchor="")
    body: list[str] = []
    fence_char = ""
    fence_len = 0

    def flush() -> None:
        content = "\n".join(body).strip()
        if content:
            sections.append(
                Section(
                    page=page,
                    title=title,
                    heading_path=cursor.path,
                    level=cursor.level,
                    anchor=cursor.anchor,
                    body=content,
                )
            )
        body.clear()

    for line in lines:
        opener = _FENCE_RE.match(line)
        if opener:
            run = opener.group(1)
            if not fence_char:
                fence_char, fence_len = run[0], len(run)
            elif run[0] == fence_char and len(run) >= fence_len:
                fence_char, fence_len = "", 0
            body.append(line)
            continue
        if fence_char:
            body.append(line)
            continue
        m = _HEADING_RE.match(line)
        if not m:
            body.append(line)
            continue
        flush()
        level = len(m.group(1))
        heading = m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        cursor = _Cursor(level=level, path=tuple(h for _, h in stack), anchor=_anchor(heading))
    flush()
    return sections


@dataclass(frozen=True)
class _Cursor:
    level: int
    path: tuple[str, ...]
    anchor: str


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass
class DocsIndex:
    sections: list[Section]
    terms: list[Counter[str]]
    head_terms: list[set[str]]
    defined_terms: list[set[str]]
    pages: dict[str, str]

    @classmethod
    def build(cls, root: Path | None) -> DocsIndex:
        sections: list[Section] = []
        pages: dict[str, str] = {}
        if root is not None:
            for page, path in page_files(root):
                page_sections = split_sections(path.read_text(encoding="utf-8"), page)
                sections.extend(page_sections)
                pages[page] = page_sections[0].title if page_sections else page
        terms = [Counter(tokenize(s.body)) for s in sections]
        head_terms = [
            set(tokenize(" ".join(s.heading_path))) | set(tokenize(s.page)) for s in sections
        ]
        defined_terms = [
            {t for m in _DEFINED_RE.finditer(s.body) for t in tokenize(m.group(1))}
            for s in sections
        ]
        return cls(
            sections=sections,
            terms=terms,
            head_terms=head_terms,
            defined_terms=defined_terms,
            pages=pages,
        )


@functools.lru_cache(maxsize=1)
def get_index() -> DocsIndex:
    return DocsIndex.build(docs_root())


@dataclass(frozen=True)
class Hit:
    page: str
    heading_path: tuple[str, ...]
    anchor: str
    snippet: str
    score: float


def _snippet(body: str, tokens: list[str]) -> str:
    lowered = body.lower()
    position = min((p for p in (lowered.find(t) for t in tokens) if p >= 0), default=0)
    start = max(0, position - _SNIPPET_CHARS // 3)
    window = body[start : start + _SNIPPET_CHARS]
    window = re.sub(r"\s+", " ", window).strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + _SNIPPET_CHARS < len(body) else ""
    return f"{prefix}{window}{suffix}"


def search(query: str, *, limit: int = 8, index: DocsIndex | None = None) -> list[Hit]:
    """Lexical scoring: term frequency in the body, with a boost for terms in the heading
    path or page name, and a bonus when the whole query appears verbatim."""
    index = index or get_index()
    tokens = list(dict.fromkeys(tokenize(query)))
    if not tokens:
        return []
    phrase = query.strip().lower()
    scored: list[tuple[float, int]] = []
    for i, section in enumerate(index.sections):
        counts = index.terms[i]
        heads = index.head_terms[i]
        score = 0.0
        for token in tokens:
            tf = counts.get(token, 0)
            if tf:
                score += 1.0 + math.log(tf)
            if token in heads:
                score += 3.0
            if token in index.defined_terms[i]:
                score += 4.0
        if len(phrase) >= 6 and phrase in section.body.lower():
            score += 4.0
        if score > 0:
            scored.append((score, i))
    scored.sort(key=lambda item: (-item[0], index.sections[item[1]].page, item[1]))
    hits = []
    for score, i in scored[:limit]:
        section = index.sections[i]
        hits.append(
            Hit(
                page=section.page,
                heading_path=section.heading_path,
                anchor=section.anchor,
                snippet=_snippet(section.body, tokens),
                score=round(score, 2),
            )
        )
    return hits


def _normalise_page(page: str) -> str:
    key = page.strip().lower()
    for prefix in ("docs/", "./"):
        if key.startswith(prefix):
            key = key[len(prefix) :]
    if key.endswith(".md"):
        key = key[:-3]
    return key


def read_page(
    page: str,
    section: str | None = None,
    *,
    max_chars: int = 16000,
    index: DocsIndex | None = None,
) -> dict[str, object]:
    """One section (with its sub-sections) or the whole page, as ``{"page", "section",
    "text"}``; ``{"error": ...}`` for an unknown page, ``{"candidates": [...]}`` when the
    section name is ambiguous."""
    index = index or get_index()
    key = _normalise_page(page)
    sections = [s for s in index.sections if s.page == key]
    if not sections:
        return {"error": f"no documentation page {page!r}", "pages": sorted(index.pages)}

    if section is None:
        text = "\n\n".join(_render(s) for s in sections)
        if len(text) > max_chars:
            text = text[:max_chars] + (
                f"\n\n[truncated: {len(text) - max_chars} more characters -- ask for a section]"
            )
        return {"page": key, "section": None, "text": text}

    wanted = section.strip().lower()
    matches = [i for i, s in enumerate(sections) if s.anchor == _anchor(wanted)]
    if not matches:
        matches = [
            i
            for i, s in enumerate(sections)
            if s.heading_path and s.heading_path[-1].lower() == wanted
        ]
    if not matches:
        matches = [
            i
            for i, s in enumerate(sections)
            if s.heading_path and wanted in s.heading_path[-1].lower()
        ]
    if len(matches) != 1:
        return {
            "candidates": [" › ".join(sections[i].heading_path) for i in matches]
            or [" › ".join(s.heading_path) for s in sections if s.level],
        }
    start = matches[0]
    level = sections[start].level
    end = start + 1
    while end < len(sections) and sections[end].level > level:
        end += 1
    text = "\n\n".join(_render(s) for s in sections[start:end])
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[truncated: {len(text) - max_chars} more characters]"
    return {"page": key, "section": " › ".join(sections[start].heading_path), "text": text}


def _render(section: Section) -> str:
    if section.level == 0:
        return section.body
    return f"{'#' * section.level} {section.heading_path[-1]}\n\n{section.body}"


def page_index_line(index: DocsIndex | None = None) -> str:
    index = index or get_index()
    if not index.pages:
        return "No product documentation is shipped with this deployment."
    return "Product documentation pages: " + "; ".join(
        f"{key} — {title}" for key, title in index.pages.items()
    )
