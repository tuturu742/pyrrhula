"""The SearXNG search transport: what actually reaches the backend.

Written when a newsroom sample needed today's news and kept getting results years old.
Two facts about this layer turned out to matter and neither was covered: the query can
carry a recency window, and the default engine set is a fact about the instance rather
than something the platform can know.
"""


async def test_a_recency_window_reaches_the_backend() -> None:
    """Without it the top results for a well-covered subject are whatever ranks best,
    which is usually years old -- measured: an unfiltered query returned hits spanning a
    decade, the same query with a week's window returned that day's."""
    captured: dict[str, object] = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            captured.update(params or {})
            return _Resp()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    import httpx

    from adapters.mcp.search_transport import SearxngSearchTransport
    from core.ports.mcp import McpServerRef

    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *a, **k: _Client()  # type: ignore[assignment]
    try:
        t = SearxngSearchTransport()
        await t.call_tool(
            McpServerRef(key="web_search", url="http://searx.test"),
            "search",
            {"query": "anything", "recency": "week"},
        )
    finally:
        httpx.AsyncClient = original  # type: ignore[assignment]

    assert captured.get("time_range") == "week"


async def test_no_recency_means_no_window() -> None:
    """'any' and absent must both mean unfiltered, not a literal time_range=any that the
    backend would reject."""
    captured: dict[str, object] = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            captured.update(params or {})
            return _Resp()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    import httpx

    from adapters.mcp.search_transport import SearxngSearchTransport
    from core.ports.mcp import McpServerRef

    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *a, **k: _Client()  # type: ignore[assignment]
    try:
        await SearxngSearchTransport().call_tool(
            McpServerRef(key="web_search", url="http://searx.test"),
            "search",
            {"query": "anything", "recency": "any"},
        )
    finally:
        httpx.AsyncClient = original  # type: ignore[assignment]

    assert "time_range" not in captured
