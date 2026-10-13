"""A turn speaks only for its author: a reply that goes on to write other participants'
lines is cut there; a reply that merely addresses someone with "Name:" is left alone.
The texts are real replies from Mafia games between models."""

from __future__ import annotations

from core.agents.attribution import keep_own_words

_TABLE = ["Avery", "Skyler", "Sage", "Kendall", "Reese", "Rowan", "Jordan", "Casey", "Host"]

_SCRIPT = """Interesting how fast the pressure consolidated on Skyler -- Hayden pushed.

For me: Kendall's summary post was the weakest of the day. Kendall, what's your suspicion?

Skyler: Everyone wants an answer, so here it is: I lean logic, but day one has no logic.

Sage: Lot of pressure already for day one. Kendall, I'll second Avery: answer us."""


def test_a_script_of_other_speakers_is_cut_at_its_first_line() -> None:
    kept = keep_own_words(_SCRIPT, "Avery", _TABLE)
    assert kept.spoken_for == ("Skyler", "Sage")
    assert kept.text.endswith("Kendall, what's your suspicion?")
    assert "I lean logic" not in kept.text and "Lot of pressure" not in kept.text


def test_bold_labels_count_too() -> None:
    text = "My read is Reese.\n\n**Reese:** I was home.\n**Rowan**: Same, I was with my sister."
    kept = keep_own_words(text, "Avery", _TABLE)
    assert kept.text == "My read is Reese." and kept.spoken_for == ("Reese", "Rowan")


def test_one_label_addressing_someone_is_left_alone() -> None:
    for author, text in [
        ("Morgan", "I'll own my vote.\n\nReese: Jordan said your case was borrowed. Explain."),
        ("Reese", "That pinged me too.\n\nCasey: the kitchen lean is thin, and I don't buy it."),
        ("Reese", 'Jordan: say "glitch" and we move on.\n\nMy vote is Hayden.'),
    ]:
        kept = keep_own_words(text, author, [*_TABLE, "Morgan"])
        assert kept.text == text and kept.spoken_for == ()


def test_a_line_where_the_writer_becomes_someone_else_is_cut() -> None:
    kept = keep_own_words(
        "Rowan: Hey everyone, I'm Rowan. I tend to stay up late on crosswords.", "Jordan", _TABLE
    )
    assert kept.text == "" and kept.spoken_for == ("Rowan",)


def test_the_authors_own_label_and_unknown_labels_are_not_cut() -> None:
    text = "Avery: here is my read.\nNote: day one is thin.\nPlan: watch Skyler and Sage."
    kept = keep_own_words(text, "Avery", _TABLE)
    assert kept.text == text and kept.spoken_for == ()


def test_analysis_lists_and_orders_to_teammates_are_not_scripts() -> None:
    """The three replies an earlier version of the guard cut by mistake."""
    analysis = (
        "Parker voted Sage.\n\nHayden fits: talked about being reluctant, then never voted "
        "Parker.\nRiley too: I watched them hedge all day."
    )
    roles = "The game is over.\n\n- Riley: Mafia Don\n- Kendall: Mafia\n- Rowan: Mafia"
    orders = (
        "Shoot Morgan tonight.\n\nKendall: don't re-claim Sheriff, say the guilty was a panic "
        "name.\n\nEmerson: stay quiet tomorrow and echo the table."
    )
    for author, text in [("Skyler", analysis), ("Host", roles), ("Casey", orders)]:
        kept = keep_own_words(text, author, [*_TABLE, "Hayden", "Riley", "Emerson", "Parker"])
        assert kept.text == text and kept.spoken_for == (), author
