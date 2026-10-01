"""Which image references a tenant may use -- the pure rules, every alias spelling."""

from __future__ import annotations

import uuid

import pytest

from core.images.namespace import (
    RegistryNamespace,
    managed_roots,
    normalise_allowlist_entry,
    ref_violation,
    repository_for,
)

TENANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
TENANT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")

LOCAL = RegistryNamespace(
    key="local",
    pull_host="localhost:5000",
    path_prefix="pyrrhula",
    aliases=("127.0.0.1:5000",),
)
HUB = RegistryNamespace(key="hub", pull_host="docker.io", path_prefix="acme", path_style="flat")


def test_an_image_built_for_a_tenant_lives_under_its_full_id() -> None:
    """The full UUID, not a prefix: eight hex characters collide at scale, and a collision
    here would make another tenant's images "your own"."""
    assert repository_for(LOCAL, TENANT_A, "godot") == (
        f"localhost:5000/pyrrhula/t{TENANT_A.hex}/godot"
    )
    assert repository_for(HUB, TENANT_A, "godot") == f"docker.io/acme/pyr-t{TENANT_A.hex}-godot"


def test_a_tenant_may_use_its_own_images() -> None:
    ref = repository_for(LOCAL, TENANT_A, "godot") + ":abc"
    assert ref_violation(ref, TENANT_A, [LOCAL], []) is None


@pytest.mark.parametrize(
    "spelling",
    [
        "localhost:5000/pyrrhula/t{hex}/godot:abc",
        "127.0.0.1:5000/pyrrhula/t{hex}/godot:abc",  # an alias is the same registry
        "LOCALHOST:5000/pyrrhula/t{hex}/godot:abc",
    ],
)
def test_a_tenant_may_not_use_another_tenants_image_by_any_spelling(spelling: str) -> None:
    ref = spelling.format(hex=TENANT_A.hex)
    problem = ref_violation(ref, TENANT_B, [LOCAL], [])
    assert problem is not None
    assert TENANT_A.hex not in problem, "the refusal must not confirm the image exists"


def test_docker_hub_flat_namespaces_are_owned_the_same_way() -> None:
    other = f"docker.io/acme/pyr-t{TENANT_A.hex}-godot:1"
    assert ref_violation(other, TENANT_B, [HUB], []) is not None
    assert ref_violation(f"index.docker.io/acme/pyr-t{TENANT_A.hex}-godot:1", TENANT_B, [HUB], [])
    assert ref_violation(other, TENANT_A, [HUB], []) is None


def test_images_outside_every_managed_area_are_untouched_without_an_allowlist() -> None:
    """Empty allowlist means today's behaviour: any public image may be used."""
    assert ref_violation("python:3.12", TENANT_B, [LOCAL, HUB], []) is None
    assert ref_violation("docker.io/acme/other:1", TENANT_B, [HUB], []) is None


def test_the_allowlist_restricts_everything_else() -> None:
    allow = ["ghcr.io/tuturu742/"]
    assert ref_violation("ghcr.io/tuturu742/godot@sha256:" + "a" * 64, TENANT_B, [], allow) is None
    problem = ref_violation("python:3.12", TENANT_B, [], allow)
    assert problem is not None and "ghcr.io/tuturu742/" in problem


def test_own_images_pass_the_allowlist_because_the_operator_declared_their_registry() -> None:
    ref = repository_for(LOCAL, TENANT_A, "godot") + ":abc"
    assert ref_violation(ref, TENANT_A, [LOCAL], ["ghcr.io/"]) is None


def test_allowlist_entries_must_name_a_registry_and_are_canonicalised() -> None:
    assert normalise_allowlist_entry("ghcr.io") == "ghcr.io/"
    assert normalise_allowlist_entry("index.docker.io/library/") == "docker.io/library/"
    with pytest.raises(ValueError, match="registry host"):
        normalise_allowlist_entry("python")


def test_docker_hub_short_names_match_library_entries() -> None:
    assert ref_violation("python:3.12", TENANT_B, [], ["docker.io/library/"]) is None


def test_managed_roots_cover_every_alias() -> None:
    assert managed_roots(LOCAL) == ["localhost:5000/pyrrhula/", "127.0.0.1:5000/pyrrhula/"]
