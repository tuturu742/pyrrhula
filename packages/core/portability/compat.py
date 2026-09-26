"""Is this bundle from a platform this deployment can vouch for?

Two version axes travel in a ``.pyr``, and they fail differently on purpose:

- ``pyr_format`` (``core.portability.upcast``) is the **file format**. A bundle in a
  format newer than this build cannot be read at all, so that is a hard failure.
- ``app_version`` (this module) is the **platform** that wrote a bundle this build *can*
  read. Newer is a warning, not a failure: a bundle is data, and the operator importing
  it is the one who knows their deployment. What this version does not understand is
  skipped and reported, which is what the import already does with any unknown content.

A bundle with no recorded version -- everything written before the stamp existed -- is
"unknown", said once, and imports like anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from packaging.version import InvalidVersion, Version

Verdict = Literal["same", "older", "newer", "unknown"]


@dataclass(frozen=True)
class Compatibility:
    verdict: Verdict
    note: str = ""


def compare_app_versions(bundle: str, running: str) -> Compatibility:
    """``bundle`` is the manifest's ``app_version``; ``running`` is this deployment's."""
    recorded = (bundle or "").strip()
    if not recorded:
        return Compatibility(
            "unknown",
            "This bundle does not record the platform version that wrote it (bundles made "
            "before versions were stamped), so compatibility cannot be verified.",
        )
    try:
        theirs, ours = Version(recorded), Version(running)
    except InvalidVersion:
        return Compatibility(
            "unknown",
            f"This bundle records platform version {recorded!r}, which this build cannot "
            f"compare against {running!r}; compatibility cannot be verified.",
        )
    if theirs > ours:
        return Compatibility(
            "newer",
            f"This bundle was written by Pyrrhula {recorded}, newer than this deployment "
            f"({running}). Compatibility cannot be verified. It may still import; anything "
            "it carries that this version does not understand is skipped and reported.",
        )
    return Compatibility("older" if theirs < ours else "same")
