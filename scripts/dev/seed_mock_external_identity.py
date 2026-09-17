from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from server.bootstrap import load_bootstrap_settings
from server.core.user_workspace import ensure_user_workspace
from server.db.engine import enable_sqlite_foreign_keys
from server.enterprise_identity.service import (
    identity_provider_key,
    upsert_external_user,
)
from server.models import (
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
    User,
    UserStatus,
)


@dataclass(frozen=True)
class SeedResult:
    source_id: str
    user_id: str
    provider_key: str
    external_user_id: str


def _require_local_database(database_url: str) -> None:
    parsed = make_url(database_url)
    if parsed.drivername.startswith("sqlite"):
        return
    if parsed.host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError(
            "development seed refuses a non-local metadata database"
        )


async def seed_mock_identity(
    session: AsyncSession,
    *,
    pas_user: str,
    source_name: str,
    tenant_id: str,
    external_user_id: str,
    display_name: str,
    email: str | None,
) -> SeedResult:
    users = list(
        (
            await session.execute(
                select(User).where(
                    or_(
                        User.id == pas_user,
                        User.external_id == pas_user,
                    )
                )
            )
        ).scalars()
    )
    if len(users) != 1:
        raise ValueError(
            "PAS user must identify exactly one existing user by id or external_id"
        )
    user = users[0]
    if user.status != UserStatus.ACTIVE:
        raise ValueError("PAS user must be active")

    source = await session.scalar(
        select(EnterpriseIdentitySource).where(
            EnterpriseIdentitySource.provider
            == IdentitySourceProvider.FEISHU,
            EnterpriseIdentitySource.tenant_id == tenant_id,
        )
    )
    if source is None:
        source = EnterpriseIdentitySource.create(
            name=source_name,
            provider=IdentitySourceProvider.FEISHU,
            tenant_id=tenant_id,
        )
        session.add(source)
        await session.flush()

    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    # This source is only a local identity namespace for UserInfo mapping.
    # No provider credentials means the background directory sync ignores it.
    source.config_ciphertext = None
    source.last_synced_at = datetime.now(UTC)
    source.last_error = None
    source.stale_after_seconds = 86400

    await upsert_external_user(
        session,
        source,
        external_user_id=external_user_id,
        display_name=display_name,
        email=email,
        user=user,
    )
    await ensure_user_workspace(session, user.id)
    await session.flush()
    return SeedResult(
        source_id=source.id,
        user_id=user.id,
        provider_key=identity_provider_key(source),
        external_user_id=external_user_id,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Seed an idempotent local identity source and PAS user mapping "
            "for external Token Exchange tests."
        )
    )
    parser.add_argument(
        "--confirm-local-development",
        action="store_true",
        help="Required acknowledgement that this changes local test data.",
    )
    parser.add_argument(
        "--pas-user",
        required=True,
        help="Existing PAS user id or external_id to map.",
    )
    parser.add_argument(
        "--source-name",
        default="Mock external identity source",
    )
    parser.add_argument("--tenant-id", default="mock-external-tenant")
    parser.add_argument(
        "--external-user-id",
        default="mock-directory-user",
    )
    parser.add_argument("--display-name", default="Mock external user")
    parser.add_argument("--email", default="mock-external@example.com")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> SeedResult:
    if not args.confirm_local_development:
        raise ValueError("--confirm-local-development is required")
    bootstrap = load_bootstrap_settings()
    _require_local_database(bootstrap.database_url)
    engine = create_async_engine(
        bootstrap.database_url,
        poolclass=NullPool,
    )
    enable_sqlite_foreign_keys(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await seed_mock_identity(
                session,
                pas_user=args.pas_user,
                source_name=args.source_name,
                tenant_id=args.tenant_id,
                external_user_id=args.external_user_id,
                display_name=args.display_name,
                email=args.email,
            )
            await session.commit()
            return result
    finally:
        await engine.dispose()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(_run(args))
    print(
        "\n".join(
            (
                "Mock external identity is ready.",
                f"  PAS source ID:    {result.source_id}",
                f"  PAS user ID:      {result.user_id}",
                f"  Provider key:     {result.provider_key}",
                f"  External user ID: {result.external_user_id}",
            )
        )
    )


if __name__ == "__main__":
    main()
