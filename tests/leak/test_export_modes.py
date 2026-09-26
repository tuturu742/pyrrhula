"""the CI-blocking leak test: the export-side counterpart of INV-8.

**The scan is of the artifact, not the code path.** What a customer relies on is a property
of the bytes in the ZIP -- not of which function was called on the way there. Asserting it
at the artifact level catches every future serializer someone bolts on, every field added
to a payload without thinking, and every "just this once" debug dump, none of which a
code-path test would see.

Three detectors, matching its own ladder (`core.secrets.leak_check`):

* **exact** -- the plaintext, or any long enough run of it, appears verbatim;
* **fuzzy** -- enough distinctive words co-occur in one file to reconstruct the fact;
* **embedding** -- a file's text is semantically close to the plaintext.

The embedding arm uses a deterministic local vectoriser rather than a provider: this test
is CI-blocking, and a CI-blocking test that needs network access and an API key is a test
that gets skipped. The vectoriser is crude on purpose -- it is a *detector*, and a
detector's job here is to be sensitive, not accurate. A false alarm costs a developer five
minutes; a miss ships a leak.
"""

from __future__ import annotations

import io
import math
import re
import uuid
import zipfile
from collections import Counter

import pytest
from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.permission.role_permission import RolePermissionService
from core.assembler.visibility import seed_default_scopes
from core.audit.models import AuditLogRow
from core.audit.verify import verify_chain
from core.portability.bundle import open_bundle
from core.portability.crypto import decrypt_bundle, is_encrypted
from core.portability.export import (
    ExportOptions,
    ExportPermissionDeniedError,
    ExportRequiresEncryptionError,
    ExportResult,
    export_workspace,
)
from core.secrets.authoring import add_holder, create_secret
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_PERMISSIONS = RolePermissionService()
_ENCRYPTOR = IdentityEncryptor()
_MODERATION = AllowAllModerationProvider()

# Deliberately distinctive: a canary made of common words would make the fuzzy and
# embedding arms fire on innocent text and prove nothing.
_HELD_CANARY = "the vault beneath the chapterhouse opens to the phrase saltmarsh nightjar eleven"
_UNHELD_CANARY = "the cartographer forged the eastern charts to hide the reef at brackwater shoal"

_WORD = re.compile(r"[a-z0-9']+")
_MIN_SHARED_WORDS = 6
_EXACT_RUN_WORDS = 5
_TAU = 0.6


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _vector(text: str) -> Counter[str]:
    return Counter(_words(text))


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    dot = sum(a[w] * b[w] for w in shared)
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


def _exact_hit(secret: str, blob: str) -> bool:
    """Verbatim, or any run of ``_EXACT_RUN_WORDS`` consecutive secret words. The run check
    matters: a serializer that wrapped or re-flowed the plaintext would defeat a plain
    substring search while leaking every word of it."""
    if secret.lower() in blob.lower():
        return True
    secret_words = _words(secret)
    blob_lower = " ".join(_words(blob))
    return any(
        " ".join(secret_words[i : i + _EXACT_RUN_WORDS]) in blob_lower
        for i in range(len(secret_words) - _EXACT_RUN_WORDS + 1)
    )


def _fuzzy_hit(secret: str, blob: str) -> bool:
    return len(set(_words(secret)) & set(_words(blob))) >= _MIN_SHARED_WORDS


def _embedding_hit(secret: str, blob: str) -> bool:
    return _cosine(_vector(secret), _vector(blob)) > _TAU


def scan_bundle(result: ExportResult, secret: str) -> list[tuple[str, str]]:
    """Every (path, detector) pair that fires. Scans **every byte of every file** in the
    archive, including the manifest, session events, and any file a future task adds --
    the enumeration comes from the ZIP itself, not from a list this test maintains.
    Full-mode bundles are now password-encrypted at rest (the sensitivity gate); this
    scans the DECRYPTED payload -- the leak question is about the ZIP's contents."""
    data = result.data
    if is_encrypted(data):
        data = decrypt_bundle(data, "test-bundle-pw")
    hits: list[tuple[str, str]] = []
    with _open_zip(data) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            blob = archive.read(name).decode("utf-8", errors="replace")
            for detector, fn in (
                ("exact", _exact_hit),
                ("fuzzy", _fuzzy_hit),
                ("embedding", _embedding_hit),
            ):
                if fn(secret, blob):
                    hits.append((name, detector))
    return hits


def _open_zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


async def _setup() -> tuple[uuid.UUID, uuid.UUID, Principal, Principal, Principal]:
    """``(tenant, workspace, author, holder, overseer)`` -- author holds nothing, so
    "author" and "holder" stay visibly different questions."""
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"export-leak-{uuid.uuid4().hex[:8]}"
    )
    await seed_default_scopes(tenant_id, workspace_id)

    principals: list[Principal] = []
    async with tenant_scope(tenant_id) as session:
        for role, name in (
            ("facilitator", "author"),
            ("participant", "holder"),
            ("overseer", "overseer"),
        ):
            principal = Principal(tenant_id=tenant_id, kind="human", display_name=name)
            session.add(principal)
            await session.flush()
            session.add(
                WorkspaceMembership(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    principal_id=principal.id,
                    role=role,
                )
            )
            principals.append(principal)
        await session.flush()
        for principal in principals:
            session.expunge(principal)

    del owner_id
    author, holder, overseer = principals
    return tenant_id, workspace_id, author, holder, overseer


async def _seed_secrets(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, author: Principal, holder: Principal
) -> tuple[uuid.UUID, uuid.UUID]:
    held = await create_secret(
        tenant_id,
        workspace_id,
        author.id,
        subject_kind="entity",
        subject_id=uuid.uuid4(),
        content=_HELD_CANARY,
        gist="a way into the vault",
        scope_key="workspace_public",
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        moderation_provider=_MODERATION,
        hint_text="somebody mentions saltmarsh",
        behavioral_directive="deflect if asked about the chapterhouse",
    )
    unheld = await create_secret(
        tenant_id,
        workspace_id,
        author.id,
        subject_kind="entity",
        subject_id=uuid.uuid4(),
        content=_UNHELD_CANARY,
        gist="the charts are wrong",
        scope_key="workspace_public",
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        moderation_provider=_MODERATION,
    )
    await add_holder(
        tenant_id,
        held.id,
        author.id,
        holder.id,
        "told",  # holder_kind: author | discovered | told
        permission_service=_PERMISSIONS,
    )
    return held.id, unheld.id


async def test_sanitised_bundle_contains_no_held_secret_plaintext(db_available: None) -> None:
    tenant_id, workspace_id, author, holder, _overseer = await _setup()
    held_id, unheld_id = await _seed_secrets(tenant_id, workspace_id, author, holder)

    result = await export_workspace(
        holder,
        tenant_id,
        workspace_id,
        options=ExportOptions(mode="sanitised"),
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
    )

    for canary in (_HELD_CANARY, _UNHELD_CANARY):
        hits = scan_bundle(result, canary)
        assert hits == [], (
            f"sanitised bundle leaked a secret's plaintext: {hits}. The scan covers every "
            "byte of every file, so this is a property of the archive, not of a code path."
        )

    # Also absent are the other three sensitive fields, and the omission is declared.
    reader = open_bundle(result.data)
    joined = "\n".join(reader.read_text(p) for p in reader.files if p.startswith("secrets/"))
    assert "saltmarsh" not in joined
    assert "deflect if asked" not in joined
    redacted = {r["id"] for r in reader.redactions if r["type"] == "secret_content"}
    assert {str(held_id), str(unheld_id)} <= redacted, (
        "a sanitised bundle that omits secrets *silently* is quiet, not honest"
    )
    # The gist still travels -- "there is a secret here" is not itself the secret.
    assert "a way into the vault" in joined


async def test_participant_bundle_limited_to_held_secrets(db_available: None) -> None:
    tenant_id, workspace_id, author, holder, _overseer = await _setup()
    held_id, unheld_id = await _seed_secrets(tenant_id, workspace_id, author, holder)

    result = await export_workspace(
        holder,
        tenant_id,
        workspace_id,
        options=ExportOptions(mode="participant"),
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
    )

    held_hits = scan_bundle(result, _HELD_CANARY)
    assert held_hits, "the participant's own held secret should be in their session log"

    unheld_hits = scan_bundle(result, _UNHELD_CANARY)
    assert unheld_hits == [], (
        f"a participant's bundle carried plaintext of a secret they do not hold: {unheld_hits}"
    )

    reader = open_bundle(result.data)
    assert str(unheld_id) in {r["id"] for r in reader.redactions if r["type"] == "secret_content"}
    assert str(held_id) not in {r["id"] for r in reader.redactions if r["type"] == "secret_content"}

    # The *author*, who holds nothing, gets no plaintext either -- authorship and
    # holding are different questions, and participant mode answers the second.
    author_result = await export_workspace(
        author,
        tenant_id,
        workspace_id,
        options=ExportOptions(mode="participant"),
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
    )
    assert scan_bundle(author_result, _HELD_CANARY) == []


async def test_full_export_requires_inspect_permission(db_available: None) -> None:
    tenant_id, workspace_id, author, holder, overseer = await _setup()
    await _seed_secrets(tenant_id, workspace_id, author, holder)

    for principal in (author, holder):
        with pytest.raises(ExportPermissionDeniedError, match="secret:inspect"):
            await export_workspace(
                principal,
                tenant_id,
                workspace_id,
                options=ExportOptions(mode="full", password="test-bundle-pw"),
                encryptor=_ENCRYPTOR,
                permission_service=_PERMISSIONS,
            )

    # Refused *before* any object selection: nothing was written, so nothing was gathered.
    async with tenant_scope(tenant_id) as session:
        attempts = list(
            (
                await session.execute(
                    select(AuditLogRow).where(AuditLogRow.action == "export:full")
                )
            ).scalars()
        )
    assert attempts == [], "a refused full export still wrote its audit row"

    granted = await export_workspace(
        overseer,
        tenant_id,
        workspace_id,
        options=ExportOptions(mode="full", password="test-bundle-pw"),
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
    )
    assert scan_bundle(granted, _HELD_CANARY), "a full export should contain everything"
    assert scan_bundle(granted, _UNHELD_CANARY)


async def test_full_export_writes_verifiable_audit_row(db_available: None) -> None:
    tenant_id, workspace_id, author, holder, overseer = await _setup()
    await _seed_secrets(tenant_id, workspace_id, author, holder)

    await export_workspace(
        overseer,
        tenant_id,
        workspace_id,
        options=ExportOptions(mode="full", password="test-bundle-pw"),
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
    )

    async with tenant_scope(tenant_id) as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLogRow)
                    .where(AuditLogRow.action == "export:full")
                    .order_by(AuditLogRow.created_at)
                )
            ).scalars()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_principal_id == overseer.id
    assert row.resource_id == workspace_id
    assert row.query == {"mode": "full"}

    # On the same hash chain as every other audit row, so the verifier detects tampering.
    # `verify_chain` returns the ids of rows whose recomputed hash diverges; an empty list
    # means the full-export row linked into the chain correctly rather than beside it.
    assert await verify_chain(tenant_id) == []


# ── publication class: the one thing that may leave in the clear, and only on purpose ──


async def _publishable_workspace() -> tuple[uuid.UUID, uuid.UUID, Principal, Principal]:
    tenant_id, workspace_id, author, _holder, overseer = await _setup()
    return tenant_id, workspace_id, author, overseer


@pytest.mark.asyncio
async def test_a_guarded_secret_blocks_an_unencrypted_full_export(db_available: None) -> None:
    """The rule the publication class is allowed to relax, and the exact point where it
    is not. One guarded secret anywhere in the workspace and a full export must refuse
    without a password -- refusing beats deciding per-secret which plaintext to omit,
    because a "full" bundle that quietly dropped some secrets would be lying.

    Scoped to *full* mode on purpose. Participant mode has always let a holder carry
    their own held secrets out in the clear -- that is 's "my session log", and
    publication does not touch it."""
    tenant_id, workspace_id, author, overseer = await _publishable_workspace()
    await create_secret(
        tenant_id,
        workspace_id,
        author.id,
        subject_kind="workspace",
        subject_id=workspace_id,
        content=_HELD_CANARY,
        gist="the vault phrase",
        scope_key="workspace_public",
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        moderation_provider=_MODERATION,
    )

    with pytest.raises(ExportRequiresEncryptionError):
        await export_workspace(
            overseer,
            tenant_id,
            workspace_id,
            encryptor=_ENCRYPTOR,
            permission_service=_PERMISSIONS,
            options=ExportOptions(mode="full"),
        )


@pytest.mark.asyncio
async def test_a_publishable_workspace_exports_in_the_clear_and_carries_its_briefs(
    db_available: None,
) -> None:
    """The case this exists for: a murder mystery whose character briefs ARE the artefact.
    The bundle is unencrypted and contains the plaintext -- which is the whole point, and
    is exactly what the guarded test above forbids for anything not declared."""
    tenant_id, workspace_id, author, overseer = await _publishable_workspace()
    await create_secret(
        tenant_id,
        workspace_id,
        author.id,
        subject_kind="workspace",
        subject_id=workspace_id,
        content=_HELD_CANARY,
        gist="the character's brief",
        scope_key="workspace_public",
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        moderation_provider=_MODERATION,
        publication="publishable",
    )

    result = await export_workspace(
        overseer,
        tenant_id,
        workspace_id,
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        options=ExportOptions(mode="full"),
    )
    assert not is_encrypted(result.data), "a publishable case must not need a password"
    assert scan_bundle(result, _HELD_CANARY), (
        "the briefs did not travel -- the bundle cannot carry the artefact it exists for"
    )


@pytest.mark.asyncio
async def test_publication_never_relaxes_the_credentials_rule(db_available: None) -> None:
    """Provider credentials are sensitive unconditionally: they spend money and
    impersonate people, and no declaration about the *secrets* may buy an unencrypted
    bundle that contains them."""
    tenant_id, workspace_id, author, overseer = await _publishable_workspace()
    await create_secret(
        tenant_id,
        workspace_id,
        author.id,
        subject_kind="workspace",
        subject_id=workspace_id,
        content=_HELD_CANARY,
        gist="a brief",
        scope_key="workspace_public",
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        moderation_provider=_MODERATION,
        publication="publishable",
    )

    with pytest.raises(ExportRequiresEncryptionError):
        await export_workspace(
            overseer,
            tenant_id,
            workspace_id,
            encryptor=_ENCRYPTOR,
            permission_service=_PERMISSIONS,
            options=ExportOptions(mode="full", sections=frozenset({"secrets", "connections"})),
        )


@pytest.mark.asyncio
async def test_publishing_a_brief_does_not_widen_who_may_read_a_guarded_one(
    db_available: None,
) -> None:
    """Publication is per secret, not per workspace: marking the case shareable must not
    turn the ops note beside it into public reading."""
    from core.secrets.authoring import list_secret_views_for_workspace

    tenant_id, workspace_id, author, _holder, overseer = await _setup()
    await create_secret(
        tenant_id,
        workspace_id,
        author.id,
        subject_kind="workspace",
        subject_id=workspace_id,
        content=_HELD_CANARY,
        gist="the published brief",
        scope_key="workspace_public",
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        moderation_provider=_MODERATION,
        publication="publishable",
    )
    await create_secret(
        tenant_id,
        workspace_id,
        author.id,
        subject_kind="workspace",
        subject_id=workspace_id,
        content=_UNHELD_CANARY,
        gist="the guarded note",
        scope_key="workspace_public",
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        moderation_provider=_MODERATION,
    )

    # The overseer is a member but not an author: the audited inspect() path is how they
    # read a guarded secret, and this list is not that path.
    views = {
        v.gist: v
        for v in await list_secret_views_for_workspace(
            tenant_id,
            workspace_id,
            overseer.id,
            encryptor=_ENCRYPTOR,
            permission_service=_PERMISSIONS,
        )
    }
    assert views["the published brief"].content == _HELD_CANARY
    assert views["the guarded note"].content is None
