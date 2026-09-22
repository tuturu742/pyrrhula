"""A tenant's own build images.

The catalog was four entries in a module-level dict, so anything outside it -- Rust, Go,
an internal registry -- had to be 'custom' with a fully qualified image typed into each
repo row. That made the escape hatch the common path and scattered image references
across rows nobody could see or update in one place.
"""

from __future__ import annotations

import pytest

from core.repos.runtimes import (
    BUILTIN_RUNTIMES,
    CUSTOM,
    InvalidRuntimeError,
    _tenant_entries,
    validate_entry,
)


def test_the_builtins_are_the_floor_every_tenant_starts_from() -> None:
    assert {"debian", "node20", "python312", "java21"} <= set(BUILTIN_RUNTIMES)


def test_a_tenant_entry_overrides_a_builtin_of_the_same_name() -> None:
    """The point as much as adding new ones: an air-gapped deployment points `debian` at
    its internal mirror once, instead of every repo carrying the mirror's address."""
    tenant = _tenant_entries({"runtimes": {"debian": {"image": "registry.internal/debian"}}})
    effective = {**BUILTIN_RUNTIMES, **tenant}
    assert effective["debian"]["image"] == "registry.internal/debian"


def test_custom_cannot_be_registered() -> None:
    """It is not a runtime, it is the absence of one. A tenant runtime by that name could
    never be selected, because the name already means 'the repo brings its own image'."""
    with pytest.raises(InvalidRuntimeError, match="reserved"):
        validate_entry(CUSTOM, "docker.io/library/debian", [])


def test_a_registration_needs_an_image() -> None:
    with pytest.raises(InvalidRuntimeError, match="image is required"):
        validate_entry("rust", "", [])


def test_an_image_is_one_reference_not_a_command() -> None:
    with pytest.raises(InvalidRuntimeError, match="single reference"):
        validate_entry("rust", "docker.io/library/rust:1.97 sh -c evil", [])


def test_a_key_is_a_name_not_a_path() -> None:
    with pytest.raises(InvalidRuntimeError, match="letters, digits"):
        validate_entry("../etc", "docker.io/library/debian", [])


def test_setup_commands_are_capped_and_trimmed() -> None:
    entry = validate_entry("rust", "docker.io/library/rust:1.97", ["  cargo --version  ", ""])
    assert entry["setup"] == ["cargo --version"]
    with pytest.raises(InvalidRuntimeError, match="at most"):
        validate_entry("rust", "docker.io/library/rust:1.97", ["x"] * 50)


def test_a_malformed_stored_entry_is_ignored_rather_than_raising_on_read() -> None:
    """Reads happen on the delegation path. One bad row in a settings blob must not take
    down every build in the tenant."""
    entries = _tenant_entries({"runtimes": {"ok": {"image": "a"}, "bad": {"no_image": True}}})
    assert set(entries) == {"ok"}
