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


# An install must never ask for credentials. A pinned plugin repo may be private or simply
# unreachable, and git's default is to *prompt* -- which turns an unattended install into a
# hung "Username for 'https://github.com':" and a CI job into one that never finishes. Fail
# fast instead, so the documented fallback below (built-in workflows, plus a manual pack drop
# or admin upload) is what a user actually gets.
_GIT_NONINTERACTIVE = {
    "GIT_TERMINAL_PROMPT": "0",
    # A configured credential helper still works; these only stop the *interactive* paths --
    # an askpass helper that would pop a GUI or read the tty yields an empty credential, and
    # with terminal prompts off git then gives up immediately instead of blocking.
    "GIT_ASKPASS": "/bin/true",
    "SSH_ASKPASS": "/bin/true",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
}


def _run(*args: str, cwd: pathlib.Path | None = None) -> None:
    subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_NONINTERACTIVE},
        timeout=300,
    )


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
# (name, the ref on disk, the ref the pin asks for, why the fetch failed)
stale: list[tuple[str, str, str, str]] = []


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
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            # Not fatal. The platform ships `builtin-workflows/` precisely so a fresh
            # install has a working "Default" workflow before any plugin repository
            # exists, and the image copies it. An unreachable plugin repo should cost
            # the extra workflows, not the whole install.
            reason = _redact(str(exc)).splitlines()[0][:160]
            # Absent and STALE are different failures and only one of them is harmless.
            # A directory left from an earlier ref keeps building into the image, so the
            # deploy ships content the pin does not name and reports success either way.
            # Observed: three pack commits written, pushed and pinned, none of which
            # reached a container, across several green deploys.
            had = dest.exists() and any(dest.iterdir())
            if had and stamp.exists() and stamp.read_text().strip() != want:
                stale.append((name, stamp.read_text().strip(), want, reason))
            else:
                failures.append((name, reason))
            dest.mkdir(parents=True, exist_ok=True)
            continue
        stamp.write_text(want + "\n")
        fetched.append(name)
    return fetched


if __name__ == "__main__":
    names = fetch(force="--force" in sys.argv)
    print(f"plugins ready under {PLUGINS_DIR} (fetched: {', '.join(names) or 'cached'})")
    for name, on_disk, wanted, reason in stale:
        print(
            f"WARNING: the {name!r} plugin on disk is NOT the pinned one.\n"
            f"         on disk: {on_disk}\n"
            f"         pinned : {wanted}\n"
            f"         fetch failed: {reason}\n"
            f"         The build will use what is on disk, so pack changes you have\n"
            f"         committed will not be in this image. Set PYRRHULA_PLUGINS_TOKEN\n"
            f"         (or GH_TOKEN) for a private repo, or PYRRHULA_PLUGINS_STRICT=1\n"
            f"         to make this a build failure instead of a warning.",
            file=sys.stderr,
        )
    if stale and os.environ.get("PYRRHULA_PLUGINS_STRICT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        sys.exit(1)
    for name, reason in failures:
        print(
            f"NOTE: could not fetch the {name!r} workflow plugin -- {reason}\n"
            f"      This is not a failure: the install continues with the built-in\n"
            f"      workflows, which are enough to run the platform. That repo is\n"
            f"      private or unreachable from here, and no credentials were asked\n"
            f"      for by design. To add the extra packs later, pick either:\n"
            f"        - drop them in a folder: copy each pack directory (the one with\n"
            f"          plugin.json) into {PLUGINS_DIR.name}/ before building, or into the\n"
            f"          deployment's plugin drop directory (PYRRHULA_PLUGIN_DROP_DIR,\n"
            f"          default /app/plugins-local) and restart -- it is picked up on boot;\n"
            f"        - upload them: admin console -> Plugin repositories -> Upload pack\n"
            f"          (.zip or .tar.gz), no git and no restart needed.",
            file=sys.stderr,
        )
