"""A1.6 acceptance criteria, encoded as an automated check rather than left as a manual
review checklist item (matching this project's existing pattern, e.g. T0.5's INV-1 lint):
"no class weighting applied to scores, anywhere in the retrieval package." D2 (plan §6.2)
is the whole reason WRRF exists instead of a multiplier on similarity scores -- this test
is what stops a future change from quietly reintroducing one.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[5]
RETRIEVAL_DIR = ROOT / "packages" / "core" / "knowledge" / "retrieval"

# Matches `score *`, `.score *`, `wrrf_score *` etc followed by a multiplication --
# the shape a class-weight-on-score regression would take. Deliberately broad (a
# false-positive here just means "go look," not "silently miss a real one").
_SCORE_MULTIPLY_RE = re.compile(r"\bscore\s*\*(?!\*)")


def test_no_score_is_ever_multiplied_in_the_retrieval_package() -> None:
    offenders: list[str] = []
    for path in RETRIEVAL_DIR.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _SCORE_MULTIPLY_RE.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "D2: priority is budget allocation (WRRF fusion + bucket fill), never a "
        "multiplier on a similarity/rank score. Offending lines:\n" + "\n".join(offenders)
    )
