"""Pure unit tests (no DB) for the document -> entries parsers."""

from __future__ import annotations

import pytest

from core.knowledge.ingestion.parsers import (
    UnsupportedDocumentError,
    parse_markdown,
    parse_pdf,
    parse_plain_text,
)


def test_parse_markdown_splits_on_headings() -> None:
    text = "# Grappling\nRoll 1d20+STR.\n\n## Stealth\nRoll 1d20+DEX.\n"
    entries = parse_markdown(text)
    assert [e.title for e in entries] == ["Grappling", "Stealth"]
    assert [e.entry_key for e in entries] == ["grappling", "stealth"]
    assert entries[0].body_md == "Roll 1d20+STR."
    assert entries[1].body_md == "Roll 1d20+DEX."


def test_parse_markdown_captures_preamble_before_first_heading() -> None:
    text = "Some intro text.\n\n# Grappling\nRoll 1d20+STR.\n"
    entries = parse_markdown(text)
    assert entries[0].title == "Preamble"
    assert entries[0].body_md == "Some intro text."
    assert entries[1].title == "Grappling"


def test_parse_markdown_dedupes_colliding_slugs() -> None:
    text = "# Combat\nMelee rules.\n\n# Combat\nRanged rules.\n"
    entries = parse_markdown(text)
    assert [e.entry_key for e in entries] == ["combat", "combat-1"]


def test_parse_markdown_falls_back_to_plain_text_without_headings() -> None:
    text = "Paragraph one.\n\nParagraph two."
    entries = parse_markdown(text)
    assert len(entries) == 1
    assert "Paragraph one." in entries[0].body_md
    assert "Paragraph two." in entries[0].body_md


def test_parse_plain_text_groups_paragraphs_into_sized_entries() -> None:
    big_paragraph = "word " * 500  # ~2500 chars, over the 2000-char grouping threshold
    text = f"{big_paragraph}\n\n{big_paragraph}"
    entries = parse_plain_text(text)
    assert len(entries) == 2
    assert entries[0].entry_key == "section-1"
    assert entries[1].entry_key == "section-2"


def test_parse_plain_text_rejects_empty_document() -> None:
    with pytest.raises(UnsupportedDocumentError):
        parse_plain_text("   \n\n   ")


def _make_pdf_bytes(lines: list[str]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    for line in lines:
        pdf.cell(text=line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def test_parse_pdf_extracts_real_text() -> None:
    data = _make_pdf_bytes(["Grappling", "Roll 1d20+STR."])
    entries = parse_pdf(data)
    assert len(entries) == 1
    assert "Grappling" in entries[0].body_md
    assert "Roll 1d20+STR." in entries[0].body_md


def test_parse_pdf_rejects_pdf_with_no_extractable_text() -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()  # blank page, no text content stream
    data = bytes(pdf.output())

    with pytest.raises(UnsupportedDocumentError):
        parse_pdf(data)
