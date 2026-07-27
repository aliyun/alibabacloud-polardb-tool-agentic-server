import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.config import reset_config
from server.core.audit_logger import log_audit
from server.logging import trace_id_var
from server.models import Base, User, AuditStatus, AuthProvider


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    yield
    reset_config()


@pytest.fixture
async def engine():
    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield e
    await e.dispose()


@pytest.fixture
async def session(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
async def sample_user(session: AsyncSession) -> User:
    user = User(external_id="audit-user", display_name="Audit User", auth_provider=AuthProvider.BUILTIN)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


class TestAuditLogger:
    async def test_log_success(self, session, sample_user):
        entry = await log_audit(
            session,
            user_id=sample_user.id,
            action="run_sql",
            sql_text="SELECT 1",
            status=AuditStatus.SUCCESS,
            duration_ms=42,
            row_count=1,
        )
        assert entry is not None
        assert entry.status == AuditStatus.SUCCESS

    async def test_log_blocked(self, session, sample_user):
        entry = await log_audit(
            session,
            user_id=sample_user.id,
            action="run_sql",
            sql_text="DROP TABLE users",
            status=AuditStatus.BLOCKED,
            error_message="Blocked by security policy",
        )
        assert entry.status == AuditStatus.BLOCKED

    async def test_log_error(self, session, sample_user):
        entry = await log_audit(
            session,
            user_id=sample_user.id,
            action="run_sql",
            sql_text="SELECT * FROM nonexistent",
            status=AuditStatus.ERROR,
            error_message="Table not found",
            duration_ms=5,
        )
        assert entry.status == AuditStatus.ERROR

    async def test_log_with_traceability_fields(self, session, sample_user):
        entry = await log_audit(
            session,
            user_id=sample_user.id,
            action="run_sql",
            sql_text="SELECT * FROM users",
            status=AuditStatus.SUCCESS,
            duration_ms=42,
            row_count=5,
            user_name="Test User",
            instance_name="prod-db",
            db_name="myapp",
        )
        assert entry is not None
        metadata = json.loads(entry.metadata_json)
        assert metadata["sql_type"] == "SELECT"
        assert metadata["user_name"] == "Test User"
        assert metadata["instance_name"] == "prod-db"
        assert metadata["db_name"] == "myapp"

    async def test_sql_type_auto_classified(self, session, sample_user):
        entry = await log_audit(
            session,
            user_id=sample_user.id,
            action="run_sql",
            sql_text="-- comment\nDELETE FROM orders",
            status=AuditStatus.SUCCESS,
        )
        assert json.loads(entry.metadata_json)["sql_type"] == "DELETE"

    async def test_sql_type_none_when_no_sql(self, session, sample_user):
        entry = await log_audit(
            session,
            user_id=sample_user.id,
            action="list_instances",
            status=AuditStatus.SUCCESS,
        )
        assert json.loads(entry.metadata_json)["sql_type"] == "OTHER"

    async def test_trace_id_and_stable_error_code_are_persisted(
        self, session, sample_user
    ):
        token = trace_id_var.set("trace-audit-test")
        try:
            entry = await log_audit(
                session,
                user_id=sample_user.id,
                action="db_instance.describe",
                target_type="db_instance",
                target_id="resource-1",
                status=AuditStatus.ERROR,
                error_code="DB_INSTANCE_NOT_FOUND",
            )
        finally:
            trace_id_var.reset(token)

        assert entry is not None
        assert entry.request_id == "trace-audit-test"
        assert entry.error_code == "DB_INSTANCE_NOT_FOUND"

    @pytest.mark.parametrize(
        "invalid_request_id",
        ["x" * 129, "contains spaces", "包含非ASCII"],
    )
    async def test_invalid_explicit_request_id_is_replaced(
        self, session, sample_user, invalid_request_id
    ):
        entry = await log_audit(
            session,
            user_id=sample_user.id,
            action="db_instance.create",
            target_type="db_instance",
            status=AuditStatus.SUCCESS,
            request_id=invalid_request_id,
        )
        assert entry is not None
        assert entry.request_id != invalid_request_id
        assert entry.request_id is not None
        assert len(entry.request_id) <= 128
        assert entry.request_id.isascii()
