from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.crypto import decrypt, encrypt
from server.models import Agent, AgentAPIToken, AgentTokenRevealLimit
from server.models.base import utc_now

REVEAL_LIMIT = 5
REVEAL_WINDOW = timedelta(minutes=1)
REVEAL_STATE_RETENTION = timedelta(days=1)


class TokenRevealRateLimitExceeded(Exception):
    pass


def _generate_agent_token() -> str:
    return f"pas_agent_{secrets.token_urlsafe(32)}"


def hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _require_agent(
    session: AsyncSession, agent_id: str, *, for_update: bool = False
) -> Agent:
    if for_update:
        result = await session.execute(
            select(Agent).where(Agent.id == agent_id).with_for_update()
        )
        agent = result.scalar_one_or_none()
    else:
        agent = await session.get(Agent, agent_id)
    if agent is None:
        raise LookupError("Agent not found")
    return agent


async def _get_token(
    session: AsyncSession, agent_id: str, *, for_update: bool = False
) -> AgentAPIToken | None:
    statement = select(AgentAPIToken).where(AgentAPIToken.agent_id == agent_id)
    if for_update:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    return result.scalar_one_or_none()


def _is_active(row: AgentAPIToken, now: datetime) -> bool:
    return (
        row.revoked_at is None
        and (row.expires_at is None or _as_utc(row.expires_at) > now)
        and row.token_ciphertext is not None
    )


async def get_or_create_token(
    session: AsyncSession,
    agent_id: str,
    expires_at: datetime | None,
) -> tuple[AgentAPIToken, str]:
    await _require_agent(session, agent_id)
    row = await _get_token(session, agent_id)
    if row is not None and _is_active(row, utc_now()):
        assert row.token_ciphertext is not None
        return row, decrypt(row.token_ciphertext)
    return await regenerate_token(session, agent_id, expires_at)


async def reveal_token(session: AsyncSession, agent_id: str) -> str:
    await _require_agent(session, agent_id)
    row = await _get_token(session, agent_id)
    if row is None or not _is_active(row, utc_now()):
        if row is not None and row.expires_at is not None:
            if _as_utc(row.expires_at) <= utc_now():
                row.token_ciphertext = None
                await session.flush()
        raise ValueError("Agent token is not active")
    assert row.token_ciphertext is not None
    return decrypt(row.token_ciphertext)


async def regenerate_token(
    session: AsyncSession,
    agent_id: str,
    expires_at: datetime | None,
) -> tuple[AgentAPIToken, str]:
    await _require_agent(session, agent_id, for_update=True)
    plaintext = _generate_agent_token()
    # Encrypt before changing persistent state so missing/invalid key fails closed.
    ciphertext = encrypt(plaintext)
    row = await _get_token(session, agent_id, for_update=True)
    inserted = False
    if row is None:
        candidate = AgentAPIToken(
            agent_id=agent_id,
            token_prefix=plaintext[:32],
            token_hash=hash_agent_token(plaintext),
            token_ciphertext=ciphertext,
            expires_at=expires_at,
        )
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            row = candidate
            inserted = True
        except IntegrityError:
            # Another transaction won first-generation insertion. Read and
            # rotate that same row so both overlapping calls linearize.
            row = await _get_token(session, agent_id, for_update=True)
            if row is None:
                raise
    if not inserted:
        row.token_prefix = plaintext[:32]
        row.token_hash = hash_agent_token(plaintext)
        row.token_ciphertext = ciphertext
        row.expires_at = expires_at
        row.revoked_at = None
        row.last_used_at = None
    await session.flush()
    return row, plaintext


async def consume_reveal_budget(
    session: AsyncSession,
    admin_id: str,
    agent_id: str,
    *,
    now: datetime | None = None,
) -> None:
    current_time = now or utc_now()
    window_cutoff = current_time - REVEAL_WINDOW
    retention_cutoff = current_time - REVEAL_STATE_RETENTION
    await session.execute(
        delete(AgentTokenRevealLimit).where(
            AgentTokenRevealLimit.window_started_at < retention_cutoff
        )
    )

    for _ in range(3):
        current_window = cast(
            CursorResult[Any],
            await session.execute(
                update(AgentTokenRevealLimit)
                .where(
                    AgentTokenRevealLimit.admin_id == admin_id,
                    AgentTokenRevealLimit.agent_id == agent_id,
                    AgentTokenRevealLimit.window_started_at > window_cutoff,
                    AgentTokenRevealLimit.request_count < REVEAL_LIMIT,
                )
                .values(request_count=AgentTokenRevealLimit.request_count + 1)
            )
        )
        if current_window.rowcount == 1:
            return

        reset_window = cast(
            CursorResult[Any],
            await session.execute(
                update(AgentTokenRevealLimit)
                .where(
                    AgentTokenRevealLimit.admin_id == admin_id,
                    AgentTokenRevealLimit.agent_id == agent_id,
                    AgentTokenRevealLimit.window_started_at <= window_cutoff,
                )
                .values(window_started_at=current_time, request_count=1)
            )
        )
        if reset_window.rowcount == 1:
            return

        row = await session.get(
            AgentTokenRevealLimit, (admin_id, agent_id)
        )
        if row is not None:
            raise TokenRevealRateLimitExceeded

        try:
            async with session.begin_nested():
                session.add(
                    AgentTokenRevealLimit(
                        admin_id=admin_id,
                        agent_id=agent_id,
                        window_started_at=current_time,
                        request_count=1,
                    )
                )
                await session.flush()
            return
        except IntegrityError:
            continue

    raise TokenRevealRateLimitExceeded


async def revoke_token(
    session: AsyncSession, agent_id: str
) -> AgentAPIToken:
    await _require_agent(session, agent_id, for_update=True)
    row = await _get_token(session, agent_id, for_update=True)
    if row is None:
        raise LookupError("Agent token not found")
    if row.revoked_at is None:
        row.revoked_at = utc_now()
    row.token_ciphertext = None
    await session.flush()
    return row


# Transitional call site compatibility while database Tool tests move from the
# old User-backed multi-token helper to independent Agents.
async def create_agent_token(
    session: AsyncSession,
    agent_id: str,
    _name: str,
    expires_at: datetime | None,
) -> tuple[AgentAPIToken, str]:
    return await regenerate_token(session, agent_id, expires_at)
