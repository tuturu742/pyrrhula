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


def test_short_secrets_need_the_whole_text_to_count() -> None:
    assert gist_gives_away("the butler did it", "The butler did it.")
    assert not gist_gives_away("the butler did it", "knows who did it")
