"""A shipped ruleset must resolve the rolls its own rulebook asks for.

Three times in one day a sample's rulebook named a roll the rule system had no check
type for, and each time the failure wore someone else's clothes:

* character creation had no unmodified roll, so a 3d6 ability score came back adjusted
  by an ability modifier and a player refused to write down a 19;
* the first encounter had no ``initiative``, so the referee called the dice tool four
  times, was refused four times, and the combat beat played as a conversation -- which
  read as a model ignoring its instructions;
* "Attack Rolls" ends "and damage is rolled" and separates the Strength bonus for melee
  from the Dexterity bonus for missiles, and one ``attack_roll`` covered neither.

The tool is honest about every refusal. Nothing downstream is, because a refused roll
leaves no ``resolution_record`` -- the beat simply has no dice in it.

Read from the **shipped bundle** rather than from the builder, because the bundle is
what a reader imports. Each rule is keyed on a phrase from the rulebook entry, and the
phrase is asserted to still be there: a rulebook that changes its mind about what it
asks for should fail this rather than silently stop being checked.
"""

from __future__ import annotations

import json
import pathlib
import zipfile

import pytest

_BUNDLE = pathlib.Path.home() / "code" / "pyrrhula-samples" / "karsh-vale" / "karsh-vale.pyr"

# phrase from the shipped rulebook -> the check type that phrase requires
_ROLLS_THE_RULEBOOK_ASKS_FOR = {
    "rolls **1d6** for Initiative": "initiative",
    "damage is rolled": "damage",
    "**Dexterity** bonus for missiles": "missile_attack",
    "**Strength** bonus for melee": "attack_roll",
    "rolls a single hit die": "hit_die",
}

pytestmark = pytest.mark.skipif(
    not _BUNDLE.is_file(), reason=f"sample bundle not checked out at {_BUNDLE}"
)


def _bundle_parts() -> tuple[dict, str]:
    with zipfile.ZipFile(_BUNDLE) as z:
        rules_path = next(
            n for n in z.namelist() if n.startswith("rules/") and "rule_system" in n
        )
        system = json.loads(z.read(rules_path))
        rulebook = "\n".join(
            z.read(n).decode() for n in z.namelist() if n.endswith(".md")
        )
    return system, rulebook


@pytest.mark.parametrize(
    ("phrase", "check_type"), sorted(_ROLLS_THE_RULEBOOK_ASKS_FOR.items())
)
def test_the_ruleset_can_resolve_a_roll_its_rulebook_names(
    phrase: str, check_type: str
) -> None:
    system, rulebook = _bundle_parts()
    assert phrase in rulebook, (
        f"the rulebook no longer says {phrase!r}; update this mapping rather than "
        f"letting the rule quietly stop being checked"
    )
    declared = set(system["check_types"])
    assert check_type in declared, (
        f"the rulebook says {phrase!r} and {check_type!r} is not a check type in "
        f"{system['key']!r}: the referee's call is refused and the roll never happens"
    )


def test_every_check_type_can_resolve_a_modifier() -> None:
    """A check type with no ``modifier_resolver`` is refused at call time with
    ``no modifier_resolver defined`` -- the same dead end by a different name."""
    system, _ = _bundle_parts()
    missing = sorted(set(system["check_types"]) - set(system["modifier_resolver"]))
    assert not missing, f"declared check types with no modifier resolver: {missing}"
