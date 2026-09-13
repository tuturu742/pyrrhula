"""Fetch the pinned workflow-plugin repositories into ``.plugins/<name>/``.

``deploy/plugins.json`` pins each plugin repo to an exact commit; CI, local test runs,
and image builds call this script so pack content (workflow manifests + schemas/
processes/rule systems) is present at a reproducible ref. The runtime story is
different: deployments sync plugin repos through the admin console (the
``plugin_repository`` registry) -- this script only serves the build/test path.

Dev override: ``PYRRHULA_PLUGINS_LOCAL_<name>=/path/to/checkout`` copies a local
working tree instead of cloning (useful while editing a plugin before pushing).

Usage: ``python scripts/fetch_plugins.py [--force]``
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGINS_FILE = REPO_ROOT / "deploy" / "plugins.json"
PLUGINS_DIR = REPO_ROOT / ".plugins"


def _run(*args: str, cwd: pathlib.Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _authenticated(url: str) -> str:
    """Add a token to an https GitHub URL when one is configured.

    A pinned plugin repo may be private -- CI has no interactive credentials, and an
    unauthenticated clone of a private repo fails with a message about a missing username
    that says nothing about the real cause. ``PYRRHULA_PLUGINS_TOKEN`` (or ``GH_TOKEN``)
    is used if present; without one, behaviour is unchanged."""
    token = os.environ.get("PYRRHULA_PLUGINS_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token or not url.startswith("https://github.com/"):
        return url
    return url.replace("https://", f"https://x-access-token:{token}@", 1)


def _redact(text: str) -> str:
    """Never let a token reach a log. git echoes the URL it was given on failure, and
    that URL may carry the credential."""
    for token in (os.environ.get("PYRRHULA_PLUGINS_TOKEN"), os.environ.get("GH_TOKEN")):
        if token:
            text = text.replace(token, "***")
    return text


def _clone_into(url: str, ref: str, dest: pathlib.Path) -> None:
    """Clone to a sibling temp directory, then swap it in.

    Never remove the working copy before its replacement exists: the previous version
    rmtree'd ``dest`` and *then* cloned, so an unreachable repo (a private one, a network
    blip) left the checkout with no packs at all and an image build that could not even
    COPY them."""
    staging = dest.with_name(dest.name + ".incoming")
    if staging.exists():
        shutil.rmtree(staging)
    try:
        _run("git", "clone", "--quiet", _authenticated(url), str(staging))
        _run("git", "checkout", "--quiet", ref, cwd=staging)
        shutil.rmtree(staging / ".git")
        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


failures: list[tuple[str, str]] = []


def fetch(force: bool = False) -> list[str]:
    spec = json.loads(PLUGINS_FILE.read_text())
    fetched: list[str] = []
    for name, entry in spec.items():
        dest = PLUGINS_DIR / name
        stamp = dest / ".ref"
        local = os.environ.get(f"PYRRHULA_PLUGINS_LOCAL_{name}")
        want = f"local:{local}" if local else f"{entry['url']}@{entry['ref']}"
        if not force and stamp.exists() and stamp.read_text().strip() == want:
            continue
        try:
            if local:
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(local, dest, ignore=shutil.ignore_patterns(".git"))
            else:
                _clone_into(entry["url"], entry["ref"], dest)
        except (subprocess.CalledProcessError, OSError) as exc:
            # Not fatal. The platform ships `builtin-workflows/` precisely so a fresh
            # install has a working "Default" workflow before any plugin repository
            # exists, and the image copies it. An unreachable plugin repo should cost
            # the extra workflows, not the whole install.
            failures.append((name, _redact(str(exc)).splitlines()[0][:160]))
            dest.mkdir(parents=True, exist_ok=True)
            continue
        stamp.write_text(want + "\n")
        fetched.append(name)
    return fetched


if __name__ == "__main__":
    names = fetch(force="--force" in sys.argv)
    print(f"plugins ready under {PLUGINS_DIR} (fetched: {', '.join(names) or 'cached'})")
    for name, reason in failures:
        print(
            f"WARNING: could not fetch the {name!r} workflow plugin -- {reason}\n"
            f"         Install continues with the built-in workflows only. If that repo "
            f"is private or unreachable from here, either make it reachable or point\n"
            f"         deploy/plugins.json at a copy you can read.",
            file=sys.stderr,
        )
