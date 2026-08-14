from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.models import (
    AuthProvider,
    Base,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalStatus,
    EnterprisePrincipalType,
    User,
)
from server.polarrag.identity import (
    IdentityContextUnavailable,
    resolve_acl_context,
    validate_provider_results,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def test_resolve_acl_context_filters_and_deduplicates_assignments(
    session,
) -> None:
    user = User(
        external_id="identity-user",
        display_name="Identity User",
        auth_provider=AuthProvider.BUILTIN,
    )
    session.add(user)
    await session.flush()
    now = datetime.now(UTC)
    assignments = [
        ("tenant-a", "feishu", EnterprisePrincipalType.USER, "ou-1", EnterprisePrincipalStatus.ACTIVE, None),
        ("tenant-a", "feishu", EnterprisePrincipalType.GROUP, "group-1", EnterprisePrincipalStatus.ACTIVE, None),
        ("tenant-a", "sharepoint", EnterprisePrincipalType.USER, "sp-1", EnterprisePrincipalStatus.ACTIVE, now + timedelta(hours=1)),
        ("tenant-a", "sharepoint", EnterprisePrincipalType.GROUP, "expired", EnterprisePrincipalStatus.ACTIVE, now - timedelta(seconds=1)),
        ("tenant-a", "feishu", EnterprisePrincipalType.GROUP, "disabled", EnterprisePrincipalStatus.DISABLED, None),
        ("tenant-b", "feishu", EnterprisePrincipalType.USER, "wrong-domain", EnterprisePrincipalStatus.ACTIVE, None),
    ]
    for domain, provider, principal_type, principal_id, status, valid_until in assignments:
        session.add(
            EnterprisePrincipalAssignment.create(
                pas_user_id=user.id,
                identity_domain=domain,
                provider=provider,
                principal_type=principal_type,
                principal_id=principal_id,
                source=EnterprisePrincipalSource.ADMIN_MANAGED,
                status=status,
                valid_until=valid_until,
            )
        )
    await session.commit()

    context = await resolve_acl_context(session, user.id, "tenant-a", now=now)

    assert context == {
        "identity_domain": "tenant-a",
        "principals": [
            {"provider": "feishu", "type": "group", "id": "group-1"},
            {"provider": "feishu", "type": "user", "id": "ou-1"},
            {
                "provider": "polarrag",
                "type": "user",
                "id": user.external_id,
            },
            {"provider": "sharepoint", "type": "user", "id": "sp-1"},
        ],
    }


async def test_resolve_acl_context_fails_closed_without_principals(
    session,
) -> None:
    user = User(
        external_id="empty-identity",
        display_name="Empty Identity",
        auth_provider=AuthProvider.BUILTIN,
    )
    session.add(user)
    await session.commit()

    with pytest.raises(IdentityContextUnavailable):
        await resolve_acl_context(session, user.id, "tenant-a")


async def test_resolve_acl_context_accepts_native_user_assignment_only(
    session,
) -> None:
    user = User(
        external_id="native-user",
        display_name="Native User",
        auth_provider=AuthProvider.BUILTIN,
    )
    session.add(user)
    await session.flush()
    session.add(
        EnterprisePrincipalAssignment.create(
            pas_user_id=user.id,
            identity_domain="tenant-a",
            provider="polarrag",
            principal_type=EnterprisePrincipalType.USER,
            principal_id=user.external_id,
            source=EnterprisePrincipalSource.ADMIN_MANAGED,
            canonical_user_external_id=user.external_id,
        )
    )
    await session.commit()

    assert await resolve_acl_context(session, user.id, "tenant-a") == {
        "identity_domain": "tenant-a",
        "principals": [
            {"provider": "polarrag", "type": "user", "id": "native-user"}
        ],
    }


@pytest.mark.parametrize(
    ("auth_provider", "external_id"),
    [
        (AuthProvider.BUILTIN, "native-user "),
        (AuthProvider.OIDC, "oidc:native-user "),
    ],
)
async def test_resolve_acl_context_preserves_native_external_id_whitespace(
    session,
    auth_provider,
    external_id,
) -> None:
    user = User(
        external_id=external_id,
        display_name="Native User",
        auth_provider=auth_provider,
    )
    session.add(user)
    await session.flush()
    session.add(
        EnterprisePrincipalAssignment.create(
            pas_user_id=user.id,
            identity_domain="tenant-a",
            provider="polarrag",
            principal_type=EnterprisePrincipalType.USER,
            principal_id=user.external_id,
            source=EnterprisePrincipalSource.ADMIN_MANAGED,
            canonical_user_external_id=user.external_id,
        )
    )
    await session.commit()

    assert await resolve_acl_context(session, user.id, "tenant-a") == {
        "identity_domain": "tenant-a",
        "principals": [
            {"provider": "polarrag", "type": "user", "id": external_id}
        ],
    }


async def test_resolve_acl_context_rejects_spoofed_native_assignment(
    session,
) -> None:
    user = User(
        external_id="native-user",
        display_name="Native User",
        auth_provider=AuthProvider.BUILTIN,
    )
    session.add(user)
    await session.flush()
    session.add(
        EnterprisePrincipalAssignment(
            pas_user_id=user.id,
            identity_domain="tenant-a",
            provider="polarrag",
            principal_type=EnterprisePrincipalType.USER,
            principal_id="another-user",
            source=EnterprisePrincipalSource.ADMIN_MANAGED,
            status=EnterprisePrincipalStatus.ACTIVE,
            user_principal_key="spoofed-native-user",
        )
    )
    await session.commit()

    with pytest.raises(IdentityContextUnavailable):
        await resolve_acl_context(session, user.id, "tenant-a")


def test_remote_provider_results_cannot_spoof_provider() -> None:
    with pytest.raises(ValueError, match="provider"):
        validate_provider_results(
            "feishu",
            [{"provider": "sharepoint", "type": "user", "id": "spoofed"}],
        )

    with pytest.raises(ValueError, match="provider"):
        validate_provider_results(
            "polarrag",
            [{"provider": "polarrag", "type": "user", "id": "spoofed"}],
        )
