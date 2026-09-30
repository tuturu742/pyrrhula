"""A second Pyrrhula on one cluster stays a second Pyrrhula.

``overlays/second-instance`` renames both namespaces and rewrites every reference between
them. The failure mode it guards against is quiet: a cross-namespace reference the overlay
does not know about does not break the render, it points the new instance at the old one.
The worker's RoleBinding would grant Job creation in another instance's namespace; the
egress NetworkPolicy would let environments reach another instance's api and not their own.

So this enumerates the references the overlay handles and fails when the base grows one it
does not -- the alarm being raised where the base changes, not months later when two
deployments turn out to share a permission.

Kustomize is not invoked here: CI has no kubectl, and a test that silently skipped without
one would be exactly the vacuous green this project has been bitten by. The base and the
overlay are both YAML, and reading them is enough to answer the question.
"""

from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASE = ROOT / "deploy" / "k8s" / "base"
OVERLAY = ROOT / "deploy" / "k8s" / "overlays" / "second-instance" / "kustomization.yaml"

# The two namespace names the base ships.
NAMESPACES = {"pyrrhula", "pyrrhula-envs"}

# Where a namespace name legitimately appears, handled generically by the overlay's
# `{namespace: ...}` and `{kind: Namespace}` patches.
GENERIC = {("metadata", "namespace"), ("metadata", "name")}

# Cross-namespace references the overlay patches one by one. Each entry is the path of
# keys leading to a namespace name. Adding a reference to the base means adding a patch
# to the overlay and an entry here -- in that order, ideally.
HANDLED = {
    ("subjects", "namespace"),  # RoleBinding -> the worker's ServiceAccount
    ("matchLabels", "kubernetes.io/metadata.name"),  # NetworkPolicy -> which api
}


# Values that merely *spell* the same word. The database is called `pyrrhula` and its role
# is called `pyrrhula`, and both must stay put when the namespace moves -- renaming them
# would point a second instance at a database that does not exist.
SAME_WORD_DIFFERENT_THING = {"POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD"}


def _documents() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(BASE.glob("*.yaml")):
        # kustomization.yaml is the build file, not a resource: what it declares is
        # rendered and then patched like anything else.
        if path.name == "kustomization.yaml":
            continue
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict):
                out.append((path.name, doc))
    return out


def _namespace_references(node: object, trail: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every place a namespace name appears, with the key path that reached it."""
    found: list[tuple[str, ...]] = []
    if isinstance(node, dict):
        # An env entry is {name, value}: the name says whether the value is a namespace
        # or a database called the same thing.
        if node.get("name") in SAME_WORD_DIFFERENT_THING:
            return found
        for key, value in node.items():
            if isinstance(value, str) and value in NAMESPACES:
                found.append((*trail, str(key)))
            else:
                found.extend(_namespace_references(value, (*trail, str(key))))
    elif isinstance(node, list):
        for item in node:
            found.extend(_namespace_references(item, trail))
    return found


def test_the_base_is_there_to_read() -> None:
    """A scan over nothing proves nothing."""
    documents = _documents()
    assert len(documents) >= 8, f"only found {len(documents)} base resources"


def test_every_namespace_reference_is_one_the_overlay_rewrites() -> None:
    unhandled: dict[str, list[str]] = {}
    for filename, doc in _documents():
        for trail in _namespace_references(doc):
            tail = trail[-2:] if len(trail) >= 2 else trail
            if tail in GENERIC or tail in HANDLED:
                continue
            # A bare `metadata.name` on a Namespace is generic; anywhere else it is a
            # name that happens to match, which is worth a human look.
            if tail == ("metadata", "name") and doc.get("kind") == "Namespace":
                continue
            unhandled.setdefault(filename, []).append(".".join(trail))
    assert not unhandled, (
        f"namespace references the second-instance overlay does not rewrite: {unhandled}. "
        "Add a patch to overlays/second-instance and an entry to HANDLED, or two "
        "deployments on one cluster will quietly share this."
    )


def test_the_overlay_patches_each_handled_reference() -> None:
    """The converse: an entry in HANDLED with no patch behind it is a guard that passes
    while the thing it guards is broken."""
    overlay = OVERLAY.read_text()
    assert "/subjects/0/namespace" in overlay
    assert "kubernetes.io~1metadata.name" in overlay
    # And the two generic rewrites, without which nothing moves at all.
    assert "kind: Namespace, name: pyrrhula}" in overlay
    assert "{namespace: pyrrhula}" in overlay


def test_the_overlay_moves_the_public_url_with_the_ingress() -> None:
    """Preview and notification links are built from PYRRHULA_PUBLIC_BASE_URL. A second
    instance whose ingress moved but whose base URL did not sends people to the first."""
    overlay = OVERLAY.read_text()
    host = "ht.pyrrhula.localhost"
    assert f"value: {host}" in overlay, "the ingress host"
    assert f"value: http://{host}" in overlay, "and the URL built from it"
