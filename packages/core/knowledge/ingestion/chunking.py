"""Entry body -> retrieval-ready chunks: paragraph-aware, targeting
~400 tokens with overlap so a chunk boundary rarely falls mid-idea. Token counting is
injected (``count_tokens``) rather than imported directly — this is ``core``, and tokenizer
selection is an adapter concern (``ModelProvider.count_tokens``); the worker-side job
handler supplies the concrete callback.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

CountTokens = Callable[[str], int]

_DEFAULT_TARGET_TOKENS = 400
_DEFAULT_OVERLAP_TOKENS = 40


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    token_count: int


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _split_oversized_paragraph(
    paragraph: str, count_tokens: CountTokens, target_tokens: int
) -> list[str]:
    """A single paragraph that alone exceeds target_tokens (rare, but unbounded chunk
    size would defeat the point of chunking) — fall back to word-count slicing."""
    words = paragraph.split()
    pieces: list[str] = []
    current: list[str] = []
    for word in words:
        current.append(word)
        if count_tokens(" ".join(current)) >= target_tokens:
            pieces.append(" ".join(current))
            current = []
    if current:
        pieces.append(" ".join(current))
    return pieces or [paragraph]


def _overlap_tail(text: str, count_tokens: CountTokens, overlap_tokens: int) -> str:
    """The tail of ``text`` worth approximately ``overlap_tokens`` tokens, cut at a word
    boundary — prepended to the next chunk so context doesn't hard-cut mid-idea."""
    words = text.split()
    for start in range(len(words)):
        candidate = " ".join(words[start:])
        if count_tokens(candidate) <= overlap_tokens:
            return candidate
    return ""


def chunk_text(
    text: str,
    count_tokens: CountTokens,
    *,
    target_tokens: int = _DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = _DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    pieces: list[str] = []
    for paragraph in _split_paragraphs(text):
        if count_tokens(paragraph) > target_tokens:
            pieces.extend(_split_oversized_paragraph(paragraph, count_tokens, target_tokens))
        else:
            pieces.append(paragraph)

    if not pieces:
        return []

    chunks: list[str] = []
    current = pieces[0]
    for piece in pieces[1:]:
        candidate = f"{current}\n\n{piece}"
        if count_tokens(candidate) <= target_tokens:
            current = candidate
            continue
        chunks.append(current)
        overlap = _overlap_tail(current, count_tokens, overlap_tokens)
        current = f"{overlap}\n\n{piece}".strip() if overlap else piece
    chunks.append(current)

    return [
        Chunk(ordinal=i, text=chunk, token_count=count_tokens(chunk))
        for i, chunk in enumerate(chunks)
    ]
