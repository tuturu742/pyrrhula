"""Fetching a page the MODEL chose, which is the part that makes this dangerous.

A search goes to one configured host. A fetch goes wherever a URL says -- and the URL
can come from a page the model just read, so "the model chose it" and "a stranger chose
it" are the same sentence. These pin the refusals.
"""

import pytest

from adapters.mcp.fetch_transport import _MAX_CHARS, _refuse_private, _TextExtractor
from core.ports.mcp import McpTransportError


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "169.254.169.254",  # the cloud metadata endpoint: credentials, one GET away
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "0.0.0.0",
    ],
)
def test_the_private_network_is_refused(host: str) -> None:
    with pytest.raises(McpTransportError) as caught:
        _refuse_private(host)
    assert "non-public" in str(caught.value) or "cannot resolve" in str(caught.value)


def test_a_public_host_is_allowed() -> None:
    """The guard must not refuse everything -- a check that never passes is not a check."""
    _refuse_private("example.com")


def test_text_survives_and_scripts_do_not() -> None:
    """Script and style content is not readable text, and a model that reads a page's
    inline JavaScript is reading something no journalist would call a source."""
    parser = _TextExtractor()
    parser.feed(
        "<html><head><title>t</title><style>.a{color:red}</style></head>"
        "<body><script>var x = 'not prose';</script>"
        "<h1>Ceasefire holds</h1><p>Reports say the line held overnight.</p>"
        "</body></html>"
    )
    text = parser.text()
    assert "Ceasefire holds" in text
    assert "Reports say the line held overnight." in text
    assert "not prose" not in text
    assert "color:red" not in text


def test_entities_are_decoded() -> None:
    parser = _TextExtractor()
    parser.feed("<p>Caf&eacute; &amp; bar &mdash; open</p>")
    assert "Café & bar — open" in parser.text()


def test_the_character_ceiling_is_small_enough_to_fit_a_turn() -> None:
    """A page is unbounded and a turn's context is not. The number matters less than its
    existing: an article that does not fit is one the reader summarises."""
    assert 0 < _MAX_CHARS <= 20_000
