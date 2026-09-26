"""The eval-arm fence: arms 1-2 deliberately put concealed plaintext into model context
(the designs rejects), so the switch must be structurally unreachable from
production. Two locks:

1. `eval_arm=` is PASSED as an argument only from eval-runner code and tests -- never
   from api routes, worker jobs, or any other core module.
2. The `"eval_expose"` action string is constructed only in the one factory branch
   gated by `eval_arm`, and consumed only by `render_injection`'s explicit case.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Where each token is ALLOWED to appear (definitions + the fenced construction site +
# eval/runner code + tests). Everything else under packages/ is production surface.
_EVAL_ARM_ALLOWED = {
    "packages/core/process/live_session.py",  # the parameter definition + pass-through
    "packages/core/assembler/secrets_gate_factory.py",  # the fenced implementation
    "packages/core/secrets/exclusion.py",  # render_injection's documented eval branch
}
_EVAL_EXPOSE_ALLOWED = {
    "packages/core/assembler/secrets_gate_factory.py",
    "packages/core/secrets/exclusion.py",
}


def _offending_files(token: str, allowed: set[str]) -> list[str]:
    out: list[str] = []
    for path in (_ROOT / "packages").rglob("*.py"):
        rel = str(path.relative_to(_ROOT))
        if rel.replace("\\", "/") in allowed:
            continue
        if "/eval/" in rel.replace("\\", "/"):
            continue  # the runner is the intended caller
        if "/tests/" in rel.replace("\\", "/"):
            continue
        if token in path.read_text(encoding="utf-8", errors="ignore"):
            out.append(rel)
    return out


def test_eval_arm_is_never_passed_from_production_code() -> None:
    offenders = _offending_files("eval_arm", _EVAL_ARM_ALLOWED)
    assert not offenders, (
        f"eval_arm reached production surface: {offenders}. Arms 1-2 put secret "
        "plaintext into context on purpose; only the eval runner may select them."
    )


def test_eval_expose_action_is_never_constructed_in_production_code() -> None:
    offenders = _offending_files("eval_expose", _EVAL_EXPOSE_ALLOWED)
    assert not offenders, f"'eval_expose' constructed outside its fenced sites: {offenders}."


def test_no_http_route_module_mentions_eval_arm() -> None:
    """Belt and braces: the api routes tree must not even contain the token."""
    offenders = []
    for path in (_ROOT / "packages" / "api").rglob("*.py"):
        if re.search(r"eval_arm|eval_expose", path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, f"api tree mentions the eval switch: {offenders}"
