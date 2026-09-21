"""Model-backed code generation for delegated coding work.

The seam ``GitMcpTransport._generate_files`` reserved: given a work item, the assembled
brief, the repo's current files and (on rework) the review comment, ask a coding model for
the changed files. Output protocol is plain file blocks -- local models handle it far more
reliably than JSON-escaped code:

    ===FILE: relative/path===
    <content>
    ===END===
    ===DELETE: relative/path===

The delete verb exists because the protocol was write-only, and both commit paths write:
a file the model simply omitted stayed exactly where it was. So "remove the dead module"
produced an empty pull request and no explanation -- the one shape of ordinary work the
pipeline could not express. Deletion is a separate verb rather than an inferred absence,
because absence is how a model expresses "I did not need to touch this".

Any failure (model unreachable, unparseable output, empty result) falls back to the caller's
scaffold -- the delegation *flow* must never break on model quality; that is the whole
"working flow now, better models later" contract. Model output is DATA: it becomes file
contents in a branch a human reviews, never instructions to this process.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CodegenOutput:
    """What one codegen call asks the branch to become.

    Deletions travel beside the writes rather than as an absence: omitting a file is how
    a model says "I did not need to touch this", so absence cannot also mean "remove it".
    """

    files: dict[str, str]
    deletes: frozenset[str] = frozenset()


# (work_item, brief, repo_files, rework_comment) -> CodegenOutput; raises on failure.
CodegenFn = Callable[[dict[str, Any], str, dict[str, str], str | None], Awaitable[CodegenOutput]]

_FILE_BLOCK = re.compile(
    r"===FILE:\s*(?P<path>[^=\n]+?)\s*===\s*\n(?P<body>.*?)(?:\n)?===END===",
    re.DOTALL,
)
_DELETE_BLOCK = re.compile(r"===DELETE:\s*(?P<path>[^=\n]+?)\s*===")
_MAX_FILES = 12
# Deletions are cheap to emit and expensive to get wrong, so the ceiling is separate
# from the file budget and deliberately roomier: removing a directory is a normal task.
# Generous on purpose: "remove the tasks directory" is one instruction and 93 files on
# this repository alone. A cap below what a real cleanup names turns a complete answer
# into a half-applied one, which is worse than refusing -- the branch then looks done.
_MAX_DELETES = 1000
_MAX_CONTEXT_FILE_CHARS = 4000
_SAFE_PATH = re.compile(r"[A-Za-z0-9_.@-]+(/[A-Za-z0-9_.@-]+)*")


class CodegenError(Exception):
    pass


def parse_file_blocks(text: str) -> dict[str, str]:
    """Extract ``===FILE:`` blocks. Paths are confined to the repo (no absolute paths, no
    ``..`` segments -- the model names files, it does not choose where they escape to)."""
    files: dict[str, str] = {}
    for match in _FILE_BLOCK.finditer(text):
        path = match.group("path").strip()
        # Reject traversal/absolute BEFORE any normalization (lstrip-style stripping would
        # turn `../evil` into a "valid" path); then drop a single literal `./` prefix.
        if path.startswith("/") or ".." in path.split("/"):
            continue
        path = path.removeprefix("./")
        body = match.group("body")
        # Strip a stray markdown fence the model may have wrapped the body in.
        stripped = body.strip("\n")
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            body = "\n".join(lines)
        if not path or not _SAFE_PATH.fullmatch(path) or ".." in path.split("/"):
            continue
        files[path] = body if body.endswith("\n") else body + "\n"
        if len(files) >= _MAX_FILES:
            break
    return files


def parse_deletions(text: str, *, keep: Iterable[str] = ()) -> set[str]:
    """Extract ``===DELETE:`` paths, under the same confinement as file blocks.

    ``keep`` is the set of paths the same response also wrote. A model that emits both
    for one path has contradicted itself; writing wins, because a delete that silently
    discarded content the model had just produced is the more expensive way to be wrong.
    """
    written = set(keep)
    deletes: set[str] = set()
    for match in _DELETE_BLOCK.finditer(text):
        path = match.group("path").strip()
        # Same order as parse_file_blocks: reject traversal and absolutes BEFORE any
        # normalisation, since stripping would turn `../evil` into a "valid" path.
        if path.startswith("/") or ".." in path.split("/"):
            continue
        path = path.removeprefix("./")
        if not path or not _SAFE_PATH.fullmatch(path) or path in written:
            continue
        deletes.add(path)
        if len(deletes) >= _MAX_DELETES:
            break
    return deletes


def _prompt(
    work_item: dict[str, Any],
    brief: str,
    repo_files: dict[str, str],
    rework_comment: str | None,
) -> str:
    fields = work_item.get("fields") or {}
    listing = "\n".join(f"- {p}" for p in sorted(repo_files)) or "- (empty repo)"
    context_blocks = "\n\n".join(
        f"--- {p} ---\n{c[:_MAX_CONTEXT_FILE_CHARS]}" for p, c in sorted(repo_files.items())
    )
    task = (
        f"Task: {work_item.get('name', 'work item')}\n"
        f"Description: {fields.get('description', '')}\n"
    )
    if rework_comment:
        task += (
            "\nThis is a REWORK: a reviewer requested changes on your earlier commit. "
            f"Review comment: {rework_comment}\n"
            "Update the existing files to address it (output every file you change)."
        )
    return (
        "You are a coding agent working on the repository below.\n\n"
        f"{task}\n"
        f"Repository files:\n{listing}\n\n"
        f"Current file contents (read-only context):\n{context_blocks}\n\n"
        f"Additional context from the session:\n{brief[:3000]}\n\n"
        "Now implement the task by writing complete files. OUTPUT FORMAT (mandatory): "
        "respond with ONLY file blocks, no commentary before, between, or after, each "
        "exactly like this:\n"
        "===FILE: relative/path===\n"
        "<complete file content>\n"
        "===END===\n"
        "\nTo DELETE a file, emit a delete block instead, with no body and no ===END===:\n"
        "===DELETE: relative/path===\n"
        "Only delete files the task actually asks you to remove. Omitting a file leaves "
        "it untouched; deleting is never the way to say 'unchanged'.\n"
    )


# Enough for a reasoning model to think AND write a whole file; a connection whose model
# needs more (or less) says so in its own params rather than through the deployment's
# environment, because that is a fact about the model, not about the host.
_DEFAULT_CODEGEN_MAX_TOKENS = 12000


def _budget_from(params: dict | None, fallback: int) -> int:
    raw = dict(params or {}).get("max_tokens")
    try:
        return int(raw) if raw else fallback
    except (TypeError, ValueError):
        return fallback


def make_model_codegen(
    *,
    model: str,
    api_base: str | None = None,
    params: dict[str, object] | None = None,
    api_key: str | None = None,
    # Reasoning models spend this budget on thinking BEFORE emitting any content, and the
    # thinking is not part of the returned text. At 4000 a local qwen3.8 asked for a full
    # game loop reasoned right through the allowance and returned an empty string --
    # which surfaced only as "model produced no parseable file blocks" and a silent
    # fallback to the scaffold. The budget has to cover think + write, not just write.
    max_tokens: int = _DEFAULT_CODEGEN_MAX_TOKENS,
) -> CodegenFn:
    """A ``CodegenFn`` backed by the ``ModelProvider`` port (LiteLLM adapter) -- a plain,
    tool-free generation, so the default provider route applies."""
    from adapters.models.litellm.provider import LiteLLMModelProvider
    from core.ports.model_provider import GenerationRequest

    provider = LiteLLMModelProvider()

    async def codegen(
        work_item: dict[str, Any],
        brief: str,
        repo_files: dict[str, str],
        rework_comment: str | None,
    ) -> dict[str, str]:
        req = GenerationRequest(
            model=model,
            messages=[
                {"role": "user", "content": _prompt(work_item, brief, repo_files, rework_comment)}
            ],
            # No temperature. A fixed 0.2 is a preference on models that accept it and a
            # hard failure on those that do not -- Anthropic's refuse anything but 1, so
            # every delegation assigned to such a persona raised UnsupportedParamsError
            # and fell back to writing a placeholder file named after the work item. The
            # connection states its own, like every other request here.
            # A connection that states its own budget wins: the request-level value is
            # only this adapter's default, and a default must not outrank a choice.
            max_tokens=_budget_from(params, max_tokens),
            api_base=api_base,
            params=dict(params or {}),
            api_key=api_key,
            purpose="delegation",
        )

        async def run(request: GenerationRequest) -> str:
            parts: list[str] = []
            async for chunk in provider.generate(request):
                if chunk.text:
                    parts.append(chunk.text)
            return "".join(parts)

        first = await run(req)
        files = parse_file_blocks(first)
        deletes = parse_deletions(first, keep=files)
        if not files and not deletes:
            # One-shot format repair: local models sometimes answer in prose or mimic the
            # context blocks; a direct correction almost always recovers them.
            repair = GenerationRequest(
                model=model,
                messages=[
                    *req.messages,
                    {"role": "assistant", "content": first[-2000:]},
                    {
                        "role": "user",
                        "content": (
                            "Your response contained no ===FILE: blocks, so nothing could "
                            "be applied. Re-output the COMPLETE files now, using ONLY this "
                            "exact format and nothing else:\n"
                            "===FILE: relative/path===\n<content>\n===END==="
                        ),
                    },
                ],
                # No temperature here. A fixed 0.2 made the repair deterministic on
                # models that accept it and impossible on those that do not: Anthropic's
                # refuse anything but 1, so the repair raised UnsupportedParamsError, the
                # whole call failed, and the delegation fell back to a scaffold that
                # writes placeholder files named after the work item. A reviewer then
                # rejected a pull request whose real fault was three layers up. The
                # connection's own params decide, like every other request here.
                max_tokens=max_tokens,
                api_base=api_base,
                params=dict(params or {}),
                purpose="delegation",
            )
            second = await run(repair)
            files = parse_file_blocks(second)
            deletes = parse_deletions(second, keep=files)
        if not files and not deletes:
            raise CodegenError(
                "model produced no parseable file blocks after a repair attempt "
                f"({len(first)} then {len(second)} chars; head: {first[:160]!r})"
            )
        return CodegenOutput(files=files, deletes=frozenset(deletes))

    return codegen
