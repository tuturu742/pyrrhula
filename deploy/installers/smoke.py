"""Post-install smoke check: can this deployment actually do the thing it exists for?

Run by every installer, always, as the last step. Piped in over stdin so one file
serves every deployment shape::

    podman exec -i pyrrhula_api_1 python - < deploy/installers/smoke.py
    kubectl -n pyrrhula exec -i deploy/pyrrhula-api -- python - < deploy/installers/smoke.py

**Why this is not the readiness probe the installer already has.** That probe POSTs a
bogus login and accepts 401/404/422, which proves the request reached the app and did a
database lookup. Everything past that point was unverified, and "everything past that
point" is where the failures actually live: a worker that OOMs on its first job, a job
queue whose leases were never reclaimed, a blob volume mounted read-only, a migration
that ran but left the app role without its grants. Every one of those passes the login
probe and cannot do a minute of useful work.

**What it deliberately does not need.**

* *No workflow pack.* Packs are optional -- `builtin-workflows/` is baked into the image
  precisely so a deployment with no plugin repo still works -- so a check that needed one
  would fail honest installs and, worse, would only ever verify the pack. An RPG check
  tells an SWE-only operator nothing.
* *No model, retrieval or otherwise.* `knowledge_ingest` parses and chunks; `embed_chunks`
  is a separate job. Splitting there is what lets this run in seconds on a box that has
  not downloaded a model yet -- which, since the installer no longer downloads one, is
  every fresh box. Model presence is reported, never asserted.
* *No network.* Nothing here reaches outside the deployment.

**Fast-failing.** Every step says what it is about to prove, and on failure says what
that specific failure means rather than "check the logs". The whole thing is seconds
unless the worker is dead, which is the one thing worth waiting ~60s to be sure about.

Exit 0 = this deployment works. Non-zero = it does not, and the reason is on stdout.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

from sqlalchemy import text

# The tenant is torn down at the end; the slug carries the marker so a crashed run leaves
# something an operator can recognise and `python -m core.tenancy.purge` can sweep.
SLUG_PREFIX = "pyrrhula-smoke"

# The worker's first claim after a cold start includes importing its own dependency tree,
# which on a slow disk is tens of seconds. Past that, a job that has not moved is a job
# nobody is going to run.
WORKER_DEADLINE_SECONDS = int(os.environ.get("PYRRHULA_SMOKE_WORKER_TIMEOUT", "90"))

_DOC = (
    "Pyrrhula post-install check.\n\n"
    "This document exists only to be chunked. Its content is irrelevant; what matters "
    "is that a blob write, a job enqueue, a worker claim, a parse and a database write "
    "all happened, in that order, on this deployment.\n\n"
    "A second paragraph, so the chunker has more than one thing to produce.\n"
)


def step(label: str) -> None:
    print(f"  .. {label}", flush=True)


def ok(label: str) -> None:
    print(f"  OK {label}", flush=True)


def die(label: str, meaning: str) -> None:
    print(f"\nFAILED: {label}\n\n{meaning}\n", flush=True)
    raise SystemExit(1)


async def _check() -> None:
    from api.blob_store_factory import get_blob_store
    from api.job_queue_factory import get_job_queue
    from core.knowledge.authoring import create_source
    from core.tenancy.provisioning import create_tenant
    from core.tenancy.scope import admin_purge_session, tenant_scope

    slug = f"{SLUG_PREFIX}-{uuid.uuid4().hex[:8]}"
    tenant_id: uuid.UUID | None = None

    try:
        # 1. Provisioning. Exercises the write path, RLS setup and the default-scope
        #    seeding a tenant cannot function without.
        step("creating a throwaway tenant and workspace")
        try:
            tenant_id, _workspace_id = await create_tenant("Smoke Check", slug)
        except Exception as exc:  # noqa: BLE001 -- the message is the product here
            die(
                "could not create a tenant",
                "The database accepted a connection but not a write. Usually the app role "
                "is missing grants (migrations ran as a different role), or the schema is "
                f"older than this image and a migration failed silently.\n\n  {exc}",
            )
        ok(f"tenant {slug} + default workspace")

        # 2. Blobs. A read-only or unmounted blob volume is invisible until the first
        #    upload, which is otherwise a user's first real document.
        step("writing and reading a blob")
        blob_key = f"smoke/{tenant_id}/{uuid.uuid4()}.txt"
        try:
            await get_blob_store().put(blob_key, _DOC.encode(), content_type="text/plain")
            back = await get_blob_store().get(blob_key)
        except Exception as exc:  # noqa: BLE001
            die(
                "the blob store is not writable",
                "Knowledge documents, exports and reports all land here. On compose this "
                "is the data volume; on Kubernetes the blobs PVC -- check it is mounted "
                f"read-write and its claim is Bound.\n\n  {exc}",
            )
        if back != _DOC.encode():
            die(
                "a blob did not read back as written",
                "The store accepted the write and returned something else. A shared "
                "volume mounted by more than one deployment with different prefixes will "
                "do this.",
            )
        ok("blob round-trip")

        # 3. The queue and the worker, together. This is the step that matters: it is the
        #    only one that proves a *second process* is alive and consuming.
        step("enqueuing a document for the worker to ingest")
        source = await create_source(tenant_id, "smoke", "Smoke Check", "misc")
        job_id = await get_job_queue().enqueue(
            tenant_id,
            "knowledge_ingest",
            {
                "tenant_id": str(tenant_id),
                "knowledge_source_id": str(source.id),
                "blob_key": blob_key,
                "filename": "smoke.txt",
                "class": "misc",
                "scope_key": "workspace_public",
            },
        )
        ok(f"job {job_id} queued")

        step(f"waiting for the worker to run it (up to {WORKER_DEADLINE_SECONDS}s)")
        deadline = time.monotonic() + WORKER_DEADLINE_SECONDS
        status = claimed = None
        error = None
        while time.monotonic() < deadline:
            async with admin_purge_session() as session:
                row = (
                    await session.execute(
                        text("SELECT status, claimed_at, error FROM job WHERE id=:j"),
                        {"j": job_id},
                    )
                ).first()
            if row is None:
                die(
                    "the queued job vanished",
                    "Something deleted a job row out from under the queue. Nothing in "
                    "Pyrrhula does that; suspect a second deployment sharing this "
                    "database.",
                )
            status, claimed, error = row
            if status in ("done", "failed"):
                break
            await asyncio.sleep(1)

        if status == "failed":
            die(
                "the worker ran the job and it failed",
                "The worker is alive, so this is the job itself. The error it recorded:\n"
                f"\n  {error}",
            )
        if status != "done":
            die(
                "no worker ran the job",
                "The job was queued and "
                + (
                    "claimed but never finished -- the worker started it and died, which "
                    "on a small box is usually the OOM killer. Check the worker's memory "
                    "limit and its last log lines."
                    if claimed
                    else "never claimed. The worker process is not running, cannot reach "
                    "this database, or is wedged. Check that the worker container/pod is "
                    "up and look at its logs -- a worker that cannot start says so there."
                ),
            )
        ok("worker claimed, ran and completed the job")

        # 4. The result actually landed. A handler that returns success without writing
        #    is a real failure mode and the queue cannot tell the difference.
        step("checking the chunks reached the database")
        async with tenant_scope(tenant_id) as session:
            chunks = await session.scalar(
                text(
                    "SELECT count(*) FROM knowledge_chunk c "
                    "JOIN knowledge_entry e ON e.id = c.entry_id "
                    "WHERE e.knowledge_source_id = :s"
                ),
                {"s": source.id},
            )
        if not chunks:
            die(
                "the job reported success but wrote no chunks",
                "Parsing produced nothing from a plain-text document. This is an "
                "application fault rather than a deployment one -- worth reporting.",
            )
        ok(f"{chunks} chunk(s) stored")

        # 5. Informational only. A deployment with no retrieval model is correctly
        #    installed; it just cannot answer a semantic query until one is chosen. That
        #    choice belongs to whoever runs it, in the admin console, not to this script.
        step("checking which retrieval models are present")
        try:
            from core.deployment_settings import get_retrieval_models
            from core.retrieval_cache import presence

            models = await get_retrieval_models()
            embedding = presence(models["embedding_model"])
            if embedding.present:
                ok(f"embedding model {embedding.model} present ({embedding.size_mb}MB)")
            else:
                print(
                    f"  -- no embedding model on this box yet ({embedding.model}).\n"
                    "     Knowledge ingests and chunks, but semantic search stays empty "
                    "until one is\n"
                    "     downloaded. Admin console -> Models: pick the models you want, "
                    "then Download\n"
                    "     (or upload a cache tarball on a box with no route to Hugging "
                    "Face).",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001 -- never fail the install over a report
            print(f"  -- could not read model state ({exc})", flush=True)

    finally:
        if tenant_id is not None:
            async with admin_purge_session() as session:
                await session.execute(text("DELETE FROM tenant WHERE id=:t"), {"t": str(tenant_id)})


def main() -> None:
    print("== verifying this deployment can do real work", flush=True)
    try:
        asyncio.run(_check())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED: the check itself could not run\n\n  {exc!r}\n", flush=True)
        sys.exit(1)
    print("== the deployment works.", flush=True)


if __name__ == "__main__":
    main()
