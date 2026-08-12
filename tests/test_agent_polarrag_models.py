from __future__ import annotations

import io
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

import server.models as models
from server.db.engine import enable_sqlite_foreign_keys
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentPolarRAGInstanceBinding,
    AgentUserAssignment,
    AgentUserToken,
    AuthProvider,
    Base,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    User,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    enable_sqlite_foreign_keys(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        admin = User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
        )
        member = User(
            external_id="member",
            display_name="Member",
            auth_provider=AuthProvider.BUILTIN,
        )
        agent = Agent(name="agent")
        database_session.add_all([admin, member, agent])
        await database_session.flush()
        instance = PolarRAGInstance(
            name="rag",
            scheme="http",
            host="rag.test",
            port=9200,
            username_ciphertext="u",
            password_ciphertext="p",
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        database_session.add(instance)
        await database_session.commit()
        yield database_session, admin, member, agent, instance
    await engine.dispose()


async def test_unique_agent_polarrag_binding(session) -> None:
    database, admin, _member, agent, instance = session
    for _ in range(2):
        database.add(
            AgentPolarRAGInstanceBinding(
                agent_id=agent.id,
                polarrag_instance_id=instance.id,
                created_by_user_id=admin.id,
            )
        )
    with pytest.raises(IntegrityError):
        await database.commit()


async def test_unique_agent_user_assignment(session) -> None:
    database, admin, member, agent, _instance = session
    for _ in range(2):
        database.add(
            AgentUserAssignment(
                agent_id=agent.id,
                user_id=member.id,
                created_by_user_id=admin.id,
            )
        )
    with pytest.raises(IntegrityError):
        await database.commit()


async def test_one_token_per_assignment(session) -> None:
    database, admin, member, agent, _instance = session
    assignment = AgentUserAssignment(
        agent_id=agent.id,
        user_id=member.id,
        created_by_user_id=admin.id,
    )
    database.add(assignment)
    await database.flush()
    database.add_all(
        [
            AgentUserToken(
                assignment_id=assignment.id,
                token_prefix=f"token-{index}",
                token_hash=str(index) * 64,
            )
            for index in (1, 2)
        ]
    )
    with pytest.raises(IntegrityError):
        await database.commit()


@pytest.mark.parametrize(
    "dialect",
    [sqlite.dialect(), mysql.dialect(), postgresql.dialect()],
    ids=["sqlite", "mysql", "postgresql"],
)
def test_agent_polarrag_ddl_compiles(dialect) -> None:
    for table_name in {
        "agent_polarrag_instance_bindings",
        "agent_user_assignments",
        "agent_user_tokens",
        "agent_group_assignments",
        "polarrag_upload_sessions",
    }:
        table = Base.metadata.tables[table_name]
        assert table_name in str(CreateTable(table).compile(dialect=dialect))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=dialect))


def test_polarrag_upload_session_binds_user_agent_resource_and_oss_upload() -> None:
    table = models.Base.metadata.tables["polarrag_upload_sessions"]

    assert {
        "pas_user_id",
        "agent_id",
        "knowledge_resource_id",
        "oss_bucket",
        "oss_endpoint",
        "oss_object_key",
        "oss_multipart_upload_id",
        "status",
        "expires_at",
        "doc_id",
    } <= set(table.c.keys())
    assert table.c.oss_multipart_upload_id.unique is True


@pytest.mark.parametrize(
    "dialect",
    [sqlite.dialect(), mysql.dialect(), postgresql.dialect()],
    ids=["sqlite", "mysql", "postgresql"],
)
def test_agent_polarrag_migration_renders(dialect, monkeypatch) -> None:
    migration = import_module(
        "server.db.migrations.versions."
        "f2a3b4c5d6e7_add_agent_polarrag_user_tokens"
    )
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect=dialect,
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))

    migration.upgrade()

    rendered = output.getvalue().lower()
    for table_name in {
        "agent_polarrag_instance_bindings",
        "agent_user_assignments",
        "agent_user_tokens",
    }:
        assert f"create table {table_name}" in rendered


@pytest.mark.parametrize(
    "dialect",
    [sqlite.dialect(), mysql.dialect(), postgresql.dialect()],
    ids=["sqlite", "mysql", "postgresql"],
)
def test_agent_group_migration_renders(dialect, monkeypatch) -> None:
    migration = import_module(
        "server.db.migrations.versions."
        "a3b4c5d6e7f8_add_agent_group_assignments"
    )
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect=dialect,
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))

    migration.upgrade()

    rendered = output.getvalue().lower()
    assert "create table agent_group_assignments" in rendered
    assert "alter table agent_user_assignments" in rendered
    assert AgentGroupAssignment.__table__.c.group_key.type.length == 64


@pytest.mark.parametrize(
    "dialect",
    [sqlite.dialect(), mysql.dialect(), postgresql.dialect()],
    ids=["sqlite", "mysql", "postgresql"],
)
def test_polarrag_upload_session_migration_renders(dialect, monkeypatch) -> None:
    migration = import_module(
        "server.db.migrations.versions."
        "c5d6e7f8a9b0_add_polarrag_upload_sessions"
    )
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect=dialect,
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))

    migration.upgrade()

    rendered = output.getvalue().lower()
    assert "create table polarrag_upload_sessions" in rendered
    assert "oss_multipart_upload_id" in rendered
