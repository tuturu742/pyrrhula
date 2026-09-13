"""C1.4 unit tests for the typed stable/volatile layout contract itself."""

from __future__ import annotations

from core.assembler.layout import LayoutSections


def test_render_orders_stable_before_volatile() -> None:
    sections = LayoutSections(stable=("A", "B"), volatile=("C", "D"))
    rendered = sections.render()
    assert rendered.index("A") < rendered.index("B") < rendered.index("C") < rendered.index("D")


def test_stable_text_and_volatile_text_are_independently_addressable() -> None:
    sections = LayoutSections(stable=("A", "B"), volatile=("C",))
    assert sections.stable_text == "A\n\nB"
    assert sections.volatile_text == "C"
    assert sections.render() == "A\n\nB\n\nC"


def test_empty_blocks_are_skipped_not_rendered_as_blank_gaps() -> None:
    sections = LayoutSections(stable=("", "A", ""), volatile=("", "B"))
    assert sections.stable_text == "A"
    assert sections.volatile_text == "B"
    assert sections.render() == "A\n\nB"


def test_all_stable_or_all_volatile_renders_without_a_stray_separator() -> None:
    assert LayoutSections(stable=("A",), volatile=()).render() == "A"
    assert LayoutSections(stable=(), volatile=("A",)).render() == "A"
    assert LayoutSections(stable=(), volatile=()).render() == ""
