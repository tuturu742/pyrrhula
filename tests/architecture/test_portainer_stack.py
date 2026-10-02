"""The Portainer stack file is the release file plus exactly what Portainer needs.

Portainer (2.39) refused the release file three ways on a real install: a mounted settings
file it cannot provide, ``${VAR:?…}`` guards it evaluates before applying the stack's
variables, and long-form ``depends_on``. The generator answers each; this holds it to
that, and holds it to the release file for everything else.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "portainer_stack", ROOT / "deploy" / "installers" / "portainer_stack.py"
)
assert _spec and _spec.loader
portainer_stack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(portainer_stack)


def _stack() -> tuple[dict, dict, str]:
    release_text = (ROOT / "docker" / "compose.release.yml").read_text()
    settings = (ROOT / "docker" / "searxng-settings.yml").read_text()
    text = portainer_stack.portainer_stack(release_text, settings)
    # The guards are the one intended difference in values; compare without them.
    unguarded = re.sub(r"\$\{([A-Z_]+):\?[^}]*\}", r"${\1}", release_text)
    return yaml.safe_load(unguarded), yaml.safe_load(text), text


def test_what_portainer_refused_is_gone() -> None:
    _, stack, text = _stack()
    assert ":?" not in text, "a required-variable guard survived"
    for name, service in stack["services"].items():
        depends = service.get("depends_on", [])
        assert isinstance(depends, list), f"{name}: depends_on must be a list for Portainer"
        for volume in service.get("volumes", []):
            assert not str(volume).startswith("./"), f"{name}: a file beside the stack ({volume})"
    assert stack["services"]["migrate"]["restart"] == "on-failure"


def test_searxng_gets_its_settings_inline() -> None:
    _, stack, _ = _stack()
    settings = yaml.safe_load((ROOT / "docker" / "searxng-settings.yml").read_text())
    inline = yaml.safe_load(stack["configs"]["searxng_settings"]["content"])
    assert inline == settings
    targets = [c["target"] for c in stack["services"]["searxng"]["configs"]]
    assert targets == ["/etc/searxng/settings.yml"]


def test_everything_else_is_the_release_file() -> None:
    release, stack, _ = _stack()
    assert set(stack["services"]) == set(release["services"])
    for name, service in release["services"].items():
        got = stack["services"][name]
        for key in ("image", "command", "ports", "networks", "environment", "user"):
            assert got.get(key) == service.get(key), f"{name}.{key} drifted from the release file"
