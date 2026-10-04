"""What is in a bundle, and what of it is already here -- without importing any of it.

Import is additive by construction: colliding keys fork rather than overwrite, so that no
import can destroy work (see ``import_.py``'s module docstring, which argues that case and
is right about it). The cost only shows up on the *second* import of the same bundle, which
is the ordinary way a sample gets updated: every unchanged source forks to `<key>-imported`
and the workspace quietly doubles.

Nothing about that is fixable inside the import itself without giving up the property that
makes import safe. What was missing is the step before it -- being shown what the file
holds, which of it is already present, and being allowed to say "just the personas". This
module is that step, and it deliberately shares ``open_bundle``/``verify_bundle`` with the
importer rather than parsing the archive its own way: a preview that reads the file
differently from the thing that acts on it is a preview of something else.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from core.knowledge.models import KnowledgeSource
from core.portability.bundle import BundleIntegrityError, open_bundle, verify_bundle
from core.portability.compat import compare_app_versions
from core.portability.upcast import upcast_to_current
from core.ports.encryptor import Encryptor
from core.tenancy.scope import tenant_scope
from core.version import APP_VERSION

# The sections an importer can be asked for, in the order import_bundle runs them. Names
# are the vocabulary the API and the UI speak, so they are stable and domain-neutral.
SECTIONS: tuple[str, ...] = (
    "knowledge",
    "entities",
    "sessions",
    "personas",
    "scopes",
    "flows",
    "rules",
    "vocabulary",
    "secrets",
    # Exact, digest-pinned runtime images. Each is checked again on arrival -- digest,
    # allowlist, smoke test on this organization's engine -- before any repo can use it.
    "images",
)


@dataclass(frozen=True)
class BundleItem:
    """One named thing in a bundle, and whether this tenant already has that name."""

    key: str
    name: str
    # True when a row with this key already exists here. Importing it anyway is allowed
    # and safe -- it forks to `<key>-imported` -- but it is the thing a reader wants to
    # know before pressing the button, because it is how a workspace doubles.
    collides: bool = False


@dataclass
class BundleInspection:
    tenant_ref: str = ""
    workflow_key: str = ""
    app_version: str = ""
    exported_at: str = ""
    encrypted: bool = False
    # Against the running platform: same | older | newer | unknown, and the sentence
    # to show when it is not simply fine. See core.portability.compat.
    compatibility: str = "same"
    compatibility_note: str = ""
    sections: dict[str, list[BundleItem]] = field(default_factory=dict)

    @property
    def collisions(self) -> int:
        return sum(1 for items in self.sections.values() for i in items if i.collides)


def _json(raw: bytes) -> dict[str, Any]:
    import json

    loaded: dict[str, Any] = json.loads(raw)
    return loaded


def _knowledge_items(files: dict[str, bytes]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path, raw in files.items():
        if path.startswith("knowledge/") and path.endswith("/meta.json"):
            meta = _json(raw)
            out.append((str(meta.get("key") or ""), str(meta.get("name") or "")))
    return out


def _persona_items(files: dict[str, bytes]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path, raw in files.items():
        # Personas travel under agents/ -- the bundle predates the rename and the format
        # is versioned, so the directory keeps its old spelling on purpose.
        if path.startswith("agents/") and path.endswith(".json"):
            doc = _json(raw)
            if "persona_type" not in doc:
                continue
            out.append((str(doc.get("key") or ""), str(doc.get("name") or "")))
    return out


def _simple_items(files: dict[str, bytes], prefix: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path, raw in files.items():
        if path.startswith(prefix) and path.endswith(".json"):
            doc = _json(raw)
            key = str(doc.get("key") or doc.get("name") or path.rsplit("/", 1)[-1])
            out.append((key, str(doc.get("name") or key)))
    return out


async def _resident_keys(tenant_id: uuid.UUID) -> dict[str, set[str]]:
    """Keys already in this tenant, per section. Only the sections whose collisions are
    worth showing: a forked flow is visible in one list, a forked knowledge source is the
    one that doubles a workspace's retrieval."""
    from core.agents.models import Persona
    from core.process.models import ProcessDefinitionRow

    async with tenant_scope(tenant_id) as session:
        knowledge = set((await session.execute(select(KnowledgeSource.key))).scalars())
        personas = set((await session.execute(select(Persona.key))).scalars())
        flows = set((await session.execute(select(ProcessDefinitionRow.key))).scalars())
    return {"knowledge": knowledge, "personas": personas, "flows": flows}


async def inspect_bundle(
    data: bytes,
    tenant_id: uuid.UUID,
    *,
    password: str | None = None,
    encryptor: Encryptor | None = None,
) -> BundleInspection:
    """Open and verify a bundle, then describe it. Writes nothing.

    Verification is not skipped for a preview: showing someone the contents of a file
    whose integrity map does not match would be describing something the importer would
    then refuse, which is worse than refusing here.
    """
    from core.portability.crypto import decrypt_bundle, is_encrypted

    was_encrypted = is_encrypted(data)
    if was_encrypted:
        data = decrypt_bundle(data, password or "")

    reader = open_bundle(data)
    broken = verify_bundle(reader)
    if broken:
        raise BundleIntegrityError(
            f"{len(broken)} file(s) do not match the manifest's integrity map: {broken}"
        )
    files, manifest = upcast_to_current(dict(reader.files), dict(reader.manifest))

    resident = await _resident_keys(tenant_id)
    found: dict[str, list[tuple[str, str]]] = {
        "knowledge": _knowledge_items(files),
        "personas": _persona_items(files),
        "flows": _simple_items(files, "process/"),
        "vocabulary": _simple_items(files, "vocabulary/"),
        "scopes": _simple_items(files, "scopes/"),
        "entities": _simple_items(files, "entities/"),
        "secrets": _simple_items(files, "secrets/"),
        "rules": _simple_items(files, "rules/"),
        "images": _simple_items(files, "images/"),
    }
    session_refs = sorted(
        {p.split("/")[1] for p in files if p.startswith("sessions/") and "/" in p[9:]}
    )
    found["sessions"] = [(ref, ref) for ref in session_refs]

    inspection = BundleInspection(
        tenant_ref=str(manifest.get("tenant_ref") or ""),
        workflow_key=str(manifest.get("workflow_key") or ""),
        app_version=str(manifest.get("app_version") or ""),
        exported_at=str(manifest.get("exported_at") or ""),
        encrypted=was_encrypted,
    )
    compat = compare_app_versions(inspection.app_version, APP_VERSION)
    inspection.compatibility = compat.verdict
    inspection.compatibility_note = compat.note
    for section in SECTIONS:
        items = found.get(section) or []
        taken = resident.get(section, set())
        inspection.sections[section] = [
            BundleItem(key=k, name=n or k, collides=bool(k) and k in taken)
            for k, n in sorted(items)
        ]
    return inspection
