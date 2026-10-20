"""Read-only enterprise selectors and batched identity response data."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from server.enterprise_identity.service import identity_provider_key
from server.models import AuthProvider, EnterpriseIdentitySource, User, UserExternalIdentity


async def resolve_enterprise_user_ids(
    session: AsyncSession, identity_source_id: str, external_user_ids: list[str],
) -> list[str]:
    source = await session.get(EnterpriseIdentitySource, identity_source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Enterprise identity source not found")
    by_subject: dict[str, str] = {}
    for start in range(0, len(external_user_ids), 500):
        rows = await session.execute(
            select(UserExternalIdentity.external_subject, UserExternalIdentity.user_id)
            .join(User, User.id == UserExternalIdentity.user_id)
            .where(
                UserExternalIdentity.identity_provider == identity_provider_key(source),
                UserExternalIdentity.external_subject.in_(external_user_ids[start:start + 500]),
            )
        )
        by_subject.update(rows.tuples().all())
    if any(subject not in by_subject for subject in external_user_ids):
        raise HTTPException(status_code=404, detail="Enterprise user not found")
    return [by_subject[subject] for subject in external_user_ids]


async def enterprise_identities_by_user(
    session: AsyncSession, user_ids: list[str],
) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {user_id: [] for user_id in user_ids}
    if not user_ids:
        return result
    sources = (await session.execute(select(EnterpriseIdentitySource))).scalars().all()
    by_provider = {identity_provider_key(source): source.id for source in sources}
    user_ids = list(result)
    for start in range(0, len(user_ids), 500):
        users = (await session.execute(
            select(User.id, User.external_id, User.auth_provider)
            .where(User.id.in_(user_ids[start:start + 500]))
        )).all()
        canonical: dict[tuple[str, str], str] = {}
        for user_id, external_id, auth_provider in users:
            if auth_provider == AuthProvider.OIDC:
                for provider in by_provider:
                    prefix = f"{provider}:"
                    if external_id.startswith(prefix):
                        canonical[(provider, external_id[len(prefix):])] = user_id
                        break
        identities = (await session.execute(
            select(UserExternalIdentity).where(or_(
                UserExternalIdentity.user_id.in_([user.id for user in users]),
                tuple_(UserExternalIdentity.identity_provider, UserExternalIdentity.external_subject)
                .in_(list(canonical)),
            ))
        )).scalars().all()
        explicit_keys = {(row.identity_provider, row.external_subject) for row in identities}
        pairs: dict[str, set[tuple[str, str]]] = {user.id: set() for user in users}
        for row in identities:
            if row.user_id in pairs and row.identity_provider in by_provider:
                pairs[row.user_id].add((by_provider[row.identity_provider], row.external_subject))
        for (provider, subject), user_id in canonical.items():
            if (provider, subject) not in explicit_keys:
                pairs[user_id].add((by_provider[provider], subject))
        for user_id, values in pairs.items():
            result[user_id] = [
                {"identity_source_id": source_id, "external_user_id": subject}
                for source_id, subject in sorted(values)
            ]
    return result
