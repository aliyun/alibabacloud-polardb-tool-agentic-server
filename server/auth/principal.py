from __future__ import annotations

import enum
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from server.models import Agent, AgentStatus, User, UserStatus


class PrincipalKind(str, enum.Enum):
    USER = "user"
    AGENT = "agent"


@dataclass(frozen=True)
class Principal:
    kind: PrincipalKind
    id: str


class PrincipalAuthenticationError(Exception):
    """Base class for subjects that cannot authenticate a principal."""


class InvalidPrincipalSubject(PrincipalAuthenticationError, ValueError):
    pass


class PrincipalNotFound(PrincipalAuthenticationError, LookupError):
    pass


class PrincipalDisabled(PrincipalAuthenticationError, PermissionError):
    pass


class PrincipalKindMismatch(PrincipalAuthenticationError, PermissionError):
    pass


def user_subject(user_id: str) -> str:
    return f"{PrincipalKind.USER.value}:{user_id}"


def agent_subject(agent_id: str) -> str:
    return f"{PrincipalKind.AGENT.value}:{agent_id}"


def parse_subject(subject: str) -> Principal:
    prefix, separator, identifier = subject.partition(":")
    if separator != ":" or not identifier:
        raise InvalidPrincipalSubject(subject)
    try:
        kind = PrincipalKind(prefix)
    except ValueError as exc:
        raise InvalidPrincipalSubject(subject) from exc
    return Principal(kind, identifier)


async def get_current_principal(
    session: AsyncSession, subject: str
) -> Principal:
    principal = parse_subject(subject)
    if principal.kind == PrincipalKind.USER:
        user = await session.get(User, principal.id)
        if user is None:
            raise PrincipalNotFound(subject)
        if user.status == UserStatus.DISABLED:
            raise PrincipalDisabled(subject)
    else:
        agent = await session.get(Agent, principal.id)
        if agent is None:
            raise PrincipalNotFound(subject)
        if agent.status == AgentStatus.DISABLED:
            raise PrincipalDisabled(subject)
    return principal


async def require_current_actor(
    session: AsyncSession,
    subject: str,
    required_kind: PrincipalKind,
) -> User | Agent:
    """Resolve an active principal only when it matches the handler contract."""
    principal = await get_current_principal(session, subject)
    if principal.kind != required_kind:
        raise PrincipalKindMismatch(subject)

    if required_kind == PrincipalKind.USER:
        actor: User | Agent | None = await session.get(User, principal.id)
    else:
        actor = await session.get(Agent, principal.id)
    if actor is None:
        raise PrincipalNotFound(subject)
    return actor
