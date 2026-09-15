"""An image reference is checked where it is typed, not where it is pulled."""

from __future__ import annotations

import pytest

from core.repos.image_ref import ImageRefError, normalise_image_ref


def test_a_pasted_registry_page_is_refused_with_the_reference_it_should_be() -> None:
    """The mistake this exists for, observed in a live deployment: you find the image on
    Docker Hub, paste the address bar, and store a web page as a runtime image. Nothing
    checked it, so the first sign of trouble was a delegation failing at pull time, in a
    job, far from the field that caused it."""
    with pytest.raises(ImageRefError) as exc:
        normalise_image_ref("https://hub.docker.com/r/barichello/godot-ci/")

    assert "barichello/godot-ci" in str(exc.value), "the message must name the fix"
    assert "web page" in str(exc.value)


def test_official_images_and_other_registries_get_the_same_treatment() -> None:
    for pasted, expected in (
        ("https://hub.docker.com/_/python", "python"),
        ("https://ghcr.io/org/thing", "ghcr.io/org/thing"),
        ("https://quay.io/repository/org/thing", "quay.io/org/thing"),
    ):
        with pytest.raises(ImageRefError, match=expected.replace("/", "/")):
            normalise_image_ref(pasted)


def test_real_references_pass_untouched() -> None:
    """Validation that rejected a legitimate reference would be worse than none: registry
    hosts carry ports, names carry slashes, and a reference may pin a digest."""
    for ref in (
        "barichello/godot-ci:4.2.2",
        "python:3.12-slim",
        "docker.io/library/python:3.12-slim",
        "ghcr.io/org/name:v1.2.3",
        "registry.example.com:5000/team/app:sha-abc123",
        "alpine@sha256:abcdef0123456789",
    ):
        assert normalise_image_ref(ref) == ref


def test_empty_means_use_the_catalog_default() -> None:
    assert normalise_image_ref(None) is None
    assert normalise_image_ref("") is None
    assert normalise_image_ref("   ") is None


def test_obvious_nonsense_is_named_rather_than_stored() -> None:
    with pytest.raises(ImageRefError, match="spaces"):
        normalise_image_ref("my image:latest")
    with pytest.raises(ImageRefError, match="scheme"):
        normalise_image_ref("https://example.com/whatever")
