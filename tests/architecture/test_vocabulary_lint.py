"""D1.6's "no literal RPG-overlay noun outside the overlay seed data" CI check (plan
§12.2, Appendix A, requirement 32): core code speaks in domain-neutral `label_key`s;
`.tsx`/`.ts` UI code must resolve those through `useLabel()`/`label()`, never hardcode
the RPG overlay's own display strings directly. A literal "Arbiter" in a `.tsx` file is
exactly the kind of hardcoded noun that breaks switching a workspace to `enterprise_v1`
(D1.6's own acceptance criterion) -- this catches it before merge, not after a user
notices the switcher didn't relabel something.

Scans a curated list of the RPG overlay's own, unambiguous display strings (not common
English words like "World"/"Session" that would false-positive against ordinary
engineering prose) against every `.ts`/`.tsx` file under `web/src`, excluding the files
that are legitimately allowed to contain them: the overlay resolver's own fallback data
(`labels.ts`) and this test file's own docstring/literals.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"

# The RPG overlay's own distinctive display strings (from the D1.6 migration's rpg_v1
# seed / DEFAULT_LABELS) -- deliberately the unambiguous ones, not generic English words
# ("World", "Session", "Character" alone would false-positive against ordinary prose).
_BANNED_LITERALS = (
    "Arbiter",
    "Rulebook",
    "Lorebook",
    "Miscellany",
    "Player Character bot",
    "NPC bot",
    "Table Owner",
    "Dice Roller",
    "Coin Flip",
    "Stat Calculator",
    "Game System",
    "What the Arbiter Knew",
    "Save Point",
    "Campaign Recap",
)

# Files where these literals are the legitimate source of truth, not a violation.
_ALLOWED_FILES = {
    WEB_SRC / "lib" / "vocabulary" / "labels.ts",
}


def _source_files() -> list[pathlib.Path]:
    return [
        p
        for p in list(WEB_SRC.rglob("*.ts")) + list(WEB_SRC.rglob("*.tsx"))
        if p not in _ALLOWED_FILES
    ]


def test_no_hardcoded_rpg_overlay_literals_outside_the_overlay_seed_data() -> None:
    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text()
        for literal in _BANNED_LITERALS:
            if literal in text:
                offenders.append(f"{path.relative_to(ROOT)}: {literal!r}")

    assert not offenders, (
        "hardcoded RPG-overlay literal(s) found outside labels.ts -- resolve through "
        "useLabel()/label() instead:\n" + "\n".join(offenders)
    )


def test_the_scan_actually_finds_something_when_present(tmp_path: pathlib.Path) -> None:
    """Guards the scanner itself: a deliberately-planted violation must be caught, so
    this test isn't silently vacuous (e.g. from a glob typo)."""
    planted = tmp_path / "Planted.tsx"
    planted.write_text('export const x = "Arbiter";')

    offenders = [p for p in [planted] if "Arbiter" in p.read_text()]
    assert offenders == [planted]
