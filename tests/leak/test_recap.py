"""Phase recaps tell a viewer nothing its context would not.

A recap is a new path into a model's context, so it is held to the same exclusion as
everything else: a disclosure appears only for whom it was made and never carries the
secret's text; a private message is never counted, let alone quoted, for a reader
outside its audience; a vote that reveals only its outcome never names its voters.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.permission.role_permission import RolePermissionService
from core.process import participants as parts
from core.process.dsl.schema import RecapSpec
from core.process.tests.test_participants import _game
from core.secrets.authoring import create_secret
from core.secrets.decisions import record_disclosure_event
from core.sessions.models import MessageRow, SessionEventRow, SessionRow
from core.sessions.recap import PhaseSpan, build_recaps
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope

_PERMISSIONS = RolePermissionService()
_PLAINTEXT = f"PLAINTEXT-{uuid.uuid4().hex}"
_GIST = f"GIST-{uuid.uuid4().hex}"


async def _next_seq(tenant_id: uuid.UUID, session_id: uuid.UUID) -> int:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        seq = row.next_event_seq
        row.next_event_seq = seq + 1
        return seq


async def _say(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    author: uuid.UUID,
    text: str,
    audience: list[uuid.UUID] | None = None,
) -> MessageRow:
    seq = await _next_seq(tenant_id, session_id)
    async with tenant_scope(tenant_id) as session:
        message = MessageRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=seq,
            author_principal_id=author,
            role="assistant",
            content_md=text,
            audience=audience,
        )
        session.add(message)
        await session.flush()
        session.expunge(message)
        return message


async def _recap_for(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    dsl: object,
    principal_id: uuid.UUID,
    name: str,
    *,
    supervisor: bool = False,
) -> str:
    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, principal_id)
        end = (await session.get(SessionRow, session_id)).next_event_seq  # type: ignore[union-attr]
    assert viewer is not None
    recaps = await build_recaps(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        viewer=viewer,
        viewer_name=name,
        viewer_is_supervisor=supervisor,
        phase=dsl.phases["talk"],  # type: ignore[attr-defined]
        definition=dsl,  # type: ignore[arg-type]
        spec=RecapSpec(),
        spans=[PhaseSpan("talk", "Talk", -1, end)],
        permission_service=_PERMISSIONS,
    )
    return recaps.get(end, "")


async def test_a_disclosure_is_recapped_only_for_whom_it_was_made(db_available: None) -> None:
    tenant_id, workspace_id, session_id, dsl, names = await _game()
    by_name = {n: p for p, n in names.items()}
    ada, bo, cy = by_name["Ada"], by_name["Bo"], by_name["Cy"]

    async with tenant_scope(tenant_id) as session:
        owner = (
            await session.execute(select(Principal.id).where(Principal.kind == "human").limit(1))
        ).scalar_one()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=owner,
                role="facilitator",
            )
        )
    secret = await create_secret(
        tenant_id,
        workspace_id,
        owner,
        subject_kind="workspace",
        subject_id=workspace_id,
        content=_PLAINTEXT,
        gist=_GIST,
        scope_key="workspace_public",
        encryptor=IdentityEncryptor(),
        permission_service=_PERMISSIONS,
        moderation_provider=AllowAllModerationProvider(),
    )
    said = await _say(tenant_id, session_id, ada, "Bo, the shrine is older than the town.")
    await record_disclosure_event(
        tenant_id,
        secret.id,
        session_id,
        said.event_seq,
        "hint",
        {"principal_ids": [str(bo)]},
        disclosed_by_principal_id=ada,
        message_id=said.id,
    )

    told = await _recap_for(tenant_id, workspace_id, session_id, dsl, bo, "Bo")
    assert "Ada hinted to you" in told
    assert "the shrine is older than the town" in told  # the words Bo heard
    outsider = await _recap_for(tenant_id, workspace_id, session_id, dsl, cy, "Cy")
    assert "What you were told" not in outsider
    for recap in (told, outsider):
        assert _PLAINTEXT not in recap and _GIST not in recap


async def test_a_private_message_is_not_even_counted_for_an_outsider(db_available: None) -> None:
    tenant_id, workspace_id, session_id, dsl, names = await _game()
    by_name = {n: p for p, n in names.items()}
    ada, bo, cy = by_name["Ada"], by_name["Bo"], by_name["Cy"]
    canary = f"CANARY-{uuid.uuid4().hex}"
    await _say(tenant_id, session_id, ada, f"Bo, {canary}", audience=[ada, bo])
    await _say(tenant_id, session_id, cy, "Anyone seen Ada?")

    member = await _recap_for(tenant_id, workspace_id, session_id, dsl, bo, "Bo")
    assert "Ada 1× (naming you)" in member
    outsider = await _recap_for(tenant_id, workspace_id, session_id, dsl, cy, "Cy")
    assert "Who spoke: you 1× (naming Ada)" in outsider
    assert "Ada 1×" not in outsider
    for recap in (member, outsider):
        assert canary not in recap


async def test_an_outcome_only_vote_never_names_its_voters(db_available: None) -> None:
    tenant_id, workspace_id, session_id, dsl, names = await _game()
    by_name = {n: p for p, n in names.items()}
    seq = await _next_seq(tenant_id, session_id)
    payload = {
        "phase": "covert_vote",
        "votes": {"Ada": "Bo", "Di": "Bo"},
        "abstained": [],
        "tied": [],
        "chosen": "Bo",
        "changes": [{"name": "Bo", "var": "status", "old": "in", "new": "out"}],
        "reveal": "outcome",
    }
    async with tenant_scope(tenant_id) as session:
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=seq,
                kind="ballot_result",
                payload=payload,
            )
        )
    town = await _recap_for(tenant_id, workspace_id, session_id, dsl, by_name["Cy"], "Cy")
    assert "Bo was chosen" in town
    assert "Ada" not in town and "Di" not in town
    host = await _recap_for(
        tenant_id, workspace_id, session_id, dsl, by_name["Host"], "Host", supervisor=True
    )
    assert "Ada -> Bo" in host
    # the rule the recap uses is the one the participants table uses
    assert "Ada" not in parts.render_ballot_result(payload, viewer_is_supervisor=False)
