"""Print a Portainer stack file for Pyrrhula, derived from docker/compose.release.yml.

    python deploy/installers/portainer_stack.py > pyrrhula-stack.yml

Paste the output into Portainer (Stacks -> Add stack -> Web editor) and set the variables
listed in docs/install.md. Derived rather than kept as a second copy, so it cannot drift
from the release file. Three things Portainer needs that the release file cannot carry:

1. **SearXNG's settings travel inline** as a compose ``configs:`` entry. A stack written in
   Portainer has no files beside it, and the release file mounts ``./searxng-settings.yml``.
2. **Required-variable guards** (``${VAR:?message}``) become ``${VAR}``. Portainer validates
   the file before it applies the stack's variables, so a guard fails even when the
   variable is set. docs/install.md lists the ones that must be.
3. **``depends_on`` in list form.** Portainer's validator refuses the long form with
   ``condition:``. The ordering those conditions gave is recovered by restarts: ``migrate``
   retries until Postgres answers, and ``api``/``worker`` (``restart: always``) come up once
   the schema exists.
"""

from __future__ import annotations

import pathlib
import re
import sys

DOCKER = pathlib.Path(__file__).resolve().parents[2] / "docker"


def short_depends_on(text: str) -> str:
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line == "    depends_on:":
            in_block = True
            out.append(line)
            continue
        if in_block:
            if re.match(r"^      [a-z_-]+:\s*$", line):
                out.append(f"      - {line.strip()[:-1]}")
                continue
            if line.startswith("      - "):
                out.append(line)
                continue
            if re.match(r"^        condition:", line):
                continue
            in_block = False
        out.append(line)
    return "\n".join(out) + "\n"


def portainer_stack(release: str, searxng_settings: str) -> str:
    mount = "    volumes:\n      - ./searxng-settings.yml:/etc/searxng/settings.yml:ro\n"
    if mount not in release:
        raise SystemExit("compose.release.yml no longer mounts searxng-settings.yml as expected")
    inline = (
        "    configs:\n"
        "      - source: searxng_settings\n"
        "        target: /etc/searxng/settings.yml\n"
    )
    text = release.replace(mount, inline)
    text = re.sub(r"\$\{([A-Z_]+):\?[^}]*\}", r"${\1}", text)
    text = short_depends_on(text)
    text = text.replace(
        '    command: ["migrate"]\n', '    command: ["migrate"]\n    restart: on-failure\n', 1
    )
    if "$" in searxng_settings:
        raise SystemExit("searxng-settings.yml contains '$'; escape it for compose interpolation")
    body = "".join(f"      {line}\n" if line else "\n" for line in searxng_settings.splitlines())
    return text + "\nconfigs:\n  searxng_settings:\n    content: |\n" + body


def main() -> None:
    sys.stdout.write(
        portainer_stack(
            (DOCKER / "compose.release.yml").read_text(),
            (DOCKER / "searxng-settings.yml").read_text(),
        )
    )


if __name__ == "__main__":
    main()
