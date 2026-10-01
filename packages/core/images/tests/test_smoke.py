"""The smoke test's verdicts, and the reference everything runs."""

from __future__ import annotations

from core.harness.registry import BUILTIN_HARNESSES, harness_fingerprint
from core.images.service import pinned_ref_for
from core.images.smoke import parse_smoke, proven_harness, smoke_script

DIGEST = "sha256:" + "e" * 64


def test_a_tag_is_dropped_when_a_reference_is_pinned() -> None:
    assert pinned_ref_for("ghcr.io/o/godot-node:4.3", DIGEST) == f"ghcr.io/o/godot-node@{DIGEST}"
    assert pinned_ref_for("localhost:5001/p/i:1", DIGEST) == f"localhost:5001/p/i@{DIGEST}"
    assert pinned_ref_for(f"python@{DIGEST}", DIGEST) == f"docker.io/library/python@{DIGEST}"


def test_the_script_only_runs_the_operators_smoke_command() -> None:
    script = smoke_script(BUILTIN_HARNESSES["opencode"])
    assert "opencode --version" in script
    assert "opencode" not in smoke_script(None)


def test_an_image_without_git_fails() -> None:
    out = parse_smoke(0, "pyr-smoke:begin\npyr-smoke:git=\npyr-smoke:end\n")
    assert not out.passed and "git" in out.reason


def test_an_image_without_a_shell_fails_with_a_reason() -> None:
    out = parse_smoke(127, "exec: sh: not found")
    assert not out.passed and "/bin/sh" in out.reason


def test_a_harness_claim_is_believed_only_on_a_matching_version() -> None:
    spec = BUILTIN_HARNESSES["opencode"]
    passed = parse_smoke(
        0,
        "pyr-smoke:begin\npyr-smoke:git=git version 2.39.5\n"
        "pyr-smoke:harness=1.18.33\npyr-smoke:end\n",
    )
    assert passed.passed
    baked = proven_harness("opencode", spec, passed.harness_output)
    assert baked == {
        "key": "opencode",
        "version": "1.18.33",
        "fingerprint": harness_fingerprint(spec),
    }
    assert proven_harness("opencode", spec, "sh: opencode: not found") is None
    assert proven_harness("opencode", spec, "1.17.0") is None
    assert proven_harness("opencode", None, "1.18.33") is None, "a harness this tenant lacks"
