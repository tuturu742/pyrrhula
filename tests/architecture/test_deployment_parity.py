"""The three deployment paths configure the same product.

Compose, k8s and AWS each stand up Pyrrhula, and each drifted independently: AWS shipped
no admin bootstrap at all (so the documented "sign in as platform admin" step had no
account to sign into), and it baked in an assistant model every other path had
deliberately emptied.

This does not demand the three be identical -- they legitimately differ, and where they
do the difference should be a decision someone wrote down. It demands that the knobs a
deployment cannot work without are present in all three.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Without these a deployment is either unreachable, unsafe, or has no way in.
_LOAD_BEARING = {
    "PYRRHULA_JWT_SECRET",
    "PYRRHULA_ENCRYPTION_KEY",
    "PYRRHULA_DATABASE_URL",
    "PYRRHULA_APP_DATABASE_URL",
    "PYRRHULA_REDIS_URL",
    "PYRRHULA_ADMIN_EMAIL",
    "PYRRHULA_ADMIN_PASSWORD",
}


def _names(path: str) -> set[str]:
    return set(re.findall(r"PYRRHULA_[A-Z_0-9]+", (ROOT / path).read_text()))


def _k8s_names() -> set[str]:
    """k8s splits the contract in two: non-secret knobs sit in the ConfigMap already
    prefixed, while secrets live in a generated secrets.env under their BARE names and
    are prefixed at mount time (`prefix: PYRRHULA_` on the secretRef in base/api.yaml).
    Read both, or every secret looks absent."""
    names = _names("deploy/k8s/base/kustomization.yaml")
    generated = (ROOT / "deploy/k8s/dev-up.sh").read_text()
    for bare in re.findall(r"^([A-Z][A-Z_0-9]+)=", generated, re.M):
        names.add(f"PYRRHULA_{bare}")
    for bare in re.findall(r'"([A-Z][A-Z_0-9]+)=', generated):
        names.add(f"PYRRHULA_{bare}")
    return names


def test_every_deployment_path_can_produce_a_platform_admin() -> None:
    compose = _names("docker/compose.selfhost.yml")
    kustomize = _k8s_names()
    aws = _names("deploy/aws/ecs.tf")

    for label, present in (("compose", compose), ("k8s", kustomize), ("aws", aws)):
        missing = sorted(_LOAD_BEARING - present)
        assert not missing, (
            f"{label} configures no {missing} -- a deployment missing these either cannot "
            "start or has no way to sign in as a platform admin"
        )


def test_no_deployment_bakes_in_an_assistant_model() -> None:
    """A fresh install has no provider credential. Naming a model anyway points it at
    something it cannot reach, which is exactly what every path removed once already."""
    aws = (ROOT / "deploy/aws/variables.tf").read_text()
    match = re.search(r'variable "assistant_model".*?default\s*=\s*"([^"]*)"', aws, re.S)
    assert match is not None, "the assistant_model variable moved; update this guard"
    assert match.group(1) == "", (
        f"AWS defaults the assistant model to {match.group(1)!r}; every other path ships "
        "it empty so a fresh install does not point at a model it has no key for"
    )


def test_the_example_env_lists_what_the_compose_file_consumes() -> None:
    """docker/env.example says "fill in every required value", so it has to name them."""
    example = _names("docker/env.example")
    for name in ("PYRRHULA_ADMIN_EMAIL", "PYRRHULA_ADMIN_PASSWORD"):
        assert name in example, (
            f"{name} is consumed by compose.selfhost.yml but absent from env.example; "
            "following the manual quickstart then yields no admin account"
        )
