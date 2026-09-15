"""A git repository as knowledge (G4.15, D15, plan §14.5/§6.1/§16.6).

**Pyrrhula holds no clone and no worktree.** A snapshot pinned to a commit SHA is ingested
through the existing A1.x pipeline into KnowledgeSources, and that is all. D15's rejected
list exists to keep rule 10 unambiguous: the moment core holds a worktree, someone will run
something in it. The coding agent reads the code natively; retrieval only needs to *brief*.

**One `knowledge_source_version` per SHA.** A1.1's content-addressed version DAG maps onto
the commit DAG without translation -- re-ingesting the same SHA is a no-op because the
content hash is unchanged, and a new SHA is a new version whose diff (A1.8) shows only what
actually changed. That is not a coincidence worth engineering around; it is why the version
model was built content-addressed.

**Path-glob -> class mapping is data.** `docs/adr/**` and `CLAUDE.md` are `rules`;
`docs/**`, `README*`, `tasks/**` are `lore`; everything else is `misc`. The mapping lives
on the ingestion request, defaulted here, so a workspace can override it without a code
change.

**Docs-first chunking is a scoping decision with reasons** (§15.9): the A1.x pipeline
already does markdown well, the dogfood pilot's working set is entirely markdown, and
code-aware chunking is a retrieval-research project with its own eval. Source files ingest
into `misc` with `experimental: true` on the entry so a later evaluation can find exactly
what was chunked naively.

**Two scans at ingestion, both quarantining**: G4.6's injection scanner (a README is
attacker-controlled text headed for a tool-calling agent's context) and the declarative
committed-credential scan. The second is **posture, not INV-8** -- the assembler cannot
exclude a secret nobody registered, and this module's docstring says so rather than letting
a reader infer a guarantee.
"""

from __future__ import annotations

import fnmatch
import io
import tarfile
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, text

from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    list_versions,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.models import KnowledgeSource, KnowledgeSourceVersion
from core.knowledge.secret_patterns import quarantine_reason as secret_reason
from core.knowledge.secret_patterns import scan_for_secrets
from core.portability.injection_scan import quarantine_reason as injection_reason
from core.portability.injection_scan import scan_text
from core.tenancy.scope import tenant_scope

# The shipped default. Ordered: first match wins, so the specific `docs/adr/**` rule beats
# the general `docs/**` one and a reader can tell which by reading down.
DEFAULT_CLASS_MAP: tuple[tuple[str, str], ...] = (
    ("docs/adr/**", "rules"),
    ("CONTRIBUTING*", "rules"),
    ("CLAUDE.md", "rules"),
    ("**/conventions*", "rules"),
    ("docs/**", "lore"),
    ("README*", "lore"),
    ("tasks/**", "lore"),
)

# Docs-first: these chunk through the normal path. Everything else is `misc` and
# `experimental` (§15.9's named Phase-6 trigger).
_DOC_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".adoc"})

_MAX_FILE_BYTES = 512 * 1024


@dataclass(frozen=True)
class RepoFile:
    path: str
    content: str


@dataclass
class RepoIngestReport:
    knowledge_source_id: uuid.UUID
    version_id: uuid.UUID | None
    commit_sha: str
    entries: int = 0
    skipped: int = 0
    unchanged: bool = False
    by_class: dict[str, int] = field(default_factory=dict)
    quarantined: list[tuple[str, str]] = field(default_factory=list)
    experimental: list[str] = field(default_factory=list)


def classify(path: str, class_map: tuple[tuple[str, str], ...] = DEFAULT_CLASS_MAP) -> str:
    """First matching glob wins. `fnmatch` rather than a bespoke matcher: `**` behaves as
    a plain wildcard across separators here, which is what makes `docs/**` match
    `docs/adr/0001.md` as a reader expects."""
    for pattern, class_ in class_map:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(f"/{path}", f"/{pattern}"):
            return class_
    return "misc"


def is_document(path: str) -> bool:
    return any(path.lower().endswith(suffix) for suffix in _DOC_SUFFIXES)


def read_tarball(data: bytes) -> list[RepoFile]:
    """The upload path. A tarball decouples this task from G4.12's transport slippage and
    serves air-gapped tenants -- both reasons the task itself gives, and both still true.

    Refuses absolute and traversing paths outright. Nothing here writes to a filesystem, so
    a traversal cannot escape anywhere; the refusal is because a member named `../../etc/
    passwd` is a sign about the *archive*, and ingesting the rest of it as ordinary
    knowledge would be ignoring that sign."""
    files: list[RepoFile] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            # Checked on the *raw* name, before normalising. `lstrip("./")` strips a
            # character set rather than a prefix, so it would quietly turn
            # "../../etc/passwd" into "etc/passwd" and make the check below pass -- which
            # is exactly the bug a traversal guard must not have.
            raw_name = member.name
            if raw_name.startswith("/") or ".." in raw_name.split("/"):
                raise ValueError(f"refusing archive with unsafe member path {raw_name!r}")
            name = raw_name[2:] if raw_name.startswith("./") else raw_name
            if member.size > _MAX_FILE_BYTES:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            raw = handle.read()
            try:
                files.append(RepoFile(path=name, content=raw.decode("utf-8")))
            except UnicodeDecodeError:
                # Binary. A repository is full of them and none is knowledge.
                continue
    return sorted(files, key=lambda f: f.path)


def entry_key_for(path: str) -> str:
    """The repo path, flattened. Keeping the path recognisable matters more than making it
    pretty: a reviewer looking at a quarantined entry needs to find the file."""
    return path.replace("/", "__")[:255]


async def ingest_repo_snapshot(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    repo_ref: str,
    commit_sha: str,
    files: list[RepoFile],
    class_map: tuple[tuple[str, str], ...] = DEFAULT_CLASS_MAP,
    scope_key: str = "workspace_public",
) -> RepoIngestReport:
    """Ingests one snapshot as a new version of the repository's KnowledgeSource.

    Re-ingesting a SHA already published is a **no-op**: the version exists, the content is
    by definition identical, and re-publishing would create a version whose diff is empty
    -- noise in the DAG that a reader has to rule out later."""
    source = await _get_or_create_source(tenant_id, workspace_id, repo_ref, scope_key)
    report = RepoIngestReport(knowledge_source_id=source.id, version_id=None, commit_sha=commit_sha)

    existing = await _version_for_sha(tenant_id, source.id, commit_sha)
    if existing is not None:
        report.version_id = existing.id
        report.unchanged = True
        return report

    for repo_file in files:
        class_ = classify(repo_file.path, class_map)
        document = is_document(repo_file.path)
        if not document:
            # Source code: plain chunking into `misc`, flagged so §15.9's code-aware-
            # chunking evaluation can find exactly what was treated naively.
            class_ = "misc"
            report.experimental.append(repo_file.path)

        reasons = [
            r
            for r in (
                injection_reason(scan_text(repo_file.content)),
                secret_reason(scan_for_secrets(repo_file.content)),
            )
            if r
        ]
        entry_key = entry_key_for(repo_file.path)
        await upsert_draft_entry(
            tenant_id,
            source.id,
            entry_key,
            EntryFields(
                title=repo_file.path,
                body_md=repo_file.content,
                class_=class_,
                scope_key=scope_key,
            ),
        )
        report.entries += 1
        report.by_class[class_] = report.by_class.get(class_, 0) + 1
        if reasons:
            report.quarantined.append((entry_key, "; ".join(reasons)))

    version = await publish_version(
        tenant_id,
        source.id,
        change_note=f"repo snapshot {repo_ref}@{commit_sha}",
    )
    report.version_id = version.id

    await _chunk_published(tenant_id, source.id, version.id)
    await _apply_quarantine(tenant_id, source.id, dict(report.quarantined))
    return report


async def _get_or_create_source(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, repo_ref: str, scope_key: str
) -> KnowledgeSource:
    key = f"repo:{repo_ref}"
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.tenant_id == tenant_id, KnowledgeSource.key == key
            )
        )
        if existing is not None:
            session.expunge(existing)
            return existing

    source = await create_source(tenant_id, key=key, name=repo_ref, class_="misc")
    await attach_source_to_workspace(tenant_id, workspace_id, source.id, scope_key)
    return source


async def _version_for_sha(
    tenant_id: uuid.UUID, source_id: uuid.UUID, commit_sha: str
) -> KnowledgeSourceVersion | None:
    """The SHA is recorded in `change_note` rather than in a new column: the note is
    already the human-readable provenance field, a repository ingest is one of very few
    things that has a natural external identifier, and adding a column used by one caller
    would be schema for one feature."""
    marker = f"@{commit_sha}"
    for version in await list_versions(tenant_id, source_id):
        if version.change_note and version.change_note.endswith(marker):
            return version
    return None


async def _chunk_published(
    tenant_id: uuid.UUID, source_id: uuid.UUID, version_id: uuid.UUID
) -> None:
    """One chunk per file. Docs-first means the *semantic* chunker is what a later task
    turns on for markdown; today both paths chunk whole-file, and the distinction that
    matters is `RepoIngestReport.experimental` naming which entries were treated naively --
    which is what §15.9's code-aware-chunking evaluation needs to find them."""
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO knowledge_chunk "
                "(tenant_id, entry_id, version_id, ordinal, text, token_count, class, "
                " scope_key, embedding, content_hash) "
                "SELECT e.tenant_id, e.id, e.version_id, 0, e.body_md, "
                "       array_length(regexp_split_to_array(e.body_md, '\\s+'), 1), "
                "       e.class, e.scope_key, NULL, md5(e.body_md) "
                "FROM knowledge_entry e "
                "WHERE e.knowledge_source_id = :s AND e.version_id = :v "
                "  AND length(trim(e.body_md)) > 0"
            ),
            {"s": source_id, "v": version_id},
        )


async def _apply_quarantine(
    tenant_id: uuid.UUID, source_id: uuid.UUID, reasons: dict[str, str]
) -> None:
    """Same mechanism as G4.6 and G4.8: flag the published entry and its chunks together,
    after publish, because publish copies drafts into version rows and retrieval reads the
    published copy."""
    if not reasons:
        return
    async with tenant_scope(tenant_id) as session:
        for entry_key, reason in reasons.items():
            await session.execute(
                text(
                    "UPDATE knowledge_entry SET quarantined = true, quarantine_reason = :r "
                    "WHERE knowledge_source_id = :s AND entry_key = :k "
                    "  AND version_id IS NOT NULL"
                ),
                {"r": reason[:2000], "s": source_id, "k": entry_key},
            )
            await session.execute(
                text(
                    "UPDATE knowledge_chunk SET quarantined = true WHERE entry_id IN "
                    "(SELECT id FROM knowledge_entry WHERE knowledge_source_id = :s "
                    " AND entry_key = :k AND version_id IS NOT NULL)"
                ),
                {"s": source_id, "k": entry_key},
            )
