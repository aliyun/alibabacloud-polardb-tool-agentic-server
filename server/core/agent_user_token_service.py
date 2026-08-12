from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from server.core.agent_token_service import hash_agent_token
from server.core.agent_access import has_agent_access
from server.core.crypto import decrypt, encrypt
from server.models import (
    Agent,
    AgentStatus,
    AgentUserAssignment,
    AgentUserToken,
    User,
    UserStatus,
)
from server.models.base import utc_now

TOKEN_PREFIX = "pas_user_agent_"


class ActiveTokenExists(ValueError):
    pass


@dataclass(frozen=True)
class AgentUserTokenContext:
    token: AgentUserToken
    assignment: AgentUserAssignment
    agent: Agent
    user: User


def generate_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def is_active(row: AgentUserToken, now: datetime | None = None) -> bool:
    current = now or utc_now()
    return (
        row.revoked_at is None
        and row.token_ciphertext is not None
        and (row.expires_at is None or _as_utc(row.expires_at) > current)
    )


def token_status(row: AgentUserToken) -> str:
    if row.revoked_at is not None or row.token_ciphertext is None:
        return "revoked"
    if row.expires_at is not None and _as_utc(row.expires_at) <= utc_now():
        return "expired"
    return "active"


async def _get_assignment(
    session: AsyncSession,
    assignment_id: str,
    *,
    for_update: bool = False,
) -> AgentUserAssignment:
    statement = (
        select(AgentUserAssignment)
        .options(
            selectinload(AgentUserAssignment.agent),
            selectinload(AgentUserAssignment.user),
            selectinload(AgentUserAssignment.token),
        )
        .where(AgentUserAssignment.id == assignment_id)
    )
    if for_update:
        statement = statement.with_for_update()
    assignment = (await session.execute(statement)).scalar_one_or_none()
    if assignment is None:
        raise LookupError("Agent user assignment not found")
    return assignment


async def _require_active_assignment(
    session: AsyncSession,
    assignment: AgentUserAssignment,
) -> None:
    if assignment.agent.status != AgentStatus.ACTIVE:
        raise ValueError("Agent is disabled")
    if assignment.user.status != UserStatus.ACTIVE:
        raise ValueError("User is disabled")
    if not await has_agent_access(
        session,
        assignment.agent_id,
        assignment.user_id,
    ):
        raise ValueError("Agent access is not active")


def _replace_token(row: AgentUserToken, plaintext: str) -> None:
    row.token_prefix = plaintext[:32]
    row.token_hash = hash_agent_token(plaintext)
    row.token_ciphertext = encrypt(plaintext)
    row.expires_at = None
    row.revoked_at = None
    row.last_used_at = None


async def issue_token(
    session: AsyncSession,
    assignment_id: str,
) -> tuple[AgentUserToken, str]:
    assignment = await _get_assignment(
        session, assignment_id, for_update=True
    )
    await _require_active_assignment(session, assignment)
    if assignment.token is not None and is_active(assignment.token):
        raise ActiveTokenExists("Agent user token is already active")

    plaintext = generate_token()
    if assignment.token is not None:
        row = assignment.token
        _replace_token(row, plaintext)
        await session.flush()
        return row, plaintext

    row = AgentUserToken(
        assignment_id=assignment.id,
        token_prefix=plaintext[:32],
        token_hash=hash_agent_token(plaintext),
        token_ciphertext=encrypt(plaintext),
    )
    try:
        async with session.begin_nested():
            session.add(row)
            assignment.token = row
            await session.flush()
    except IntegrityError as exc:
        raise ActiveTokenExists("Agent user token is already active") from exc
    return row, plaintext


async def reveal_token(
    session: AsyncSession,
    assignment_id: str,
) -> tuple[AgentUserToken, str]:
    assignment = await _get_assignment(session, assignment_id)
    await _require_active_assignment(session, assignment)
    row = assignment.token
    if row is None or not is_active(row):
        raise ValueError("Agent user token is not active")
    assert row.token_ciphertext is not None
    return row, decrypt(row.token_ciphertext)


async def regenerate_token(
    session: AsyncSession,
    assignment_id: str,
) -> tuple[AgentUserToken, str]:
    assignment = await _get_assignment(
        session, assignment_id, for_update=True
    )
    await _require_active_assignment(session, assignment)
    if assignment.token is None:
        return await issue_token(session, assignment_id)
    plaintext = generate_token()
    _replace_token(assignment.token, plaintext)
    await session.flush()
    return assignment.token, plaintext


async def revoke_token(
    session: AsyncSession,
    assignment_id: str,
) -> AgentUserToken:
    assignment = await _get_assignment(
        session, assignment_id, for_update=True
    )
    row = assignment.token
    if row is None:
        raise LookupError("Agent user token not found")
    if row.revoked_at is None:
        row.revoked_at = utc_now()
    row.token_ciphertext = None
    await session.flush()
    return row


async def resolve_token(
    session: AsyncSession,
    plaintext: str,
) -> AgentUserTokenContext | None:
    if not plaintext.startswith(TOKEN_PREFIX):
        return None
    row = (
        await session.execute(
            select(AgentUserToken)
            .options(
                selectinload(AgentUserToken.assignment).selectinload(
                    AgentUserAssignment.agent
                ),
                selectinload(AgentUserToken.assignment).selectinload(
                    AgentUserAssignment.user
                ),
            )
            .where(AgentUserToken.token_hash == hash_agent_token(plaintext))
        )
    ).scalar_one_or_none()
    if row is None or not is_active(row):
        return None
    assert row.token_ciphertext is not None
    try:
        if decrypt(row.token_ciphertext) != plaintext:
            return None
    except Exception:
        return None
    assignment = row.assignment
    if (
        assignment.agent.status != AgentStatus.ACTIVE
        or assignment.user.status != UserStatus.ACTIVE
    ):
        return None
    if not await has_agent_access(
        session,
        assignment.agent_id,
        assignment.user_id,
    ):
        return None
    return AgentUserTokenContext(
        token=row,
        assignment=assignment,
        agent=assignment.agent,
        user=assignment.user,
    )
