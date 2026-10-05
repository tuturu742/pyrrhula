"""The docs index is pure Python over the checkout's own docs/ -- no database."""

from __future__ import annotations

import uuid

from core.agents.tools import ToolContext, ToolRegistry
from core.docs.index import (
    DocsIndex,
    docs_root,
    page_index_line,
    read_page,
    search,
    split_sections,
)
from core.docs.tools import DOCS_READ_TOOLS, register_docs_tools
from core.ports.model_provider import ToolCall

FENCED = """# Title

Intro.

## Real section

```bash
# not a heading
echo hi
```

~~~
# also not a heading
~~~

````md
```
# nested fence, still not a heading
```
````

Tail.
"""


def test_fenced_hash_lines_are_not_headings() -> None:
    sections = split_sections(FENCED, "t")
    assert [s.heading_path for s in sections] == [("Title",), ("Title", "Real section")]
    assert "# not a heading" in sections[1].body
    assert "# also not a heading" in sections[1].body
    assert "nested fence" in sections[1].body
    assert sections[1].body.endswith("Tail.")


def test_heading_path_tracks_levels() -> None:
    text = "# A\n\na\n\n## B\n\nb\n\n### C\n\nc\n\n## D\n\nd\n"
    sections = split_sections(text, "p")
    assert [s.heading_path for s in sections] == [("A",), ("A", "B"), ("A", "B", "C"), ("A", "D")]
    assert sections[2].level == 3


def test_real_docs_have_no_fence_headings() -> None:
    """`# Kubernetes` inside operations.md's shell block is a comment, not a heading --
    the same word is a real heading in self-host.md, so the check is per page."""
    index = DocsIndex.build(docs_root())
    for page, fenced in (
        ("operations", "Kubernetes"),
        ("operations", "Compose"),
        ("self-host", "Fill in the four required secrets"),
        ("install", "$TOKEN"),
    ):
        headings = [s.heading_path[-1] for s in index.sections if s.page == page and s.level]
        assert headings, page
        assert not any(h.startswith(fenced) for h in headings), (page, fenced)
    assert any(s.heading_path[-1] == "Kubernetes" for s in index.sections if s.page == "self-host")


def test_search_finds_a_setting_in_configuration() -> None:
    hits = search("secret_mode")
    assert hits, "no hits"
    assert hits[0].page == "configuration"
    assert "Settings in the product" in hits[0].heading_path
    assert "secret_mode" in hits[0].snippet


def test_read_page_returns_the_section_with_its_children() -> None:
    result = read_page("configuration", "Settings in the product")
    assert result["page"] == "configuration"
    assert "assistant_context_max_tokens" in str(result["text"])
    assert read_page("docs/configuration.md")["section"] is None
    assert "candidates" in read_page("configuration", "zzz-no-such-heading")
    assert "error" in read_page("no-such-page")


def test_readme_and_faq_resolve_from_the_checkout() -> None:
    assert "multi-tenant" in str(read_page("readme")["text"]).lower()
    assert read_page("faq")["page"] == "faq"


def test_page_index_line_names_every_page_once() -> None:
    line = page_index_line()
    index = DocsIndex.build(docs_root())
    for key in index.pages:
        assert line.count(f"{key} — ") == 1, key
    assert "install — " in line and "readme — " in line


async def test_tools_register_and_answer() -> None:
    registry = ToolRegistry()
    specs = register_docs_tools(registry)
    assert {s.name for s in specs} == DOCS_READ_TOOLS
    ctx = ToolContext(tenant_id=uuid.uuid4(), persona_id=uuid.uuid4(), session_id=None)
    hit = await registry.dispatch(
        ToolCall(id="1", name="search_docs", arguments={"query": "secret_mode"}), ctx
    )
    assert '"page": "configuration"' in hit.content
    miss = await registry.dispatch(
        ToolCall(id="2", name="search_docs", arguments={"query": "qzxv"}), ctx
    )
    assert "no documentation section matches" in miss.content
    page = await registry.dispatch(
        ToolCall(id="3", name="read_doc", arguments={"page": "faq"}), ctx
    )
    assert '"page": "faq"' in page.content
