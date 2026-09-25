"""Fetch one web page and return its readable text (the ``web_fetch`` preset).

A search returns a title, a URL and a snippet. That is enough to know a story exists and
not enough to report it: a desk given snippets cannot quote anybody, because a sentence
in quotation marks it never read is invented however plausible it sounds. This is the
other half -- the page itself.

Three things this must not become, in order of how badly they end:

**A way into the private network.** A model chooses the URL, and a model can be talked
into choosing one by the very page it is reading. So the host is resolved and every
address it resolves to is checked: loopback, link-local (including the cloud metadata
endpoints at 169.254.169.254), private ranges and anything not global is refused before a
connection is opened. Redirects are followed manually and re-checked at every hop, since
a public host is free to redirect to 127.0.0.1.

**A way to fill the context.** Pages are unbounded; a turn's budget is not. The body is
read to a byte ceiling and the extracted text is cut to a character ceiling.

**A source of instructions.** The returned text is enveloped as untrusted tool output by
the same machinery search results go through. Nothing a page says is an instruction.
"""

from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from typing import Any

import httpx

from core.ports.mcp import McpServerRef, McpToolResult, McpToolSpec, McpTransportError

FETCH_TOOL = McpToolSpec(
    name="fetch",
    description=(
        "Fetch a web page and return its readable text. Use it on a URL a search "
        "returned, when the snippet is not enough to report from -- a search gives you "
        "the headline, this gives you the article."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "the page to fetch (http or https)"}
        },
        "required": ["url"],
    },
)

# A page is read to here and no further, before any parsing.
_MAX_BYTES = 2_000_000
# And the text handed back is cut to here. A turn has a context budget; an article that
# does not fit in it is an article the reader summarises, not one the tool streams.
_MAX_CHARS = 12_000
_MAX_REDIRECTS = 4
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})


class _TextExtractor(HTMLParser):
    """Tags out, text in, block elements become line breaks.

    Deliberately stdlib: this ships in an image, and a readability library is a
    dependency and a CVE surface for something a hundred lines of parser does adequately.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in ("p", "br", "div", "h1", "h2", "h3", "h4", "li", "tr", "article"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            text = data.strip()
            if text:
                self._parts.append(text + " ")

    def text(self) -> str:
        joined = "".join(self._parts)
        lines = [ln.strip() for ln in joined.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def _refuse_private(host: str) -> None:
    """Resolve and refuse anything not on the public internet.

    Checked per hostname rather than per URL string: `localhost`, `127.0.0.1`,
    `0x7f.1`, a DNS name that resolves to a private address and a redirect to any of
    them are the same attack, and only resolution sees them all.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise McpTransportError(f"cannot resolve {host!r}") from exc
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not parsed.is_global or parsed.is_loopback or parsed.is_link_local:
            raise McpTransportError(
                f"refusing to fetch {host!r}: resolves to non-public address {address}"
            )


class WebFetchTransport:
    async def list_tools(self, server: McpServerRef) -> list[McpToolSpec]:  # noqa: ARG002
        return [FETCH_TOOL]

    async def call_tool(
        self, server: McpServerRef, name: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        if name != "fetch":
            raise McpTransportError(f"unknown tool {name!r} on {server.key!r}")
        url = str(arguments.get("url") or "").strip()
        if not url:
            return McpToolResult(content="no url given", is_error=True)

        seen = 0
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            while True:
                parsed = httpx.URL(url)
                if parsed.scheme not in ("http", "https"):
                    return McpToolResult(
                        content=f"refusing {parsed.scheme!r}: only http and https",
                        is_error=True,
                    )
                _refuse_private(parsed.host)
                try:
                    response = await client.get(url, headers={"user-agent": "Pyrrhula/1.0"})
                except httpx.HTTPError as exc:
                    raise McpTransportError(f"fetch failed: {exc}") from exc
                if response.is_redirect and seen < _MAX_REDIRECTS:
                    location = response.headers.get("location")
                    if not location:
                        break
                    url = str(parsed.join(location))
                    seen += 1
                    continue
                break

        if response.status_code >= 400:
            return McpToolResult(
                content=f"{url} returned HTTP {response.status_code}", is_error=True
            )
        body = response.content[:_MAX_BYTES]
        content_type = response.headers.get("content-type", "")
        if "html" in content_type:
            parser = _TextExtractor()
            parser.feed(body.decode(response.encoding or "utf-8", "replace"))
            text = parser.text()
        else:
            text = body.decode(response.encoding or "utf-8", "replace")

        clipped = len(text) > _MAX_CHARS
        text = text[:_MAX_CHARS]
        if clipped:
            text += "\n\n[... page truncated ...]"
        if not text.strip():
            return McpToolResult(content=f"{url} had no readable text", is_error=True)
        return McpToolResult(
            content=f"{url}\n\n{text}",
            structured={"url": url, "chars": len(text), "truncated": clipped},
        )
