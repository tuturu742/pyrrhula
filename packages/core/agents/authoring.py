"""Persona + Agent management (D1.5, plan §12.4): create/edit agents (persona,
role, model profile), model profiles (provider/model/params/fallback), and provider
credentials. The full CRUD ``core.agents.seed``'s docstring deferred to this task.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from core.agents.models import Agent, Persona, PersonaVersion, ProviderCredentialRow
from core.ports.encryptor import Encryptor
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope


class PersonaNotFoundError(Exception):
    pass


class AgentNotFoundError(Exception):
    pass


async def resolve_connection_api_key(
    tenant_id: uuid.UUID, credential_ref: str | None, *, encryptor: Encryptor
) -> str | None:
    """The counterpart to ``store_provider_credential``: fetch a connection's stored key by
    its opaque ``credential_ref`` and decrypt it for an actual provider call. Returns None
    when there's no key on file (local providers need none). The plaintext is used
    transiently for one request and never persisted or logged."""
    if not credential_ref:
        return None
    async with tenant_scope(tenant_id) as session:
        row = await session.get(ProviderCredentialRow, uuid.UUID(credential_ref))
        if row is None:
            return None
        return encryptor.decrypt(row.ciphertext)


async def store_provider_credential(
    tenant_id: uuid.UUID, api_key: str, *, encryptor: Encryptor
) -> uuid.UUID:
    """Encrypts and stores ``api_key``, returning an opaque id -- the only thing that
    ever leaves this function. No corresponding "read back the key" function exists at
    the API layer; decrypting for an actual provider call is a separate, later wiring
    concern (the same "not yet integrated into the runtime" boundary as everywhere else
    this phase), not something D1.5's UI needs or gets access to."""
    async with tenant_scope(tenant_id) as session:
        row = ProviderCredentialRow(tenant_id=tenant_id, ciphertext=encryptor.encrypt(api_key))
        session.add(row)
        await session.flush()
        return row.id


async def create_agent(
    tenant_id: uuid.UUID,
    name: str,
    provider: str,
    model: str,
    *,
    params: dict[str, object] | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    fallback_agent_id: uuid.UUID | None = None,
    encryptor: Encryptor,
) -> Agent:
    credential_ref: str | None = None
    if api_key:
        credential_id = await store_provider_credential(tenant_id, api_key, encryptor=encryptor)
        credential_ref = str(credential_id)

    async with tenant_scope(tenant_id) as session:
        profile = Agent(
            tenant_id=tenant_id,
            name=name,
            provider=provider,
            model=model,
            params=params or {},
            credential_ref=credential_ref,
            api_base=api_base,
            fallback_agent_id=fallback_agent_id,
        )
        session.add(profile)
        await session.flush()
        return profile


async def update_agent(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    name: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    params: dict[str, object] | None = None,
    api_key: str | None = None,
    api_base: str | None | object = ...,
    fallback_agent_id: uuid.UUID | None = None,
    encryptor: Encryptor,
) -> Agent:
    """``api_key`` is write-only and optional: omitted (``None``) means "leave the
    existing credential_ref alone" -- a caller editing params never has to (and never
    can, since it's never returned) re-supply the key just to change temperature.
    ``api_base`` uses the sentinel ``...`` default instead (matching ``update_persona``'s
    own ``entity_id``): unlike a key, ``None`` is a legal, expected *value* here --
    "clear the override, go back to the provider default" -- so omitted has to be
    spelled differently from explicit clear."""
    new_credential_ref: str | None = None
    if api_key:
        credential_id = await store_provider_credential(tenant_id, api_key, encryptor=encryptor)
        new_credential_ref = str(credential_id)

    async with tenant_scope(tenant_id) as session:
        profile = await session.get(Agent, agent_id)
        if profile is None:
            raise AgentNotFoundError(f"no model profile {agent_id}")
        if name is not None:
            profile.name = name
        if provider is not None:
            profile.provider = provider
        if model is not None:
            profile.model = model
        if params is not None:
            profile.params = params
        if new_credential_ref is not None:
            profile.credential_ref = new_credential_ref
        if api_base is not ...:
            profile.api_base = api_base  # type: ignore[assignment]
        if fallback_agent_id is not None:
            profile.fallback_agent_id = fallback_agent_id
        await session.flush()
        return profile


async def get_agent(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> Agent | None:
    async with tenant_scope(tenant_id) as session:
        return await session.get(Agent, agent_id)


async def list_agents(tenant_id: uuid.UUID, *, include_archived: bool = False) -> list[Agent]:
    async with tenant_scope(tenant_id) as session:
        stmt = select(Agent).where(Agent.tenant_id == tenant_id)
        if not include_archived:
            stmt = stmt.where(Agent.archived_at.is_(None))
        rows = (await session.execute(stmt.order_by(Agent.created_at))).scalars()
        return list(rows)


async def archive_agent(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """Soft-delete: stamp ``archived_at`` so the profile drops out of every list. Idempotent
    (re-archiving a row already archived leaves its original timestamp). Never a hard delete
    -- purge is the superuser CLI's job."""
    async with tenant_scope(tenant_id) as session:
        profile = await session.get(Agent, agent_id)
        if profile is None:
            raise AgentNotFoundError(f"no model profile {agent_id}")
        if profile.archived_at is None:
            profile.archived_at = datetime.now(UTC)
        await session.flush()


async def create_persona(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    key: str,
    name: str,
    agent_id: uuid.UUID,
    *,
    persona_type: str = "participant",
    persona_md: str = "",
    entity_id: uuid.UUID | None = None,
    web_search: bool = False,
    params: dict[str, object] | None = None,
) -> Persona:
    async with tenant_scope(tenant_id) as session:
        agent_principal = Principal(tenant_id=tenant_id, kind="agent", display_name=name)
        session.add(agent_principal)
        await session.flush()

        agent = Persona(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            principal_id=agent_principal.id,
            key=key,
            name=name,
            agent_id=agent_id,
            persona_type=persona_type,
            persona_md=persona_md,
            entity_id=entity_id,
            web_search=web_search,
            params=dict(params or {}),
        )
        session.add(agent)
        await session.flush()
        return agent


async def update_persona(
    tenant_id: uuid.UUID,
    persona_id: uuid.UUID,
    *,
    name: str | None = None,
    persona_type: str | None = None,
    persona_md: str | None = None,
    entity_id: uuid.UUID | None | object = ...,
    agent_id: uuid.UUID | None = None,
    web_search: bool | None = None,
    params: dict[str, object] | None = None,
) -> Persona:
    """``entity_id``'s default is the sentinel ``...`` (not ``None``), the same "was this
    field even sent" distinction every other update function here needs: ``None`` is a
    legal *value* (unlink the entity), not "leave it alone" -- an omitted field must be
    spelled differently from an explicit clear."""
    async with tenant_scope(tenant_id) as session:
        agent = await session.get(Persona, persona_id)
        if agent is None:
            raise PersonaNotFoundError(f"no agent {persona_id}")
        if name is not None:
            agent.name = name
        if params is not None:
            agent.params = dict(params)
        if web_search is not None:
            agent.web_search = web_search
        if persona_type is not None:
            agent.persona_type = persona_type
        if persona_md is not None:
            agent.persona_md = persona_md
        if entity_id is not ...:
            agent.entity_id = entity_id  # type: ignore[assignment]
        if agent_id is not None:
            agent.agent_id = agent_id
        await session.flush()
        return agent


async def get_persona(tenant_id: uuid.UUID, persona_id: uuid.UUID) -> Persona | None:
    async with tenant_scope(tenant_id) as session:
        return await session.get(Persona, persona_id)


async def list_personas(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, *, include_archived: bool = False
) -> list[Persona]:
    async with tenant_scope(tenant_id) as session:
        stmt = select(Persona).where(
            Persona.tenant_id == tenant_id, Persona.workspace_id == workspace_id
        )
        if not include_archived:
            stmt = stmt.where(Persona.archived_at.is_(None))
        rows = (await session.execute(stmt.order_by(Persona.created_at))).scalars()
        return list(rows)


async def archive_persona(tenant_id: uuid.UUID, persona_id: uuid.UUID) -> None:
    """Soft-delete: stamp ``archived_at``. The agent then disappears from ``list_personas`` and
    from the scheduler's candidate resolver (core.agents.scheduling), so it never takes
    another turn -- its append-only history (persona versions, past turns) is untouched."""
    async with tenant_scope(tenant_id) as session:
        agent = await session.get(Persona, persona_id)
        if agent is None:
            raise PersonaNotFoundError(f"no agent {persona_id}")
        if agent.archived_at is None:
            agent.archived_at = datetime.now(UTC)
        await session.flush()


async def record_persona_version(
    tenant_id: uuid.UUID,
    persona_id: uuid.UUID,
    persona_md: str,
    *,
    created_by: uuid.UUID | None,
    ai_assisted: bool = False,
) -> Persona:
    """F3.12: writes an ``persona_version`` history row and updates
    ``agent.persona_md`` in the same transaction. Unlike ``update_persona`` (a plain field
    edit with no history), this is the write path an approved edit proposal takes --
    the only thing that populates ``persona_version`` today."""
    async with tenant_scope(tenant_id) as session:
        agent = await session.get(Persona, persona_id)
        if agent is None:
            raise PersonaNotFoundError(f"no agent {persona_id}")
        agent.persona_md = persona_md
        session.add(
            PersonaVersion(
                tenant_id=tenant_id,
                persona_id=persona_id,
                persona_md=persona_md,
                created_by=created_by,
                ai_assisted=ai_assisted,
            )
        )
        await session.flush()
        return agent


async def list_persona_versions(
    tenant_id: uuid.UUID, persona_id: uuid.UUID
) -> list[PersonaVersion]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(PersonaVersion)
                .where(PersonaVersion.persona_id == persona_id)
                .order_by(PersonaVersion.created_at.desc())
            )
        ).scalars()
        return list(rows)
