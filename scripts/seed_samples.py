"""Rebuild a purged deployment's tenants from the sample bundles.

This is the scripted form of the steps each sample's README walks a person through:
create the model connections, import the ``.pyr``, and bind every persona to the
connection its role calls for. The README stays the human path and this stays the daily
one -- a loop that reinstalls both deployments every day cannot be a sequence of clicks,
and a runbook nobody can execute is a runbook that rots.

**Connections come from files, never from this repository.** The keys live outside the
tree and are read at run time; nothing here writes one to disk, prints one, or puts one
in a bundle. A `.pyr` has never carried a credential -- that is why every sample README
has a step telling you to add your own.

**Roles decide models, not persona names.** The mapping is declared once, below, and
applied by matching a persona's own ``persona_type`` and seniority rather than by
listing names per sample: a sample that gains a seventh persona should not need this
file edited to get a model.

Usage::

    python scripts/seed_samples.py --secrets-dir /path/to/secrets --samples hagnaryd-mystery,...

Run it against the deployment you just installed; it talks to the database the same way
the API does, so it needs the same environment (``PYRRHULA_APP_DATABASE_URL`` et al).
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import uuid
from dataclasses import dataclass

# Where each sample's bundle lives, relative to the samples checkout, and which kind of
# table it is -- which is what decides the models.
SAMPLES: dict[str, tuple[str, str]] = {
    "hagnaryd-mystery": ("hagnaryd-mystery/hagnaryd-mystery.pyr", "rpg"),
    "karsh-vale": ("karsh-vale/karsh-vale.pyr", "rpg"),
    "coffee-campaign": ("coffee-campaign/coffee-campaign.pyr", "enterprise"),
    "mice-invaders": ("mice-invaders/mice-invaders.pyr", "swdev"),
    "pyrrhula": ("pyrrhula/pyrrhula.pyr", "swdev"),
}


@dataclass(frozen=True)
class Connection:
    """One model connection to create in every seeded tenant."""

    name: str
    provider: str
    model: str
    secret_file: str | None = None  # None -> a local provider that needs no key
    api_base: str | None = None


# The deployment's connections. Ollama needs no key and is reached over the host network;
# the two hosted providers read their key from the secrets directory at run time.
CONNECTIONS: tuple[Connection, ...] = (
    Connection("Anthropic Sonnet", "anthropic", "claude-sonnet-5", "anthropic"),
    Connection("Anthropic Opus", "anthropic", "claude-opus-5-5", "anthropic"),
    Connection("DeepSeek", "deepseek", "deepseek-chat", "deepseek"),
    Connection(
        "Ollama Qwen",
        "ollama_chat",
        "qwen3.8:27b",
        None,
        api_base="http://host.containers.internal:11434",
    ),
)

# Which connection a persona gets, by table kind and seniority. Seniority is read from
# the persona's own name/key because that is where the samples express it; anything
# unmatched falls to the table's default, so a new persona is never left without a model.
SWDEV_SENIOR = ("lead", "architect", "senior", "staff", "principal")
SWDEV_JUNIOR = ("junior", "middle", "mid", "qa", "tester", "dev")

ASSISTANT_CONNECTION = "DeepSeek"  # informational personas, every table kind


def connection_for(kind: str, persona_type: str, name: str, key: str) -> str:
    """The connection name this persona should be bound to."""
    haystack = f"{name} {key}".lower()
    if persona_type == "informational":
        return ASSISTANT_CONNECTION
    if kind == "swdev":
        if any(word in haystack for word in SWDEV_SENIOR):
            return "Anthropic Opus"
        if any(word in haystack for word in SWDEV_JUNIOR):
            return "Anthropic Sonnet"
        # A supervisor with an unfamiliar title still leads the table.
        return "Anthropic Opus" if persona_type == "supervisor" else "Anthropic Sonnet"
    if kind == "rpg":
        # A mix on purpose: the referee carries the scene and the rules, the players
        # answer to it, and running every seat on one model makes a table that agrees
        # with itself.
        return "DeepSeek" if persona_type == "supervisor" else "Ollama Qwen"
    # enterprise
    return "DeepSeek"


def read_secret(secrets_dir: pathlib.Path, name: str) -> str:
    path = secrets_dir / name
    if not path.is_file():
        raise SystemExit(f"no secret file {path} -- cannot create the {name} connection")
    value = path.read_text().strip()
    if not value:
        raise SystemExit(f"{path} is empty")
    return value


async def seed(
    slug: str,
    sample: str,
    samples_dir: pathlib.Path,
    secrets_dir: pathlib.Path,
) -> None:
    from sqlalchemy import select

    from api.encryptor_factory import get_encryptor
    from api.moderation_provider_factory import moderation_provider_for
    from api.permission_service_factory import get_permission_service
    from core.agents.authoring import create_agent
    from core.agents.models import Persona
    from core.portability.import_ import import_bundle
    from core.tenancy.scope import tenant_scope
    from core.tenancy.seed import seed_dev_tenant

    relative, kind = SAMPLES[sample]
    bundle = samples_dir / relative
    if not bundle.is_file():
        raise SystemExit(f"no bundle at {bundle}")

    # Idempotent: an existing tenant of this slug is reused, so re-running the seed
    # after a partial failure does not fork a second copy of the workspace.
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=slug, tenant_name=slug.replace("-", " ").title()
    )
    print(f"[{slug}] tenant {tenant_id} workspace {workspace_id}", flush=True)

    # The seat signup gives a person, which `seed_dev_tenant` does not: owning a tenant
    # is not a role inside its workspaces. Without it the import authors no secrets --
    # "may not author secrets in workspace ..." -- and the sample lands with its private
    # briefs missing. Steward is what the sample READMEs describe you as after signing
    # up: the seat that can both build the room and inspect it.
    from core.tenancy.models import WorkspaceMembership

    async with tenant_scope(tenant_id) as session:
        held = await session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.principal_id == owner_id,
            )
        )
        if held is None:
            session.add(
                WorkspaceMembership(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    principal_id=owner_id,
                    role="steward",
                )
            )
            await session.flush()

    # Step "add your model connection", once per connection the deployment offers.
    encryptor = get_encryptor()
    made: dict[str, uuid.UUID] = {}
    for conn in CONNECTIONS:
        key = read_secret(secrets_dir, conn.secret_file) if conn.secret_file else None
        agent = await create_agent(
            tenant_id,
            conn.name,
            conn.provider,
            conn.model,
            api_key=key,
            api_base=conn.api_base,
            encryptor=encryptor,
        )
        made[conn.name] = agent.id
    print(f"[{slug}] connections: {', '.join(made)}", flush=True)

    # Step "import the case".
    report = await import_bundle(
        bundle.read_bytes(),
        tenant_id,
        workspace_id,
        bundle_ref=f"{sample}.pyr",
        encryptor=encryptor,
        # Secrets need all four of these, and an import missing any of them skips
        # every one -- silently enough that a six-agent mystery imports looking
        # complete while the eleven private briefs that are the whole point of it are
        # absent. A secret is authored by someone, sealed, permission-checked and
        # moderated; none of those has a sensible default, so the caller supplies them.
        importing_principal_id=owner_id,
        permission_service=get_permission_service(),
        moderation_provider=await moderation_provider_for(tenant_id),
    )
    skipped_secrets = [s for s in report.skipped if s.startswith("secret")]
    print(f"[{slug}] imported {sample}", flush=True)
    if skipped_secrets:
        raise SystemExit(f"[{slug}] secrets did not import: {skipped_secrets}")

    # Step "point the cast at your connection" -- the one the README makes you do by
    # hand for every persona, which is where a six-agent sample loses people.
    bound: dict[str, int] = {}
    async with tenant_scope(tenant_id) as session:
        personas = (
            await session.execute(select(Persona).where(Persona.tenant_id == tenant_id))
        ).scalars()
        for persona in personas:
            want = connection_for(kind, persona.persona_type, persona.name, persona.key)
            persona.agent_id = made[want]
            bound[want] = bound.get(want, 0) + 1
        await session.flush()
    print(f"[{slug}] bound personas: {bound}", flush=True)

    await register_repos(slug, tenant_id, owner_id, bundle.parent, secrets_dir)


async def register_repos(
    slug: str,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    sample_dir: pathlib.Path,
    secrets_dir: pathlib.Path,
) -> None:
    """Register the repositories a sample declares in ``repos.json``, if it has one.

    A ``.pyr`` cannot carry this: a repository registration is half configuration and
    half credential, and a bundle holds neither by design. So the sample declares the
    configuration beside its bundle and the credential is read from the secrets
    directory at run time -- the same split every sample README already describes for
    model connections.
    """
    import json

    from api.encryptor_factory import get_encryptor
    from core.agents.authoring import store_provider_credential
    from core.repos.runtimes import register_runtime
    from core.repos.service import create_repo

    manifest = sample_dir / "repos.json"
    if not manifest.is_file():
        return
    declared = json.loads(manifest.read_text())
    encryptor = get_encryptor()

    for spec in declared:
        # Runtimes first: a repo naming one that is not registered yet is refused, and
        # the refusal would read as a broken sample rather than an ordering problem.
        for key, entry in (spec.get("runtimes") or {}).items():
            await register_runtime(
                tenant_id, key, str(entry["image"]), [str(c) for c in entry.get("setup") or []]
            )
            print(f"[{slug}] runtime {key} -> {entry['image']}", flush=True)

        credential_ref = None
        secret_name = spec.get("credential_secret")
        if secret_name:
            token = read_secret(secrets_dir, secret_name)
            credential_ref = await store_provider_credential(
                tenant_id, token, encryptor=encryptor
            )

        repo = await create_repo(
            tenant_id,
            str(spec["key"]),
            str(spec.get("name") or spec["key"]),
            description=str(spec.get("description") or ""),
            source_url=spec.get("source_url"),
            default_branch=str(spec.get("default_branch") or "main"),
            runtime=str(spec.get("runtime") or "debian"),
            runtime_image=spec.get("runtime_image"),
            credential_ref=credential_ref,
            setup_cmds=[str(c) for c in spec.get("setup_cmds") or []],
            test_cmd=spec.get("test_cmd"),
            build_cmd=spec.get("build_cmd"),
            artifact_name=spec.get("artifact_name"),
            created_by=owner_id,
        )
        print(f"[{slug}] repo {repo.key} ({spec.get('source_url') or 'store-only'})", flush=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secrets-dir", required=True, type=pathlib.Path)
    parser.add_argument(
        "--samples-dir",
        type=pathlib.Path,
        default=pathlib.Path.home() / "code" / "pyrrhula-samples",
    )
    parser.add_argument(
        "--samples",
        required=True,
        help="comma-separated sample names, or 'slug=sample' to name the tenant",
    )
    args = parser.parse_args()

    for item in args.samples.split(","):
        item = item.strip()
        if not item:
            continue
        slug, _, sample = item.partition("=")
        sample = sample or slug
        if sample not in SAMPLES:
            raise SystemExit(f"unknown sample {sample!r}; known: {', '.join(sorted(SAMPLES))}")
        await seed(slug, sample, args.samples_dir, args.secrets_dir)
    print("done", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))  # type: ignore[func-returns-value]
