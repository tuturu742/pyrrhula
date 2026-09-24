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
    "newsroom": ("newsroom/newsroom.pyr", "enterprise"),
}


@dataclass(frozen=True)
class Connection:
    """One model connection to create in every seeded tenant."""

    name: str
    provider: str
    model: str
    secret_file: str | None = None  # None -> a local provider that needs no key
    api_base: str | None = None
    # Provider params, stored on the connection. `max_tokens` is the one that matters
    # here: codegen falls back to 12000 when a connection says nothing, which is a safe
    # floor and well under what the hosted models will actually emit. A coding task whose
    # answer is several whole files hits that floor and simply stops mid-file -- observed
    # against loxia, where regenerating five terminal-grid snapshot fixtures needs more
    # output than the floor allows, so the agent could only ever deliver one per round.
    params: dict[str, object] | None = None


# The deployment's connections. Ollama needs no key and is reached over the host network;
# the two hosted providers read their key from the secrets directory at run time.
CONNECTIONS: tuple[Connection, ...] = (
    Connection(
        "Anthropic Sonnet",
        "anthropic",
        "claude-sonnet-5",
        "anthropic",
        params={"max_tokens": 32000},
    ),
    Connection(
        "Anthropic Opus", "anthropic", "claude-opus-5-5", "anthropic", params={"max_tokens": 32000}
    ),
    Connection("DeepSeek", "deepseek", "deepseek-chat", "deepseek"),
    # The reasoning model, for the one seat at a table that has to hold a whole case in
    # its head rather than answer for one person in it.
    Connection("DeepSeek Reasoner", "deepseek", "deepseek-reasoner", "deepseek"),
    # The current generation, for the table that has to hold eight beats together.
    Connection("DeepSeek V4 Pro", "deepseek", "deepseek-v4-pro", "deepseek"),
    # api_base is resolved at run time -- see ollama_base_url.
    Connection("Ollama Qwen", "ollama_chat", "qwen3.8:27b", None),
    # The local model the newsroom runs on, and the reason that sample can claim to
    # prove internet access at all: a hosted model could be answering from training
    # data, and nothing in the transcript would tell you apart. This one cannot know
    # what happened this week unless it looked.
    #
    # A sparse MoE rather than a dense model of the same footprint, which is what makes
    # it usable here. On this class of machine memory capacity is abundant and bandwidth
    # is the constraint, so 30B total with 3B active runs at ~46 tok/s where the dense
    # 27B managed turns past ten minutes -- the measurement that got Ollama dropped from
    # every other sample.
    Connection("Ollama Qwen3 MoE", "ollama_chat", "qwen3:30b-a3b", None),
)

# Where a container reaches a model server running on the host. There is no single right
# answer: `host.containers.internal` is podman's, `host.docker.internal` is Docker
# Desktop's, and on this host both resolve to an address that does not answer while the
# bridge gateway does. Guessing wrong is expensive to diagnose -- the name resolves, so
# it reads as a hung model rather than an unreachable one -- so the candidates are tried
# and the first that accepts a connection wins.
_OLLAMA_PORT = 11434


def ollama_base_url() -> str | None:
    import os
    import socket

    override = os.environ.get("PYRRHULA_OLLAMA_BASE")
    if override:
        return override

    candidates = ["host.containers.internal", "host.docker.internal", "127.0.0.1"]
    # The default gateway is the host on a bridge network, and is what actually answers
    # here. Read it rather than assuming a subnet.
    try:
        with open("/proc/net/route") as handle:
            for line in handle.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    packed = int(fields[2], 16)
                    candidates.insert(
                        0, ".".join(str((packed >> (8 * i)) & 0xFF) for i in range(4))
                    )
                    break
    except OSError:
        pass

    for host in candidates:
        probe = socket.socket()
        probe.settimeout(2)
        try:
            probe.connect((host, _OLLAMA_PORT))
            return f"http://{host}:{_OLLAMA_PORT}"
        except OSError:
            continue
        finally:
            probe.close()
    return None


# Which connection a persona gets, by table kind and seniority. Seniority is read from
# the persona's own name/key because that is where the samples express it; anything
# unmatched falls to the table's default, so a new persona is never left without a model.
SWDEV_SENIOR = ("lead", "architect", "senior", "staff", "principal")
SWDEV_JUNIOR = ("junior", "middle", "mid", "qa", "tester", "dev")

ASSISTANT_CONNECTION = "DeepSeek"  # informational personas, every table kind


# Per-sample overrides, keyed on the sample rather than the tenant slug -- the slug is
# whatever the operator called the tenant, the sample is what the cast actually is.
#
# hagnaryd is a mystery rather than a dice game: the suspects have to keep their own
# accounts straight under cross-examination, and the investigator has to hold every
# account at once and notice where two of them cannot both be true. That is a reasoning
# job, and the local 27B was neither fast enough nor sharp enough for it -- a single turn
# ran past ten minutes and history summarisation, which uses the persona's own model,
# timed out at 600s and dropped the session's history on the floor.
SAMPLE_CONNECTIONS: dict[str, dict[str, str]] = {
    "hagnaryd-mystery": {"supervisor": "DeepSeek Reasoner", "participant": "DeepSeek"},
    # The campaign is eight beats -- the longest session in the suite -- and on the local
    # 27B a single player turn ran past ten minutes while history summarisation, which
    # runs on the persona's own model, timed out at 600s against itself. The referee
    # stays on the table default; only the players move.
    # The referee too, and for a sharper reason than speed. On the table default it ran
    # the "first fight" beat as a conversation in a reeve's front room and the second
    # encounter as another one -- two combat beats, no monster, no rolls -- while the
    # players on the stronger model were producing exact, in-character work. The beat that
    # stages danger is the hardest seat at the table, not the easiest.
    #
    # The model was not the whole story, and this comment said it was for a while: those
    # phases also gave each player exactly one turn, because a declared actor entry walks
    # the roster once and `max_turns` caps that walk rather than repeating it. A fight
    # with one action per player is not a fight on any model. The flow writes its rounds
    # out now; the seating below is the other half.
    "karsh-vale": {"participant": "DeepSeek V4 Pro", "supervisor": "DeepSeek V4 Pro"},
    # Every seat local, on purpose. The point of this sample is that the paper could not
    # have been written without reaching the internet, and that claim only means
    # something if the model has no other way to know: a hosted model asked about this
    # week may simply answer, and the transcript looks identical either way.
    "newsroom": {
        "supervisor": "Ollama Qwen3 MoE",
        "participant": "Ollama Qwen3 MoE",
        "informational": "Ollama Qwen3 MoE",
    },
}


def connection_for(
    kind: str, persona_type: str, name: str, key: str, sample: str | None = None
) -> str:
    """The connection name this persona should be bound to."""
    haystack = f"{name} {key}".lower()
    if persona_type == "informational":
        return ASSISTANT_CONNECTION
    override = SAMPLE_CONNECTIONS.get(sample or "", {}).get(persona_type)
    if override:
        return override
    if kind == "swdev":
        if any(word in haystack for word in SWDEV_SENIOR):
            return "Anthropic Opus"
        if any(word in haystack for word in SWDEV_JUNIOR):
            return "Anthropic Sonnet"
        # A supervisor with an unfamiliar title still leads the table.
        return "Anthropic Opus" if persona_type == "supervisor" else "Anthropic Sonnet"
    if kind == "rpg":
        # The local 27B is no longer the default for any seat: a single player turn ran
        # past ten minutes, and history summarisation -- which runs on the persona's own
        # model -- timed out at 600s against itself and dropped the session's history.
        # The connection stays registered for a deployment that wants it; nothing is
        # seated on it.
        return "DeepSeek"
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
    local_base = ollama_base_url()
    for conn in CONNECTIONS:
        key = read_secret(secrets_dir, conn.secret_file) if conn.secret_file else None
        api_base = conn.api_base
        if conn.provider.startswith("ollama"):
            if local_base is None:
                print(
                    f"[{slug}] no model server answering on :{_OLLAMA_PORT} from here; "
                    f"skipping {conn.name}",
                    flush=True,
                )
                continue
            api_base = local_base
        agent = await create_agent(
            tenant_id,
            conn.name,
            conn.provider,
            conn.model,
            api_key=key,
            api_base=api_base,
            params=conn.params,
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

    # What ruleset the bundle pointed each tool at, before the pack load below can
    # overwrite it. See `restore_tool_bindings`.
    imported_tool_bindings = await tool_bindings(tenant_id)

    # Step "point the cast at your connection" -- the one the README makes you do by
    # hand for every persona, which is where a six-agent sample loses people.
    bound: dict[str, int] = {}
    async with tenant_scope(tenant_id) as session:
        personas = (
            await session.execute(select(Persona).where(Persona.tenant_id == tenant_id))
        ).scalars()
        for persona in personas:
            want = connection_for(kind, persona.persona_type, persona.name, persona.key, sample)
            persona.agent_id = made[want]
            bound[want] = bound.get(want, 0) + 1
        await session.flush()
    print(f"[{slug}] bound personas: {bound}", flush=True)

    # A persona with no seat in the workspace cannot act in it. entity:create is granted
    # by a workspace role, and a bundle carries a cast rather than a deployment's
    # membership table -- so imported personas arrive able to speak and unable to do
    # anything. Observed: two players rolled a full set of Basic Fantasy ability scores
    # and then reported "the sheet won't bind -- the system refuses the write", and the
    # referee carried on from the transcript because there was nothing else to do.
    seated: dict[str, int] = {}
    async with tenant_scope(tenant_id) as session:
        personas = (
            await session.execute(select(Persona).where(Persona.tenant_id == tenant_id))
        ).scalars()
        for persona in personas:
            role = "facilitator" if persona.persona_type == "supervisor" else "participant"
            held = await session.scalar(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.principal_id == persona.principal_id,
                )
            )
            if held is None:
                session.add(
                    WorkspaceMembership(
                        tenant_id=tenant_id,
                        workspace_id=workspace_id,
                        principal_id=persona.principal_id,
                        role=role,
                    )
                )
                seated[role] = seated.get(role, 0) + 1
        await session.flush()
    print(f"[{slug}] seated personas: {seated or 'already seated'}", flush=True)

    # The workspace assistant is created on demand -- by repo analysis, by the assist
    # widget -- with an empty model profile for an operator to fill in. Nothing here
    # fills it, so the first thing to need it got provider "" and model "", and the call
    # went out as model="/". Repo analysis then fell back on every step and produced a
    # graph with no summaries in it, reporting success.
    from core.agents.assistant import ensure_workspace_assistant

    assistant = await ensure_workspace_assistant(tenant_id, workspace_id)
    async with tenant_scope(tenant_id) as session:
        live = await session.get(Persona, assistant.id)
        if live is not None and live.agent_id != made[ASSISTANT_CONNECTION]:
            live.agent_id = made[ASSISTANT_CONNECTION]
            await session.flush()
    print(f"[{slug}] assistant -> {ASSISTANT_CONNECTION}", flush=True)

    # `slug=sample` means "a tenant called <slug>, from <sample>'s cast" -- the loxia
    # sample is exactly that, borrowing the pyrrhula bundle's bench and bringing only its
    # own repository. Its `repos.json` therefore lives in `loxia/`, not beside the bundle,
    # and looking only beside the bundle silently registered no repository at all: the
    # tenant came up complete except for the one thing the sample is about, which then had
    # to be added by hand and did not survive the next purge.
    config_dir = samples_dir / slug if (samples_dir / slug).is_dir() else bundle.parent
    if config_dir != bundle.parent:
        print(f"[{slug}] extra configuration from {config_dir}", flush=True)
    await register_repos(slug, tenant_id, workspace_id, owner_id, config_dir, secrets_dir)
    await register_mcp_servers(slug, tenant_id, workspace_id, config_dir)
    await load_packs(slug, tenant_id, workspace_id, kind)
    await restore_tool_bindings(slug, tenant_id, imported_tool_bindings)


# Which pack each kind of table needs loaded on top of its bundle. A bundle carries the
# flow it was authored with; the pack carries the rest, including flows written after the
# bundle was exported.
PACKS = {"rpg": "rpg", "swdev": "swdev", "enterprise": None}


async def register_mcp_servers(
    slug: str, tenant_id: uuid.UUID, workspace_id: uuid.UUID, sample_dir: pathlib.Path
) -> None:
    """Attach the MCP servers a sample declares in ``mcp.json``, if it has one.

    A bundle is content and never code, so a sample whose case depends on an external
    tool ships the tool beside it and the registration here. The workspace allowlist is
    the egress control, which is why this is a deployment step rather than something the
    bundle could carry: what a tenant may call out to is not the bundle author's
    decision.

    The server itself still has to be running -- see the sample's README. Registering a
    url nothing answers on costs a failed tool call at the table, not a failed import.
    """
    import json

    from core.mcp.registry import register_server

    manifest = sample_dir / "mcp.json"
    if not manifest.is_file():
        return
    for spec in json.loads(manifest.read_text()):
        await register_server(
            tenant_id,
            workspace_id,
            str(spec["key"]),
            str(spec["url"]),
            enabled_tools=[str(t) for t in spec.get("enabled_tools") or []],
            effectful_tools=[str(t) for t in spec.get("effectful_tools") or []],
            require_confirmation=bool(spec.get("require_confirmation", False)),
            max_calls_per_session=spec.get("max_calls_per_session"),
            # Per-server knobs the sample knows and the platform cannot: which search
            # engines this instance can actually reach, for one. Passing them through is
            # what lets a sample ship a working search without a core change.
            options=dict(spec.get("options") or {}),
        )
        budget = spec.get("max_calls_per_session")
        print(
            f"[{slug}] mcp {spec['key']} -> {spec['url']}"
            + (f" (max {budget}/session)" if budget else ""),
            flush=True,
        )


async def tool_bindings(tenant_id: uuid.UUID) -> dict[str, str]:
    """Which rule system each tool definition currently validates against."""
    from sqlalchemy import select as _select

    from core.resolution.registry import ToolDefinitionRow
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                _select(ToolDefinitionRow).where(ToolDefinitionRow.tenant_id == tenant_id)
            )
        ).scalars()
        return {row.key: row.validation_ref for row in rows if row.validation_ref}


async def restore_tool_bindings(slug: str, tenant_id: uuid.UUID, imported: dict[str, str]) -> None:
    """Put back the ruleset the bundle bound each tool to.

    A pack ships a generic tool and a generic ruleset; a sample ships a specific one and
    a tool bound to it. Both register the same key, and the pack load runs last, so
    ``register_tool_definition``'s upsert quietly replaced the sample's binding with the
    pack's.

    The damage is invisible until somebody reads a roll. In karsh-vale the players rolled
    3d6 for ability scores against the generic d20 ruleset, which resolves an ability
    modifier for the check type, so a roll of [5, 6, 6] was recorded as **19** -- a score
    3d6 cannot produce. A player noticed, refused to write it on a sheet, and asked the
    referee which check type to use. She was right, and there was no answer that would
    have helped: the tool was pointed at the wrong ruleset.
    """
    from sqlalchemy import select as _select

    from core.resolution.registry import ToolDefinitionRow
    from core.tenancy.scope import tenant_scope

    restored: list[str] = []
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                _select(ToolDefinitionRow).where(ToolDefinitionRow.tenant_id == tenant_id)
            )
        ).scalars()
        for row in rows:
            want = imported.get(row.key)
            if want and row.validation_ref != want:
                restored.append(f"{row.key}: {row.validation_ref} -> {want}")
                row.validation_ref = want
        await session.flush()
    if restored:
        print(f"[{slug}] tool bindings restored after pack load: {restored}", flush=True)


async def load_packs(slug: str, tenant_id: uuid.UUID, workspace_id: uuid.UUID, kind: str) -> None:
    """Load this table's pack, then leave one active version of each flow.

    ``load_pack`` is versioned, not idempotent: loading the same pack twice leaves v1 and
    v2 of every flow active, both in the picker, both called the same thing and not
    distinguishable by looking at them. A daily rebuild that reloads packs turns that
    into a list nobody can choose from -- which is how a workspace ends up with nine
    flows and two that work.

    The newest version of each key wins, which is what a reload means. Older ones are
    archived rather than deleted: a session already running on v1 keeps its definition,
    because a flow row is what an in-flight session resolves its phases against.
    """
    import pathlib as _pathlib

    from sqlalchemy import select

    from core.packs.loader import load_pack
    from core.process.authoring import archive_definition
    from core.process.models import ProcessDefinitionRow
    from core.tenancy.scope import tenant_scope

    pack = PACKS.get(kind)
    if not pack:
        return
    pack_dir = _pathlib.Path("/app/packs") / pack
    if not pack_dir.is_dir():
        print(f"[{slug}] no pack at {pack_dir}; skipping", flush=True)
        return
    loaded = await load_pack(pack_dir, tenant_id, workspace_id)
    print(f"[{slug}] pack {pack}: {sorted(loaded.process_definition_ids)}", flush=True)

    async with tenant_scope(tenant_id) as session:
        rows = list(
            (
                await session.execute(
                    select(ProcessDefinitionRow).where(
                        ProcessDefinitionRow.tenant_id == tenant_id,
                        ProcessDefinitionRow.archived_at.is_(None),
                    )
                )
            ).scalars()
        )
    newest: dict[tuple[str, object], int] = {}
    for row in rows:
        ident = (row.key, row.workspace_id)
        newest[ident] = max(newest.get(ident, 0), row.version)
    superseded = [r for r in rows if r.version < newest[(r.key, r.workspace_id)]]
    for row in superseded:
        await archive_definition(tenant_id, row.id)
    if superseded:
        print(
            f"[{slug}] archived {len(superseded)} superseded flow version(s): "
            f"{sorted({f'{r.key} v{r.version}' for r in superseded})}",
            flush=True,
        )


async def register_repos(
    slug: str,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
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
    from core.repos.service import create_repo, store_key

    manifest = sample_dir / "repos.json"
    if not manifest.is_file():
        return
    declared = json.loads(manifest.read_text())
    encryptor = get_encryptor()
    registered: list[uuid.UUID] = []

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
            credential_ref = await store_provider_credential(tenant_id, token, encryptor=encryptor)

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
            preview_image=spec.get("preview_image"),
            preview_cmd=spec.get("preview_cmd"),
            preview_port=spec.get("preview_port"),
            preview_env={str(k): str(v) for k, v in (spec.get("preview_env") or {}).items()},
            created_by=owner_id,
        )
        print(f"[{slug}] repo {repo.key} ({spec.get('source_url') or 'store-only'})", flush=True)

        # Registering a repository creates the row; it does not fetch the code. The
        # hosted store stays empty, and everything downstream that reads a tree fails
        # in its own vocabulary -- repo analysis says "none of the requested repos are
        # readable in the hosted store", which sounds like a permissions problem and is
        # actually an empty directory. The HTTP route clones on registration; a seed
        # that calls create_repo directly has to do the same.
        if spec.get("source_url"):
            from adapters.gitremote.registry import resolve_remote
            from adapters.mcp.git_store import GitStore, default_git_root

            store = GitStore(default_git_root())
            remote = resolve_remote(str(spec["source_url"]), spec.get("provider"))
            token = read_secret(secrets_dir, secret_name) if secret_name else None
            try:
                branch = await store.clone_from(
                    store_key(tenant_id, repo.key),
                    str(spec["source_url"]),
                    token=token,
                    userinfo=remote.push_userinfo(token) if remote and token else None,
                )
                print(f"[{slug}] cloned {repo.key} (default branch {branch})", flush=True)
            except Exception as exc:  # noqa: BLE001 -- report; the row is still valid
                print(f"[{slug}] clone FAILED for {repo.key}: {str(exc)[:200]}", flush=True)

        registered.append(repo.id)

    if registered:
        # The knowledge graph is not a side effect of registering a repository; it is a
        # job somebody asks for, normally by pressing Analyze on the repo-graph page. A
        # purge takes the graph with everything else, so a daily loop that never asks
        # rebuilds a deployment where the planning phases retrieve nothing about the
        # code and the workspace assistant cannot answer a question about the repository
        # -- with no error anywhere, because an empty graph is a valid empty graph.
        from api.job_queue_factory import get_job_queue

        job_id = await get_job_queue().enqueue(
            tenant_id,
            "analyze_workspace_repos",
            {
                "tenant_id": str(tenant_id),
                "workspace_id": str(workspace_id),
                "repo_ids": [str(rid) for rid in registered],
            },
        )
        print(
            f"[{slug}] repo graph analysis queued for {len(registered)} repo(s): {job_id}",
            flush=True,
        )


async def ensure_retrieval_models() -> None:
    """Queue the embedding-model download if it is not on this deployment yet.

    Model downloads were deliberately taken out of the installer: they are gigabytes,
    and which models a deployment wants is the operator's choice rather than the
    installer's. The consequence is that a purge takes the model cache with it and a
    freshly installed deployment has no embedder at all -- every turn then fails at
    context assembly with ModelNotDownloadedError, which reads like a broken build
    rather than an empty cache.
    """
    from api.job_queue_factory import get_job_queue
    from core.tenancy.admin import ADMIN_TENANT_ID

    try:
        from adapters.embedding.local.provider import LocalEmbeddingProvider

        LocalEmbeddingProvider()._load()  # noqa: SLF001 -- the cheapest "is it there?"
        print("retrieval models: already cached", flush=True)
        return
    except Exception:  # noqa: BLE001 -- absent, wrong version, unreadable: all mean fetch
        pass

    await get_job_queue().enqueue(ADMIN_TENANT_ID, "download_retrieval_models", {})
    print(
        "retrieval models: download queued -- turns will fail until the worker finishes "
        "(gigabytes; watch the worker log)",
        flush=True,
    )


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

    await ensure_retrieval_models()

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
