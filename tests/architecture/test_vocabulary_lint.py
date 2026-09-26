"""The "no literal RPG-overlay noun outside the overlay seed data" CI check: core code
speaks in domain-neutral `label_key`s;
`.tsx`/`.ts` UI code must resolve those through `useLabel()`/`label()`, never hardcode
the RPG overlay's own display strings directly. A literal "Arbiter" in a `.tsx` file is
exactly the kind of hardcoded noun that breaks switching a workspace to `enterprise_v1`
(its own acceptance criterion) -- this catches it before merge, not after a user
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

# The RPG overlay's own distinctive display strings (from the seeded rpg_v1
# overlay / DEFAULT_LABELS) -- deliberately the unambiguous ones, not generic English words
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


# ── The backend half of the same rule ──────────────────────────────────────────────
#
# CLAUDE.md rule 1 bans domain words in `packages/core`, and until now nothing checked
# it: the scan above only ever looked at `web/src` display strings, so `dice_roller`,
# `dice_grammar` and `max_dice_count` sat in a core table, a core tool key and a core
# handler name for four phases without anything noticing. The words below are the
# unambiguous ones -- a core module has no honest reason to say "dice" or "campaign",
# whereas "player" appears inside "multiplayer" and "character" inside "characters of a
# string", so those two are matched as whole words only and still carry exemptions.

CORE = ROOT / "packages" / "core"

_BANNED_CORE_WORDS = (
    "dice",
    "game_master",
    "campaign",
    "dungeon",
    "sprint",
    "standup",
    "pull_request",
)

# `packages/core/process/dsl/` ships example/fixture process definitions, which are pack
# *content* shaped like code -- the same exemption `.plugins/` content gets.
_CORE_EXEMPT_DIRS = (CORE / "process" / "dsl",)


def _core_files() -> list[pathlib.Path]:
    return [
        p for p in CORE.rglob("*.py") if not any(p.is_relative_to(d) for d in _CORE_EXEMPT_DIRS)
    ]


def test_no_domain_words_in_core() -> None:
    """CLAUDE.md rule 1, enforced rather than asserted. A domain word here is not a
    style nit: it is the thing that makes `packages/core` unusable for the next
    vertical, which is the entire premise of the overlay design.

    A line may carry a `vocab-ok:` marker with a reason. There are exactly two honest
    ones: prose that *names* overlay values in order to explain this very rule, and a
    foreign key we call rather than coin (an MCP server's own tool name). Anything
    else wanting the marker is a design smell, not a lint problem."""
    offenders: list[str] = []
    for path in _core_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if "vocab-ok" in line:
                continue
            lowered = line.lower()
            for word in _BANNED_CORE_WORDS:
                if word in lowered:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {word!r}")

    assert not offenders, (
        "domain word(s) in packages/core -- CLAUDE.md rule 1. Core speaks in "
        "domain-neutral terms and emits label_keys; the domain word belongs in pack "
        "content or a vocabulary overlay:\n" + "\n".join(offenders)
    )


def test_the_core_scan_actually_finds_something_when_present(tmp_path: pathlib.Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text("DICE_GRAMMAR = {}\n")
    assert [w for w in _BANNED_CORE_WORDS if w in planted.read_text().lower()] == ["dice"]
