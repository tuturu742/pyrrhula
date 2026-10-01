"""Which coding harnesses a tenant may use.

A harness is an existing agent loop -- opencode, and others later -- run inside the exec
environment in place of the one-shot "emit whole files" codegen path. It is a *registered
capability*, never a built-in feature, for two reasons that both came from asking what a
real deployment needs:

- a tenant may be forbidden one by internal policy, so a built-in has to be maskable;
- a tenant may run a tailored enterprise build of a harness this project has never heard
  of, so registering one must need no code from us.

The shape is ``core.repos.runtimes``': deployment built-ins are the floor, a tenant entry
with the same key wins, and it all lives on ``tenant.settings`` because it is a handful of
small values per tenant and a table would add RLS surface for no query it answers.

**A spec is data.** The invocation is a template over a fixed whitelist of placeholders,
not an expression language, so a harness spec can never run anything Pyrrhula did not put
in the script. It is also admin-scoped: rule 10 forbids user-authored code, and the line
this stays on the right side of is the one ``setup_cmds``/``test_cmd`` already sit on --
operator configuration, never something a persona or a workflow pack can introduce.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select

from core.tenancy.models import Tenant
from core.tenancy.scope import tenant_scope

SETTING_KEY = "harnesses"

# What a `command` template may refer to. Anything else is refused at registration rather
# than left to fail in a container: a typo in a template is otherwise a delegation that
# runs a command with a literal "{promptfile}" in it and reports a confusing exit code.
PLACEHOLDERS = frozenset(
    {"prompt_file", "model", "workdir", "inference_base_url", "inference_token"}
)

# Dialects the inference proxy serves. A spec naming anything else is refused here, at
# registration, because the failure is a configuration error and not a run-time surprise.
WIRE_FORMATS = frozenset({"openai"})

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_MAX_KEY_LEN = 63
_MAX_CMD_LEN = 2000
_MAX_SETUP_CMDS = 10
_MAX_HARNESSES = 20


class InvalidHarnessError(ValueError):
    """A harness registration that cannot be honoured. Always names the field."""


# The deployment's floor. opencode is MIT (Copyright (c) 2025 opencode), so it may be
# named here and bundled into an image; a harness whose licence forbids redistribution is
# registered by the operator instead, which is why `redistributable` is part of the spec.
BUILTIN_HARNESSES: dict[str, dict[str, Any]] = {
    "opencode": {
        # No image: opencode installs into whatever runtime the repo already needs, since
        # it has to run that repo's tests. A deployment that would rather not pay the
        # install on every k8s Job overrides this entry with a pre-baked image.
        "image": "",
        "setup_cmds": ["npm i -g opencode-ai@1.18.33"],
        # --auto: approve permissions not explicitly denied; without it the loop stops at
        # the first edit waiting for a human who is not there.
        # --format json: a parsed event stream, not screen-scraping.
        "command": (
            'opencode run --auto --format json --model pyr/{model} -- "$(cat {prompt_file})"'
        ),
        "env": {
            "OPENCODE_CONFIG": "/root/.config/opencode/opencode.json",
            "PYR_INFERENCE_KEY": "{inference_token}",
        },
        # Written outside the working tree on purpose: opencode drops its config into the
        # cwd otherwise, and every delegation would commit an opencode.json.
        "config_files": {
            "/root/.config/opencode/opencode.json": (
                '{"provider":{"pyr":{"npm":"@ai-sdk/openai-compatible",'
                '"name":"Pyrrhula","options":{"baseURL":"{inference_base_url}",'
                '"apiKey":"{env:PYR_INFERENCE_KEY}"},'
                '"models":{"{model}":{"name":"{model}"}}}}}'
            )
        },
        "wire_format": "openai",
        "licence": "MIT",
        "redistributable": True,
        "enabled": True,
    }
}


def _unknown_placeholders(text: str) -> set[str]:
    """Placeholders in a template that this deployment does not supply.

    ``{env:...}`` is left alone: that is the harness's own indirection, resolved inside the
    container, and is how a key reaches a config file without being written into it.
    """
    found = {name for name in _PLACEHOLDER_RE.findall(text)}
    return {name for name in found if name not in PLACEHOLDERS and not name.startswith("env")}


def validate_spec(key: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Check one registration and return it in storage shape."""
    key = (key or "").strip()
    if not key:
        raise InvalidHarnessError("harness key is required")
    if len(key) > _MAX_KEY_LEN:
        raise InvalidHarnessError(f"harness key is longer than {_MAX_KEY_LEN} characters")
    if not key.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise InvalidHarnessError("harness key may contain letters, digits, '-', '_' and '.' only")

    command = str(spec.get("command") or "").strip()
    if not command:
        raise InvalidHarnessError(f"harness {key!r}: command is required")
    if len(command) > _MAX_CMD_LEN:
        raise InvalidHarnessError(f"harness {key!r}: command is longer than {_MAX_CMD_LEN} chars")
    if "{prompt_file}" not in command:
        raise InvalidHarnessError(
            f"harness {key!r}: command must use {{prompt_file}} -- a harness that is never "
            "told the task would run against whatever it finds"
        )

    wire_format = str(spec.get("wire_format") or "openai").strip()
    if wire_format not in WIRE_FORMATS:
        raise InvalidHarnessError(
            f"harness {key!r}: wire_format {wire_format!r} is not served by this deployment "
            f"(serving: {sorted(WIRE_FORMATS)})"
        )

    setup = [str(c).strip() for c in (spec.get("setup_cmds") or []) if str(c).strip()]
    if len(setup) > _MAX_SETUP_CMDS:
        raise InvalidHarnessError(f"harness {key!r}: at most {_MAX_SETUP_CMDS} setup commands")

    env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
    config_files = {str(k): str(v) for k, v in (spec.get("config_files") or {}).items()}

    for label, text in (
        [("command", command)]
        + [(f"setup_cmds[{i}]", c) for i, c in enumerate(setup)]
        + [(f"env[{k}]", v) for k, v in env.items()]
        + [(f"config_files[{k}]", v) for k, v in config_files.items()]
    ):
        unknown = _unknown_placeholders(text)
        if unknown:
            raise InvalidHarnessError(
                f"harness {key!r}: {label} uses unknown placeholder(s) "
                f"{sorted(unknown)}; this deployment supplies {sorted(PLACEHOLDERS)}"
            )

    return {
        "image": str(spec.get("image") or "").strip(),
        "setup_cmds": setup,
        "command": command,
        "env": env,
        "config_files": config_files,
        "wire_format": wire_format,
        "licence": str(spec.get("licence") or "").strip(),
        "redistributable": bool(spec.get("redistributable", False)),
        "enabled": bool(spec.get("enabled", True)),
    }


def _tenant_entries(settings: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    raw = (settings or {}).get(SETTING_KEY)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue  # a malformed entry is ignored, never raised on a read path
        # An entry may exist purely to mask a built-in, in which case it carries no
        # command of its own -- "we do not allow this here" is a legitimate registration.
        if not value.get("command") and value.get("enabled", True):
            continue
        out[str(key)] = value
    return out


async def resolved_harnesses(tenant_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """Every harness this tenant may select. Built-ins first, its own on top.

    Entries disabled by the tenant are dropped rather than returned with a flag: a caller
    asking "what may I use" should not have to remember to filter, which is how a withheld
    capability ends up offered.
    """
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        settings = tenant.settings if tenant else None
    merged: dict[str, dict[str, Any]] = {**BUILTIN_HARNESSES}
    for key, entry in _tenant_entries(settings).items():
        if not entry.get("enabled", True):
            merged.pop(key, None)  # the tenant has withheld this one
            continue
        merged[key] = entry
    return {key: entry for key, entry in merged.items() if entry.get("enabled", True)}


async def get_harness(tenant_id: uuid.UUID, key: str | None) -> dict[str, Any] | None:
    """The harness a persona named, if the tenant still has it.

    Resolved at use time rather than trusted from the persona row, the same way
    ``core.mcp.client.available_tools`` re-derives rather than believing what was shown
    earlier: withdrawing a harness has to take effect without editing every persona.
    """
    if not key:
        return None
    return (await resolved_harnesses(tenant_id)).get(key)


async def register_harness(tenant_id: uuid.UUID, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Add or replace one of this tenant's harnesses."""
    entry = validate_spec(key, spec)
    if entry.get("image"):
        # A harness image replaces the runtime image, so it answers to the same rules.
        from core.images.namespace import check_image_ref_for_tenant
        from core.repos.image_ref import ImageRefError, normalise_image_ref

        try:
            entry["image"] = normalise_image_ref(str(entry["image"])) or ""
            await check_image_ref_for_tenant(tenant_id, str(entry["image"]))
        except ImageRefError as exc:
            raise InvalidHarnessError(f"harness {key!r}: image: {exc}") from exc
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None:
            raise InvalidHarnessError("no such tenant")
        existing = _tenant_entries(tenant.settings)
        if key.strip() not in existing and len(existing) >= _MAX_HARNESSES:
            raise InvalidHarnessError(f"a tenant may register at most {_MAX_HARNESSES} harnesses")
        tenant.settings = {
            **(tenant.settings or {}),
            SETTING_KEY: {**existing, key.strip(): entry},
        }
    return entry


async def withhold_harness(tenant_id: uuid.UUID, key: str) -> None:
    """Make a harness unavailable to this tenant, built-in or not.

    This is the "internal policy forbids it" case, and it is why a mask exists at all: a
    built-in lives in code, so a tenant cannot delete one. Masking is a registration that
    says no.
    """
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None:
            raise InvalidHarnessError("no such tenant")
        existing = _tenant_entries(tenant.settings)
        tenant.settings = {
            **(tenant.settings or {}),
            SETTING_KEY: {**existing, key.strip(): {"enabled": False}},
        }


async def remove_harness(tenant_id: uuid.UUID, key: str) -> bool:
    """Forget one of this tenant's entries, mask included.

    A built-in of the same name becomes available again, which is how a tenant undoes a
    withholding.
    """
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None:
            return False
        stored = (tenant.settings or {}).get(SETTING_KEY)
        existing: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
        had = key.strip() in existing
        existing.pop(key.strip(), None)
        tenant.settings = {**(tenant.settings or {}), SETTING_KEY: existing}
    return had


def render(text: str, values: dict[str, str]) -> str:
    """Substitute the whitelisted placeholders, and nothing else.

    ``str.format`` is deliberately not used: a template is operator-written but the values
    are not, and ``format`` would honour attribute access and indexing on whatever it was
    handed. This walks the same regex that validation checked.
    """
    return _PLACEHOLDER_RE.sub(lambda match: values.get(match.group(1), match.group(0)), text)
