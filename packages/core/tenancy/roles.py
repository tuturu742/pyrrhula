"""Workspace role names, and the one implication among them.

Two kinds of role check live in this codebase and must not be confused:

* **What may this role DO?** -- answered by ``role_permission`` data through the
  ``PermissionService`` port. ``steward`` has its own rows there (the union of
  facilitator's and overseer's), so that path needs nothing from this module.
* **Is this role one the caller is looking for by name?** -- scope membership
  (``assembler.visibility``), notification targeting (``sessions.notifications``), and the
  overseer-presence requirement (``overseer.workspace_requirements``) all match a
  stored role against a wanted role name. That is where implication has to be applied, and
  it is declared here once so the three sites cannot drift.

``steward`` is the solo workspace's combined seat. A workspace's creator is written as a
steward so one person can both build the room (author secrets, knowledge, flows -- the
facilitator half) and watch it (inspect secrets, read the audit -- the overseer half).
Multi-human workspaces still assign facilitator and overseer separately, because there the
separation between who authors a secret and who is entitled to inspect it is exactly what
makes the audited ``inspect()`` path mean something. A steward collapses that distinction
only for the person who is, unavoidably, both.
"""

from __future__ import annotations

WORKSPACE_ROLES: frozenset[str] = frozenset(
    {"steward", "facilitator", "participant", "overseer", "viewer"}
)

# held role -> the role names it also satisfies. Deliberately not reflexive here; every
# consumer treats an exact match as satisfying, and folding that in would just make the
# table lie about what is an *implication* versus what is identity.
_IMPLIES: dict[str, frozenset[str]] = {
    "steward": frozenset({"facilitator", "overseer"}),
}


def role_satisfies(held: str, wanted: str) -> bool:
    """Does a principal holding ``held`` count as ``wanted`` for a name-based check?

    True when they are the same role, or when ``held`` implies ``wanted``. Used only for
    role-*name* matching -- never as a permission check, which goes through the port."""
    return held == wanted or wanted in _IMPLIES.get(held, frozenset())


def roles_satisfying(wanted: str) -> frozenset[str]:
    """Every stored role name that satisfies ``wanted`` -- for pushing a name-based check
    down into a SQL ``role IN (...)`` predicate instead of post-filtering in Python."""
    return frozenset(r for r in WORKSPACE_ROLES if role_satisfies(r, wanted))
