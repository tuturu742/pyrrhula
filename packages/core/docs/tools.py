"""The two read tools that put the manual in front of an assistant: ``search_docs`` and
``read_doc``. No model, no database, nothing tenant-specific -- the same for the workspace
assistant and the admin console's."""

from __future__ import annotations

import json

from core.agents.tools import ToolContext, ToolRegistry, ToolResult
from core.docs.index import page_index_line, read_page, search
from core.ports.model_provider import ToolSpec

SEARCH_DOCS = "search_docs"
READ_DOC = "read_doc"
DOCS_READ_TOOLS = frozenset({SEARCH_DOCS, READ_DOC})


def _obj(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {"type": "object", "properties": properties, "required": required}


async def _search(args: dict[str, object], _ctx: ToolContext) -> ToolResult:
    query = str(args.get("query") or "")
    hits = search(query)
    if not hits:
        return ToolResult(
            content=f"no documentation section matches {query!r}; try other words, "
            "or the exact name of a setting or page."
        )
    return ToolResult(
        content=json.dumps(
            [
                {
                    "page": h.page,
                    "section": " › ".join(h.heading_path),
                    "anchor": h.anchor,
                    "snippet": h.snippet,
                }
                for h in hits
            ]
        )
    )


async def _read(args: dict[str, object], _ctx: ToolContext) -> ToolResult:
    page = str(args.get("page") or "")
    section = args.get("section")
    result = read_page(page, str(section) if section else None)
    return ToolResult(content=json.dumps(result))


def register_docs_tools(registry: ToolRegistry) -> list[ToolSpec]:
    specs = [
        ToolSpec(
            name=SEARCH_DOCS,
            description=(
                "Search the product documentation (how Pyrrhula itself works: installing, "
                "settings, concepts, operating a deployment). Returns matching sections "
                "with a snippet; follow up with read_doc for the best one, and cite the "
                "page and heading in your answer."
            ),
            parameters=_obj(
                {"query": {"type": "string", "description": "words or a setting name"}},
                ["query"],
            ),
        ),
        ToolSpec(
            name=READ_DOC,
            description=(
                "Read one section of a documentation page (with its sub-sections), or the "
                "whole page when no section is given. Page keys and section headings come "
                "from search_docs."
            ),
            parameters=_obj(
                {
                    "page": {"type": "string", "description": "page key, e.g. configuration"},
                    "section": {"type": "string", "description": "heading text (optional)"},
                },
                ["page"],
            ),
        ),
    ]
    registry.register(specs[0], _search)
    registry.register(specs[1], _read)
    return specs


def docs_prompt_line() -> str:
    return (
        "For questions about how Pyrrhula itself works (installing, settings, concepts, "
        "operations) call search_docs, then read_doc for the best hit, and cite the page "
        "and heading. " + page_index_line()
    )
