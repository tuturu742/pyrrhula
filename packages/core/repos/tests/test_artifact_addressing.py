"""Artifacts are addressed by ref, not just by repository."""

from __future__ import annotations

from core.repos.service import (
    artifact_blob_key,
    artifact_ref_slug,
    mint_artifact_read_token,
    verify_artifact_read_token,
)


def test_two_branches_of_one_repo_do_not_share_a_blob_key() -> None:
    """Regression (silent data loss): the key was `artifacts/<store>/<name>`, and
    `artifact_name` is one fixed string in the repo's config -- so every branch wrote the
    same place. Two delegations running at once overwrote each other and a preview served
    whichever finished last, with nothing recording that it happened. Six parallel
    delegations were a routine thing to ask for."""
    a = artifact_blob_key("t1-loxia", "game.tar.gz", "pyr/feature-a")
    b = artifact_blob_key("t1-loxia", "game.tar.gz", "pyr/feature-b")

    assert a != b
    assert a.startswith("artifacts/t1-loxia/refs/")


def test_slugging_a_ref_cannot_collide_two_different_refs() -> None:
    """Slugging is lossy -- `feat/login` and `feat-login` flatten to the same characters --
    so the readable part alone would hand two branches one blob key and one container
    name. The digest is what makes the slug injective."""
    assert artifact_ref_slug("feat/login") != artifact_ref_slug("feat-login")
    assert artifact_ref_slug("main") == artifact_ref_slug("main")
    # Still a usable DNS label once a preview name is built around it.
    slug = artifact_ref_slug("refs/heads/a-very-long-branch-name-that-keeps-on-going-forever")
    assert len(slug) <= 33 and slug.replace("-", "").isalnum()


def test_no_ref_keeps_the_pre_ref_key() -> None:
    """Artifacts uploaded before this existed must stay readable, which is what lets the
    download path fall back instead of 404ing every existing preview on upgrade."""
    assert artifact_blob_key("t1-loxia", "game.tar.gz") == "artifacts/t1-loxia/game.tar.gz"


def test_an_artifact_token_is_scoped_to_its_ref() -> None:
    """A preview of one branch must not be able to read another branch's build just by
    asking for it -- the token names the ref as well as the artifact."""
    token = mint_artifact_read_token("t1-loxia", "game.tar.gz", ttl_seconds=60, git_ref="main")

    assert verify_artifact_read_token(token, "t1-loxia", "game.tar.gz", "main") is True
    assert verify_artifact_read_token(token, "t1-loxia", "game.tar.gz", "other") is False
    assert verify_artifact_read_token(token, "t1-loxia", "game.tar.gz") is False
