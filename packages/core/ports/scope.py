"""``ScopeSet`` (INV-4): the branded return type of
``core.assembler.visibility.scopes_for``. Retrieval call sites type their scope argument
as ``ScopeSet`` rather than a plain ``frozenset[str]`` so a hand-built set that never went
through the resolver doesn't typecheck -- "forgetting to resolve visibility first" becomes
a compile error, the same discipline INV-4 already applies to "forgetting the filter
entirely" (a required, defaultless argument).

Lives in ``core.ports`` (a dependency-free layer already imported by both
``core.knowledge.retrieval`` and, once it exists, ``core.assembler``) rather than inside
``core.assembler`` itself, specifically to avoid a reverse dependency: INV-1's whole
import-graph shape is assembler/overseer depending on knowledge, never the other way
around, and ``core.knowledge.retrieval.*`` needs this type as much as
``core.assembler.visibility`` does.
"""

from __future__ import annotations


class ScopeSet(frozenset[str]):
    """A set of scope keys a specific principal is legitimately entitled to read in a
    specific phase (or the EXPORT pseudo-phase). Structurally just a ``frozenset[str]``
    -- the branding is in the type, not the runtime value -- so it drops into any code
    that already expects ``frozenset[str]``/``Sequence[str]`` without conversion."""
