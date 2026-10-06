"""The assistant's gist check, pure: a gist may say what a secret is about, not what it is."""

from __future__ import annotations

import pytest

from core.agents.assistant_chat import gist_gives_away

_SECRET = "The barrow's warden is Linnea's lost brother."


@pytest.mark.parametrize(
    "gist",
    [
        _SECRET,
        "the barrow's warden is linnea's lost brother",
        "Secret: the barrow's warden is Linnea's lost brother!",
        "Linnea's lost brother is the barrow's warden",
    ],
)
def test_gists_that_give_the_secret_away(gist: str) -> None:
    assert gist_gives_away(_SECRET, gist)


@pytest.mark.parametrize(
    "gist",
    [
        "The identity of the barrow's warden.",
        "the warden's true identity",
        "something about Linnea's family",
    ],
)
def test_gists_that_only_say_what_it_is_about(gist: str) -> None:
    assert not gist_gives_away(_SECRET, gist)


_LONG = (
    "The warden of the barrow under Karsh Vale is Linnea Faelor's lost brother, taken by "
    "the Hollow Crown as a boy; he remembers her and will not strike first."
)


@pytest.mark.parametrize(
    "gist",
    [
        "The barrow's warden is Linnea's lost brother.",
        "the warden is Linnea's brother, taken by the Hollow Crown",
    ],
)
def test_a_one_line_statement_of_a_longer_secret_gives_it_away(gist: str) -> None:
    """The model writes the secret out at length and keeps the user's sentence as the
    gist: few of the long text's words, but the gist is nothing but the secret."""
    assert gist_gives_away(_LONG, gist)


@pytest.mark.parametrize(
    "gist",
    [
        "The identity of the barrow's warden.",
        "who the warden really is",
        "something the referee knows about Linnea's family",
    ],
)
def test_a_longer_secret_with_an_honest_gist(gist: str) -> None:
    assert not gist_gives_away(_LONG, gist)


def test_short_secrets_need_the_whole_text_to_count() -> None:
    assert gist_gives_away("the butler did it", "The butler did it.")
    assert not gist_gives_away("the butler did it", "knows who did it")
