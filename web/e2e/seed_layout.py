"""Synthetic persisted layout data, restricted to the browser fixture's own SQLite DB."""
import asyncio
import os
from pathlib import Path

from sqlalchemy.engine import make_url


def owned_database_url(environ=None):
    env = os.environ if environ is None else environ
    url = env.get("PAS_DATABASE_URL", "")
    owned = env.get("PAS_E2E_OWNED_DIRECTORY", "")
    if not owned:
        raise ValueError("Browser seed requires its owned temporary directory")
    directory = Path(owned).resolve()
    parsed = make_url(url)
    if (parsed.drivername != "sqlite+aiosqlite" or not parsed.database or
            not directory.name.startswith("pas-browser-e2e-") or
            Path(parsed.database).resolve() != directory / "metadata.db"):
        raise ValueError("Browser seed requires its owned temporary SQLite database")
    return url


async def main():
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from server.enterprise_identity.service import replace_directory_snapshot
    from server.models import EnterpriseIdentitySource, IdentitySourceProvider, EnterpriseIdentitySourceStatus, User

    engine = create_async_engine(owned_database_url())
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            source = EnterpriseIdentitySource.create(
                name="Enterprise directory with a long source name 用于布局回归",
                provider=IdentitySourceProvider.FEISHU, tenant_id="layout-synthetic-tenant",
            )
            session.add(source)
            await session.flush()
            external_id = "enterprise-member-" + "0123456789" * 8
            await replace_directory_snapshot(session, source,
                users=[{"id": external_id, "display_name": "Enterprise directory account with a long display name 用于布局回归的企业账号"}],
                groups=[{"id": "layout-department", "display_name": "Research and development with a long department name 研发部门", "principal_type": "department"},
                        {"id": "layout-group", "display_name": "Reviewers with a long group name 评审组", "principal_type": "group"}],
                memberships=[{"group_id": key, "member_id": external_id, "member_type": "user"}
                             for key in ("layout-department", "layout-group")])
            source.status = EnterpriseIdentitySourceStatus.DISABLED
            session.add(User(external_id="local-account-" + "abcdefghij" * 8, display_name="Local layout account"))
            await session.commit()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
