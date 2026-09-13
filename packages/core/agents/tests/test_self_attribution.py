"""A persona must not announce its own name in its turn.

Every other speaker reaches the model as "Name: text" -- that is how a multi-party scene
stays legible in the replayed conversation -- and models imitate the pattern on their own
line. The transcript already attributes each turn, so the result renders twice
("Petra Lind: Petra Lind: Sit down, all of you") and reads as a character introducing
herself mid-interrogation. Observed live in the Hägnaryd sample.
"""

from __future__ import annotations

from core.agents.runtime import strip_self_attribution


def test_the_speakers_own_prefix_is_removed() -> None:
    assert (
        strip_self_attribution(
            "Kriminalinspektör Petra Lind: Sit down, all of you.",
            "Kriminalinspektör Petra Lind",
        )
        == "Sit down, all of you."
    )


def test_leading_whitespace_and_case_do_not_hide_it() -> None:
    assert strip_self_attribution("  petra lind:   Sit down.", "Petra Lind") == "Sit down."


def test_another_speakers_name_is_left_alone() -> None:
    """Addressing someone is ordinary dialogue, not self-attribution."""
    said = "Marta Sjöberg: you were in the cellar, weren't you?"
    assert strip_self_attribution(said, "Petra Lind") == said


def test_a_colon_inside_ordinary_prose_survives() -> None:
    said = "One thing is clear: somebody greased those stairs."
    assert strip_self_attribution(said, "Petra Lind") == said


def test_only_the_first_prefix_goes() -> None:
    """A second mention is the model quoting itself, which is content, not attribution."""
    out = strip_self_attribution("Lind: Lind: sit down.", "Lind")
    assert out == "Lind: sit down."


def test_a_turn_that_is_only_the_name_is_kept_intact() -> None:
    """Stripping it would leave an empty message, which is worse than an odd one."""
    assert strip_self_attribution("Petra Lind:", "Petra Lind") == "Petra Lind:"


def test_no_name_or_no_content_changes_nothing() -> None:
    assert strip_self_attribution("Sit down.", "") == "Sit down."
    assert strip_self_attribution("", "Petra Lind") == ""
