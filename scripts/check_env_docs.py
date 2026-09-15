#!/usr/bin/env python3
"""Every environment variable the code reads must have a row in docs/configuration.md.

Run in CI. The doc is the audit's output (see `tasks/config-as-settings.md`); a variable
added without a row is a variable nobody will find, and a row left behind after a variable
is removed is a lie about what the deployment reads.

Only **table rows** count as documenting a variable. Prose may name a retired variable to
record that it was removed and where its setting went instead -- that history is worth
keeping and must not read as a live knob.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATTERN = r"(PYRRHULA_[A-Z_0-9]+|PYR_ARTIFACT_[A-Z]+|GH_TOKEN|DEEPSEEK_KEY)"
# Not environment variables: a module constant and a truncated grep match.
NOT_ENV = {"PYRRHULA_EXTENSION_KEY", "PYRRHULA_PLUGINS_LOCAL_"}


def main() -> int:
    doc = (ROOT / "docs" / "configuration.md").read_text()
    documented = {
        name
        for line in doc.splitlines()
        if line.lstrip().startswith("|")
        for name in re.findall(rf"`{PATTERN}`", line)
    }

    found = subprocess.run(
        ["grep", "-rhoE", PATTERN, "packages/", "scripts/", "deploy/", "docker/", "install.sh"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    ).stdout.split()
    used = set(found) - NOT_ENV
    settings = (ROOT / "packages" / "core" / "config.py").read_text()
    used |= {f"PYRRHULA_{f.upper()}" for f in re.findall(r"^    ([a-z_]+):", settings, re.M)}

    # The verification scripts share one documented wildcard row.
    used = {v for v in used if not v.startswith("PYRRHULA_VERIFY")}
    documented = {v for v in documented if not v.startswith("PYRRHULA_VERIFY")}

    undocumented = sorted(used - documented)
    stale = sorted(documented - used)
    for name in undocumented:
        print(f"undocumented: {name} is read by the code but has no row in the reference")
    for name in stale:
        print(f"stale: {name} has a row but nothing reads it -- delete the row or the code")
    if undocumented or stale:
        return 1
    print(f"ok: {len(used)} environment variables, all documented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
