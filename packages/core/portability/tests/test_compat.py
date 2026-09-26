"""A bundle from a newer platform warns and proceeds; the file format is the axis that
refuses (see ``core.portability.upcast``). Both directions and the missing case."""

from __future__ import annotations

from core.portability.compat import compare_app_versions


def test_a_newer_bundle_is_a_warning_with_a_sentence() -> None:
    result = compare_app_versions("0.2.0", "0.1.0")
    assert result.verdict == "newer"
    assert "0.2.0" in result.note and "0.1.0" in result.note
    assert "cannot be verified" in result.note


def test_same_and_older_are_silent() -> None:
    same = compare_app_versions("0.1.0", "0.1.0")
    assert same.verdict == "same" and same.note == ""
    older = compare_app_versions("0.0.9", "0.1.0")
    assert older.verdict == "older" and older.note == ""


def test_a_bundle_without_a_version_is_unknown_and_says_so() -> None:
    for recorded in ("", "   "):
        result = compare_app_versions(recorded, "0.1.0")
        assert result.verdict == "unknown"
        assert "does not record" in result.note


def test_garbage_is_unknown_not_an_exception() -> None:
    result = compare_app_versions("yesterday", "0.1.0")
    assert result.verdict == "unknown"
    assert "yesterday" in result.note


def test_prerelease_ordering_follows_pep_440() -> None:
    # An rc precedes its release: a deployment on the release must not warn about a
    # bundle its own rc wrote, and a later patch is newer than an rc.
    assert compare_app_versions("0.1.0rc1", "0.1.0").verdict == "older"
    assert compare_app_versions("0.1.1", "0.1.0rc1").verdict == "newer"
