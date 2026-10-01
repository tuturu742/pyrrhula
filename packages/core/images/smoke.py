"""The smoke test an image passes before anything runs in it.

Run on the tenant's own execution engine through ``ExecEnvProvider.run_script`` -- the
same call, image and pull credential a delegation will use -- so a pass proves the thing
that matters: *this* engine can pull *this* digest and the result has what a delegation
needs. ``git`` is required (every delegation clones). A harness is checked only when the
image claims one, and only a printed version that matches lets a delegation skip the
harness install; a claim the smoke test cannot confirm is recorded as unconfirmed, never
believed.

The script is assembled from fixed text plus the harness's ``smoke_cmd``, which is
operator configuration on the same footing as its ``setup_cmds`` (rule 10). Nothing from
the image, the bundle or the tenant is interpolated into it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_MARK = "pyr-smoke:"
_MAX_OUTPUT = 4000


def smoke_script(harness_spec: dict[str, Any] | None) -> str:
    lines = [
        "set -u",
        f'echo "{_MARK}begin"',
        "if command -v git >/dev/null 2>&1; then",
        f'  echo "{_MARK}git=$(git --version 2>&1 | head -n1)"',
        "else",
        f'  echo "{_MARK}git="',
        "fi",
    ]
    smoke_cmd = str((harness_spec or {}).get("smoke_cmd") or "").strip()
    if smoke_cmd:
        lines.append(f'echo "{_MARK}harness=$( ( {smoke_cmd} ) 2>&1 | head -n1)"')
    lines.append(f'echo "{_MARK}end"')
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class SmokeOutcome:
    passed: bool
    git: str
    harness_output: str | None
    # Why it failed, in words a person can act on. Empty when it passed.
    reason: str
    transcript: str


def parse_smoke(exit_code: int, output: str) -> SmokeOutcome:
    found: dict[str, str] = {}
    for line in output.splitlines():
        if line.startswith(_MARK):
            key, _, value = line[len(_MARK) :].partition("=")
            found[key] = value.strip()
    transcript = output[-_MAX_OUTPUT:]
    harness_output = found.get("harness")
    if "end" not in found:
        return SmokeOutcome(
            False,
            "",
            harness_output,
            f"the smoke test did not finish (exit {exit_code}); the image may lack /bin/sh",
            transcript,
        )
    git = found.get("git", "")
    if not git:
        return SmokeOutcome(
            False,
            "",
            harness_output,
            "git is not installed in the image, and every delegation clones the repository",
            transcript,
        )
    return SmokeOutcome(True, git, harness_output, "", transcript)


def proven_harness(
    claim_key: str, spec: dict[str, Any] | None, harness_output: str | None
) -> dict[str, str] | None:
    """The ``baked_harness`` record when the smoke output proves the claim, else None."""
    if not claim_key or not spec or harness_output is None:
        return None
    version = str(spec.get("version") or "").strip()
    if not version or version not in harness_output:
        return None
    from core.harness.registry import harness_fingerprint

    return {"key": claim_key, "version": version, "fingerprint": harness_fingerprint(spec)}
