from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

from server.models import (
    AuthProvider,
    Base,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalStatus,
    EnterprisePrincipalType,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    User,
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


async def _user(session, external_id: str) -> User:
    user = User(
        external_id=external_id,
        display_name=external_id,
        auth_provider=AuthProvider.BUILTIN,
    )
    session.add(user)
    await session.flush()
    return user


async def test_polarrag_catalog_uses_stable_unique_upstream_coordinates(
    session,
) -> None:
    creator = await _user(session, "catalog-creator")
    instance = PolarRAGInstance(
        name="primary",
        scheme="https",
        host="rag.example.test",
        port=9200,
        username_ciphertext="encrypted-user",
        password_ciphertext="encrypted-password",
        tls_verify=True,
        status=PolarRAGInstanceStatus.ACTIVE,
        created_by=creator.id,
    )
    session.add(instance)
    await session.flush()
    space = PolarRAGSpace(
        polarrag_instance_id=instance.id,
        space_id="space-a",
        name="Space A",
        identity_domain="tenant-a",
        enabled=True,
    )
    session.add(space)
    await session.flush()
    resource = KnowledgeResource(
        knowledge_space_id=space.knowledge_space_id,
        polarrag_instance_id=instance.id,
        space_id=space.space_id,
        kb_id="kb-a",
        name="Knowledge A",
        kb_type="PUBLIC",
        identity_domain="tenant-a",
        binding_mode=KnowledgeBindingMode.DOMAIN,
        sync_status=KnowledgeResourceSyncStatus.ACTIVE,
        enabled=True,
    )
    session.add(resource)
    await session.commit()

    duplicate = KnowledgeResource(
        knowledge_space_id=space.knowledge_space_id,
        polarrag_instance_id=instance.id,
        space_id=space.space_id,
        kb_id="kb-a",
        name="Duplicate",
        kb_type="PUBLIC",
        identity_domain="tenant-a",
        binding_mode=KnowledgeBindingMode.DOMAIN,
        sync_status=KnowledgeResourceSyncStatus.ACTIVE,
        enabled=True,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_external_user_principal_maps_to_only_one_pas_user(
    session,
) -> None:
    first = await _user(session, "first")
    second = await _user(session, "second")
    first_assignment = EnterprisePrincipalAssignment.create(
        pas_user_id=first.id,
        identity_domain="tenant-a",
        provider="feishu",
        principal_type=EnterprisePrincipalType.USER,
        principal_id="ou-1",
        source=EnterprisePrincipalSource.ADMIN_MANAGED,
    )
    session.add(first_assignment)
    await session.commit()

    second_assignment = EnterprisePrincipalAssignment.create(
        pas_user_id=second.id,
        identity_domain="tenant-a",
        provider="feishu",
        principal_type=EnterprisePrincipalType.USER,
        principal_id="ou-1",
        source=EnterprisePrincipalSource.ADMIN_MANAGED,
    )
    session.add(second_assignment)
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_external_group_principal_can_map_to_multiple_pas_users(
    session,
) -> None:
    first = await _user(session, "first-group")
    second = await _user(session, "second-group")
    for user in (first, second):
        session.add(
            EnterprisePrincipalAssignment.create(
                pas_user_id=user.id,
                identity_domain="tenant-a",
                provider="sharepoint",
                principal_type=EnterprisePrincipalType.GROUP,
                principal_id="group-1",
                source=EnterprisePrincipalSource.ADMIN_MANAGED,
            )
        )
    await session.commit()


def test_principal_assignment_create_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="provider"):
        EnterprisePrincipalAssignment.create(
            pas_user_id="user-1",
            identity_domain="tenant-a",
            provider="caller-controlled",
            principal_type=EnterprisePrincipalType.USER,
            principal_id="id-1",
            source=EnterprisePrincipalSource.ADMIN_MANAGED,
        )


def test_principal_active_status_and_expiry_are_separate() -> None:
    assignment = EnterprisePrincipalAssignment.create(
        pas_user_id="user-1",
        identity_domain="tenant-a",
        provider="feishu",
        principal_type=EnterprisePrincipalType.USER,
        principal_id="ou-1",
        source=EnterprisePrincipalSource.ADMIN_MANAGED,
        status=EnterprisePrincipalStatus.ACTIVE,
        valid_until=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert assignment.status == EnterprisePrincipalStatus.ACTIVE


@pytest.mark.parametrize(
    "dialect",
    [sqlite.dialect(), mysql.dialect(), postgresql.dialect()],
    ids=["sqlite", "mysql", "postgresql"],
)
def test_polarrag_catalog_ddl_compiles_for_supported_databases(dialect) -> None:
    table_names = {
        "polarrag_instances",
        "polarrag_spaces",
        "knowledge_resources",
        "enterprise_principal_assignments",
    }

    for table_name in table_names:
        table = Base.metadata.tables[table_name]
        assert table_name in str(CreateTable(table).compile(dialect=dialect))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=dialect))


@pytest.mark.parametrize(
    "dialect",
    [sqlite.dialect(), mysql.dialect(), postgresql.dialect()],
    ids=["sqlite", "mysql", "postgresql"],
)
def test_polarrag_migration_renders_for_supported_databases(
    dialect,
    monkeypatch,
) -> None:
    migration = import_module(
        "server.db.migrations.versions."
        "e1f2a3b4c5d6_add_polarrag_mcp_catalog"
    )
    upgrade_sql = io.StringIO()
    upgrade_context = MigrationContext.configure(
        dialect=dialect,
        opts={"as_sql": True, "output_buffer": upgrade_sql},
    )
    monkeypatch.setattr(migration, "op", Operations(upgrade_context))

    migration.upgrade()

    rendered_upgrade = upgrade_sql.getvalue().lower()
    for table_name in {
        "polarrag_instances",
        "polarrag_spaces",
        "knowledge_resources",
        "enterprise_principal_assignments",
    }:
        assert f"create table {table_name}" in rendered_upgrade

    downgrade_sql = io.StringIO()
    downgrade_context = MigrationContext.configure(
        dialect=dialect,
        opts={"as_sql": True, "output_buffer": downgrade_sql},
    )
    monkeypatch.setattr(migration, "op", Operations(downgrade_context))

    migration.downgrade()

    rendered_downgrade = downgrade_sql.getvalue().lower()
    for table_name in {
        "polarrag_instances",
        "polarrag_spaces",
        "knowledge_resources",
        "enterprise_principal_assignments",
    }:
        assert f"drop table {table_name}" in rendered_downgrade


@pytest.mark.parametrize(
    "dialect",
    [sqlite.dialect(), mysql.dialect(), postgresql.dialect()],
    ids=["sqlite", "mysql", "postgresql"],
)
def test_polarrag_oss_upload_migration_renders_for_supported_databases(
    dialect,
    monkeypatch,
) -> None:
    migration = import_module(
        "server.db.migrations.versions."
        "b4c5d6e7f8a9_add_polarrag_space_oss_upload"
    )
    upgrade_sql = io.StringIO()
    upgrade_context = MigrationContext.configure(
        dialect=dialect,
        opts={"as_sql": True, "output_buffer": upgrade_sql},
    )
    monkeypatch.setattr(migration, "op", Operations(upgrade_context))

    migration.upgrade()

    rendered = upgrade_sql.getvalue().lower()
    assert "oss_bucket" in rendered
    assert "oss_access_key_id_ciphertext" in rendered
    assert "oss_access_key_secret_ciphertext" in rendered
