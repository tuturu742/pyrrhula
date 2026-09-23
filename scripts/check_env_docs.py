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


def _documented_defaults(doc: str) -> dict[str, str]:
    """Variable -> the Default cell, for rows that quote a single literal.

    Rows whose default is prose ("generated", "derived from…", "unset", or a sentence
    explaining that two deployments differ) are skipped deliberately: the point is to
    catch a *stated* default drifting away from the code, not to forbid explaining one.
    """
    found: dict[str, str] = {}
    for line in doc.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        names = re.findall(rf"`{PATTERN}`", cells[0])
        literal = re.fullmatch(r"`([^`]*)`", cells[1])
        if len(names) == 1 and literal:
            found[names[0]] = literal.group(1)
    return found


def _settings_defaults() -> dict[str, str]:
    """What core/config.py actually falls back to, as text."""
    from core.config import Settings

    out: dict[str, str] = {}
    for name, field in Settings.model_fields.items():
        default = field.default
        if default is None or repr(default) == "PydanticUndefined":
            continue
        rendered = ("true" if default else "false") if isinstance(default, bool) else str(default)
        out[f"PYRRHULA_{name.upper()}"] = rendered
    return out


def check_defaults(doc: str) -> list[str]:
    """Every documented literal default must match the Settings field it names.

    This column went unaudited for a long time and four of five values sampled in a
    later review were wrong -- a reader configuring from the table got the wrong port,
    the wrong offline flag and an image reference podman will not resolve.
    """
    problems: list[str] = []
    documented = _documented_defaults(doc)
    actual = _settings_defaults()
    for name, stated in documented.items():
        if name not in actual:
            continue  # not a Settings field (compose-only, image-level); nothing to check
        if stated.strip() != actual[name].strip():
            problems.append(
                f"default drift: {name} is documented as {stated!r} but core/config.py "
                f"falls back to {actual[name]!r}"
            )
    return problems


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
    drift = check_defaults(doc)
    for problem in drift:
        print(problem)

    if undocumented or stale or drift:
        return 1
    print(f"ok: {len(used)} environment variables, all documented, defaults agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
