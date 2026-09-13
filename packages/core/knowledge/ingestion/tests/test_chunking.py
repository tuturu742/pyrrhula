"""Pure unit tests (no DB) for the chunker. Uses a trivial word-count tokenizer so token
math is exact and assertions aren't tied to tiktoken's specific encoding.
"""

from __future__ import annotations

from core.knowledge.ingestion.chunking import chunk_text


def _word_count(text: str) -> int:
    return len(text.split())


def test_single_short_paragraph_is_one_chunk() -> None:
    chunks = chunk_text("Roll 1d20 plus your STR modifier.", _word_count, target_tokens=400)
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0
    assert chunks[0].token_count == _word_count(chunks[0].text)


def test_packs_multiple_small_paragraphs_into_one_chunk_under_budget() -> None:
    text = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."
    chunks = chunk_text(text, _word_count, target_tokens=400)
    assert len(chunks) == 1


def test_splits_into_multiple_chunks_once_target_exceeded() -> None:
    # Each paragraph is 10 words; target 15 words -> at most one paragraph fits per chunk
    # once the second paragraph would push a chunk over budget.
    paragraphs = [" ".join([f"w{i}"] * 10) for i in range(5)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, _word_count, target_tokens=15, overlap_tokens=0)
    assert len(chunks) > 1
    for chunk in chunks:
        # Overlap is 0 here, so no chunk should wildly exceed the target.
        assert chunk.token_count <= 15 or chunk.token_count == _word_count(paragraphs[0])


def test_overlap_carries_tail_of_previous_chunk_into_next() -> None:
    paragraphs = [" ".join([f"w{i}-{j}" for j in range(10)]) for i in range(4)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, _word_count, target_tokens=15, overlap_tokens=5)
    assert len(chunks) > 1
    # The second chunk should start with some tail words from the first chunk's text.
    first_words = chunks[0].text.split()
    second_words = chunks[1].text.split()
    assert any(w in second_words for w in first_words[-5:])


def test_oversized_single_paragraph_is_split_by_word_count() -> None:
    huge_paragraph = " ".join(f"word{i}" for i in range(200))
    chunks = chunk_text(huge_paragraph, _word_count, target_tokens=50, overlap_tokens=0)
    assert len(chunks) >= 4
    for chunk in chunks:
        assert chunk.token_count <= 50


def test_empty_text_produces_no_chunks() -> None:
    assert chunk_text("   \n\n  ", _word_count) == []


def test_ordinals_are_sequential_from_zero() -> None:
    paragraphs = [" ".join([f"w{i}"] * 10) for i in range(5)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, _word_count, target_tokens=15, overlap_tokens=0)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
