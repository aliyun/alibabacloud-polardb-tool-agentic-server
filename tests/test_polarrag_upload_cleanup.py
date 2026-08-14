from __future__ import annotations

import asyncio
import base64
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core.crypto import encrypt
from server.db.engine import enable_sqlite_foreign_keys
from server.models import (
    Agent,
    AuthProvider,
    Base,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    PolarRAGUploadCleanup,
    PolarRAGUploadSession,
    PolarRAGUploadStatus,
    User,
)
from server.polarrag.access import KnowledgeAccessPlan
from server.polarrag.contracts import PolarRAGErrorCode, PolarRAGUpstreamError
from server.polarrag.mcp_upload import (
    UploadSessionError,
    abort_upload,
    complete_upload,
)
from server.polarrag.upload_cleanup import sweep_expired_uploads


class FakeStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.aborted: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        self.aborted.append((key, upload_id))
        if self.fail:
            raise RuntimeError("sensitive OSS failure")

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        if self.fail:
            raise RuntimeError("sensitive OSS failure")


@pytest.fixture
async def seeded(tmp_path, monkeypatch):
    key = os.urandom(32)
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(key).decode(),
    )
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'upload-cleanup.db'}"
    )
    enable_sqlite_foreign_keys(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(
            external_id="cleanup-user",
            display_name="Cleanup User",
            auth_provider=AuthProvider.BUILTIN,
        )
        agent = Agent(name="cleanup-agent")
        session.add_all([user, agent])
        await session.flush()
        instance = PolarRAGInstance(
            name="cleanup-rag",
            scheme="https",
            host="rag.example.test",
            port=9200,
            username_ciphertext="encrypted-user",
            password_ciphertext="encrypted-password",
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=user.id,
        )
        session.add(instance)
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id,
            space_id="space-a",
            name="Space A",
            identity_domain="tenant-a",
            oss_bucket="documents",
            oss_endpoint="https://oss.example.test",
            oss_access_key_id_ciphertext=encrypt("old-ak", key=key),
            oss_access_key_secret_ciphertext=encrypt("old-sk", key=key),
            oss_object_prefix="pas/documents",
            oss_config_validated=True,
            enabled=True,
        )
        session.add(space)
        await session.flush()
        resource = KnowledgeResource(
            knowledge_space_id=space.knowledge_space_id,
            polarrag_instance_id=instance.id,
            space_id=space.space_id,
            kb_id="kb-a",
            name="KB A",
            kb_type="PUBLIC",
            identity_domain=space.identity_domain,
            binding_mode=KnowledgeBindingMode.DOMAIN,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE,
            enabled=True,
        )
        session.add(resource)
        await session.commit()
        yield factory, user, agent, space, resource
    await engine.dispose()


async def _add_cleanup(
    factory,
    user: User,
    agent: Agent,
    space: PolarRAGSpace,
    resource: KnowledgeResource,
    *,
    status: PolarRAGUploadStatus,
    object_state: str,
    expires_at: datetime,
) -> str:
    async with factory() as session:
        upload = PolarRAGUploadSession(
            pas_user_id=user.id,
            agent_id=agent.id,
            knowledge_resource_id=resource.id,
            filename="guide.md",
            file_type="md",
            file_size_bytes=1,
            file_md5="a" * 32,
            file_sha256="b" * 64,
            oss_bucket="documents",
            oss_endpoint="https://oss.example.test",
            oss_object_key="pas/documents/object/guide.md",
            oss_multipart_upload_id="upload-id",
            part_size_bytes=1,
            part_count=1,
            status=status,
            expires_at=expires_at,
        )
        session.add(upload)
        await session.flush()
        session.add(
            PolarRAGUploadCleanup(
                upload_session_id=upload.id,
                knowledge_space_id=space.knowledge_space_id,
                oss_bucket=upload.oss_bucket,
                oss_endpoint=upload.oss_endpoint,
                oss_object_key=upload.oss_object_key,
                oss_multipart_upload_id=upload.oss_multipart_upload_id,
                oss_access_key_id_ciphertext=(
                    space.oss_access_key_id_ciphertext
                ),
                oss_access_key_secret_ciphertext=(
                    space.oss_access_key_secret_ciphertext
                ),
                object_state=object_state,
                expires_at=expires_at,
            )
        )
        await session.commit()
        return upload.id


def _allow_access(
    monkeypatch,
    user: User,
    space: PolarRAGSpace,
    resource: KnowledgeResource,
) -> None:
    from server.polarrag import mcp_upload

    plan = KnowledgeAccessPlan(
        resources=[resource],
        instance=object(),
        space=space,
        acl_context={
            "identity_domain": space.identity_domain,
            "principals": [
                {
                    "provider": "polarrag",
                    "type": "user",
                    "id": user.id,
                }
            ],
        },
        partial_failures=[],
    )

    async def access(*_args, **_kwargs):
        return plan

    monkeypatch.setattr(mcp_upload, "_session_access", access)


@pytest.mark.parametrize(
    ("status", "object_state", "expected_abort", "expected_delete"),
    [
        (PolarRAGUploadStatus.PREPARED, "multipart", 1, 0),
        (PolarRAGUploadStatus.UPLOADED, "object", 0, 1),
        (PolarRAGUploadStatus.PREPARED, "finalizing", 1, 1),
    ],
)
async def test_sweeper_cleans_expired_state_and_terminates_session(
    seeded,
    status,
    object_state,
    expected_abort,
    expected_delete,
) -> None:
    factory, user, agent, space, resource = seeded
    now = datetime.now(UTC)
    upload_id = await _add_cleanup(
        factory,
        user,
        agent,
        space,
        resource,
        status=status,
        object_state=object_state,
        expires_at=now - timedelta(seconds=1),
    )
    store = FakeStore()

    result = await sweep_expired_uploads(
        factory,
        now=now,
        object_store_factory=lambda _space, _cleanup: store,
    )

    assert result == {"cleaned": 1, "failed": 0}
    assert len(store.aborted) == expected_abort
    assert len(store.deleted) == expected_delete
    async with factory() as session:
        upload = await session.get(PolarRAGUploadSession, upload_id)
        assert upload is not None
        assert upload.status == PolarRAGUploadStatus.ABORTED
        assert await session.get(PolarRAGUploadCleanup, upload_id) is None


async def test_sweeper_retains_failed_cleanup_for_bounded_retry(seeded) -> None:
    factory, user, agent, space, resource = seeded
    now = datetime.now(UTC)
    upload_id = await _add_cleanup(
        factory,
        user,
        agent,
        space,
        resource,
        status=PolarRAGUploadStatus.UPLOADED,
        object_state="object",
        expires_at=now - timedelta(seconds=1),
    )
    failing = FakeStore(fail=True)

    first = await sweep_expired_uploads(
        factory,
        now=now,
        object_store_factory=lambda _space, _cleanup: failing,
    )
    immediate = await sweep_expired_uploads(
        factory,
        now=now,
        object_store_factory=lambda _space, _cleanup: FakeStore(),
    )

    assert first == {"cleaned": 0, "failed": 1}
    assert immediate == {"cleaned": 0, "failed": 0}
    async with factory() as session:
        cleanup = await session.get(PolarRAGUploadCleanup, upload_id)
        assert cleanup is not None
        assert cleanup.cleanup_attempts == 1
        assert cleanup.cleanup_error_code == "OSS_CLEANUP_FAILED"
        assert cleanup.cleanup_after is not None

    recovered = FakeStore()
    retried = await sweep_expired_uploads(
        factory,
        now=now + timedelta(minutes=2),
        object_store_factory=lambda _space, _cleanup: recovered,
    )
    assert retried == {"cleaned": 1, "failed": 0}
    assert recovered.deleted == ["pas/documents/object/guide.md"]


async def test_cleanup_journal_survives_agent_cascade(seeded) -> None:
    factory, user, agent, space, resource = seeded
    upload_id = await _add_cleanup(
        factory,
        user,
        agent,
        space,
        resource,
        status=PolarRAGUploadStatus.PREPARED,
        object_state="multipart",
        expires_at=datetime.now(UTC),
    )
    async with factory() as session:
        stored_agent = await session.get(Agent, agent.id)
        assert stored_agent is not None
        await session.delete(stored_agent)
        await session.commit()

    async with factory() as session:
        assert await session.get(PolarRAGUploadSession, upload_id) is None
        assert await session.get(PolarRAGUploadCleanup, upload_id) is not None


async def test_sweeper_uses_creation_credentials_after_space_rotation(
    seeded,
    monkeypatch,
) -> None:
    from server.polarrag import upload_cleanup

    factory, user, agent, space, resource = seeded
    now = datetime.now(UTC)
    upload_id = await _add_cleanup(
        factory,
        user,
        agent,
        space,
        resource,
        status=PolarRAGUploadStatus.UPLOADED,
        object_state="object",
        expires_at=now - timedelta(seconds=1),
    )
    captured: dict[str, str] = {}

    class CapturingStore(FakeStore):
        def __init__(
            self,
            *,
            endpoint: str,
            bucket: str,
            access_key_id: str,
            access_key_secret: str,
        ) -> None:
            super().__init__()
            captured.update(
                endpoint=endpoint,
                bucket=bucket,
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
            )

    monkeypatch.setattr(upload_cleanup, "OssObjectStore", CapturingStore)
    async with factory() as session:
        stored_space = await session.get(
            PolarRAGSpace, space.knowledge_space_id
        )
        assert stored_space is not None
        stored_space.oss_bucket = "new-documents"
        stored_space.oss_endpoint = "https://new-oss.example.test"
        stored_space.oss_access_key_id_ciphertext = encrypt("new-ak")
        stored_space.oss_access_key_secret_ciphertext = encrypt("new-sk")
        await session.commit()

    result = await sweep_expired_uploads(factory, now=now)

    assert result == {"cleaned": 1, "failed": 0}
    assert captured == {
        "endpoint": "https://oss.example.test",
        "bucket": "documents",
        "access_key_id": "old-ak",
        "access_key_secret": "old-sk",
    }
    async with factory() as session:
        assert await session.get(PolarRAGUploadCleanup, upload_id) is None


async def test_submit_does_not_block_unrelated_sqlite_write(
    seeded,
    monkeypatch,
) -> None:
    factory, user, agent, space, resource = seeded
    now = datetime.now(UTC)
    upload_id = await _add_cleanup(
        factory,
        user,
        agent,
        space,
        resource,
        status=PolarRAGUploadStatus.UPLOADED,
        object_state="object",
        expires_at=now - timedelta(seconds=1),
    )
    _allow_access(monkeypatch, user, space, resource)
    submit_entered = asyncio.Event()
    release_submit = asyncio.Event()

    class BlockingClient:
        async def submit_document(self, *_args, **_kwargs):
            submit_entered.set()
            await release_submit.wait()
            return {"doc_id": "doc-from-polarrag", "status": "DISPATCHED"}

    async def run_complete():
        async with factory() as session:
            result = await complete_upload(
                session,
                user,
                agent_id=agent.id,
                upload_session_id=upload_id,
                resource_scope=None,
                object_store_factory=lambda _space: FakeStore(),
                client_factory=lambda _instance: BlockingClient(),
            )
            await session.commit()
            return result

    complete_task = asyncio.create_task(run_complete())
    await submit_entered.wait()
    try:
        async with factory() as unrelated_session:
            await unrelated_session.execute(text("PRAGMA busy_timeout=0"))
            unrelated_session.add(Agent(name="unrelated-write"))
            await unrelated_session.commit()
        cleanup_store = FakeStore()
        sweep_result = await sweep_expired_uploads(
            factory,
            now=now,
            object_store_factory=lambda _space, _cleanup: cleanup_store,
        )
        assert sweep_result == {"cleaned": 0, "failed": 0}
        assert cleanup_store.deleted == []
    finally:
        release_submit.set()
        await complete_task

    async with factory() as session:
        upload = await session.get(PolarRAGUploadSession, upload_id)
        assert upload is not None
        assert upload.status == PolarRAGUploadStatus.COMPLETED
        assert await session.get(PolarRAGUploadCleanup, upload_id) is None


async def test_sweeper_preserves_object_after_expired_submit_claim(
    seeded,
) -> None:
    from server.polarrag import upload_cleanup

    factory, user, agent, space, resource = seeded
    now = datetime.now(UTC)
    upload_id = await _add_cleanup(
        factory,
        user,
        agent,
        space,
        resource,
        status=PolarRAGUploadStatus.UPLOADED,
        object_state="object",
        expires_at=now - timedelta(hours=1),
    )
    async with factory() as session:
        cleanup = await session.get(PolarRAGUploadCleanup, upload_id)
        assert cleanup is not None
        cleanup.operation_kind = "submitting"
        cleanup.operation_token = "abandoned-submit-owner"
        cleanup.cleanup_lease_until = now - timedelta(seconds=1)
        await session.commit()
    store = FakeStore()

    result = await sweep_expired_uploads(
        factory,
        now=now,
        object_store_factory=lambda _space, _cleanup: store,
    )

    assert result == {"cleaned": 0, "failed": 0}
    assert store.deleted == []
    async with factory() as session:
        cleanup = await session.get(PolarRAGUploadCleanup, upload_id)
        assert cleanup is not None
        assert cleanup.operation_kind == "submitting"
        assert cleanup.operation_token == "abandoned-submit-owner"
        abort_claim = await upload_cleanup._claim_cleanup(
            session,
            upload_id,
            now=now,
            require_due=False,
            operation="aborting",
        )
        assert abort_claim is None


async def test_complete_recovers_expired_submit_claim(
    seeded,
    monkeypatch,
) -> None:
    factory, user, agent, space, resource = seeded
    now = datetime.now(UTC)
    upload_id = await _add_cleanup(
        factory,
        user,
        agent,
        space,
        resource,
        status=PolarRAGUploadStatus.UPLOADED,
        object_state="object",
        expires_at=now - timedelta(hours=1),
    )
    async with factory() as session:
        cleanup = await session.get(PolarRAGUploadCleanup, upload_id)
        assert cleanup is not None
        cleanup.operation_kind = "submitting"
        cleanup.operation_token = "crashed-process"
        cleanup.cleanup_lease_until = now - timedelta(seconds=1)
        await session.commit()
    _allow_access(monkeypatch, user, space, resource)

    class RecoveryClient:
        async def submit_document(self, *_args, **_kwargs):
            return {"doc_id": "stable-upstream-doc", "status": "DISPATCHED"}

    async with factory() as session:
        _, result = await complete_upload(
            session,
            user,
            agent_id=agent.id,
            upload_session_id=upload_id,
            resource_scope=None,
            object_store_factory=lambda _space: FakeStore(),
            client_factory=lambda _instance: RecoveryClient(),
        )
        await session.commit()

    assert result["doc_id"] == "stable-upstream-doc"
    async with factory() as session:
        upload = await session.get(PolarRAGUploadSession, upload_id)
        assert upload is not None
        assert upload.status == PolarRAGUploadStatus.COMPLETED
        assert await session.get(PolarRAGUploadCleanup, upload_id) is None


async def test_stale_owner_cannot_release_reclaimed_operation(seeded) -> None:
    from server.polarrag import upload_cleanup

    factory, user, agent, space, resource = seeded
    now = datetime.now(UTC)
    upload_id = await _add_cleanup(
        factory,
        user,
        agent,
        space,
        resource,
        status=PolarRAGUploadStatus.UPLOADED,
        object_state="object",
        expires_at=now,
    )
    async with factory() as session:
        first = await upload_cleanup._claim_cleanup(
            session,
            upload_id,
            now=now,
            require_due=False,
            operation="submitting",
            polarrag_instance_id=resource.polarrag_instance_id,
            submission_payload_ciphertext=encrypt("{}"),
        )
        assert first is not None
        second = await upload_cleanup._claim_cleanup(
            session,
            upload_id,
            now=now + upload_cleanup.CLEANUP_LEASE + timedelta(seconds=1),
            require_due=False,
            operation="submitting",
            polarrag_instance_id=resource.polarrag_instance_id,
            submission_payload_ciphertext=encrypt("{}"),
        )
        assert second is not None
        assert second.token != first.token

        assert await upload_cleanup._release_cleanup(session, first) is False
        cleanup = await session.get(PolarRAGUploadCleanup, upload_id)
        assert cleanup is not None
        assert cleanup.operation_token == second.token
        assert await upload_cleanup._release_cleanup(session, second) is True


async def test_definitive_submit_rejection_can_be_aborted(
    seeded,
    monkeypatch,
) -> None:
    factory, user, agent, space, resource = seeded
    upload_id = await _add_cleanup(
        factory,
        user,
        agent,
        space,
        resource,
        status=PolarRAGUploadStatus.UPLOADED,
        object_state="object",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    _allow_access(monkeypatch, user, space, resource)

    class RejectedClient:
        async def submit_document(self, *_args, **_kwargs):
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.INVALID_RESPONSE,
                status_code=400,
            )

    async with factory() as session:
        with pytest.raises(UploadSessionError):
            await complete_upload(
                session,
                user,
                agent_id=agent.id,
                upload_session_id=upload_id,
                resource_scope=None,
                object_store_factory=lambda _space: FakeStore(),
                client_factory=lambda _instance: RejectedClient(),
            )
        cleanup = await session.get(PolarRAGUploadCleanup, upload_id)
        assert cleanup is not None
        assert cleanup.operation_kind is None
        assert cleanup.reconcile_required is False

        store = FakeStore()
        _, result = await abort_upload(
            session,
            user,
            agent_id=agent.id,
            upload_session_id=upload_id,
            object_store_factory=lambda _space: store,
        )
        await session.commit()

    assert result == {"status": PolarRAGUploadStatus.ABORTED.value}
    assert store.deleted == ["pas/documents/object/guide.md"]
    async with factory() as session:
        assert await session.get(PolarRAGUploadCleanup, upload_id) is None


@pytest.mark.parametrize("rejected", [False, True])
async def test_orphaned_unknown_submit_is_reconciled(
    seeded,
    monkeypatch,
    rejected: bool,
) -> None:
    from server.polarrag import upload_cleanup

    factory, user, agent, space, resource = seeded
    upload_id = await _add_cleanup(
        factory,
        user,
        agent,
        space,
        resource,
        status=PolarRAGUploadStatus.UPLOADED,
        object_state="object",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    _allow_access(monkeypatch, user, space, resource)

    class InterruptedClient:
        async def submit_document(self, *_args, **_kwargs):
            raise asyncio.CancelledError

    async with factory() as session:
        with pytest.raises(asyncio.CancelledError):
            await complete_upload(
                session,
                user,
                agent_id=agent.id,
                upload_session_id=upload_id,
                resource_scope=None,
                object_store_factory=lambda _space: FakeStore(),
                client_factory=lambda _instance: InterruptedClient(),
            )

    async with factory() as session:
        cleanup = await session.get(PolarRAGUploadCleanup, upload_id)
        assert cleanup is not None
        assert cleanup.operation_kind == "submitting"
        assert cleanup.submission_payload_ciphertext is not None
        assert user.id not in cleanup.submission_payload_ciphertext
        assert "oss://" not in cleanup.submission_payload_ciphertext

    async with factory() as session:
        stored_agent = await session.get(Agent, agent.id)
        assert stored_agent is not None
        await session.delete(stored_agent)
        await session.commit()
        assert await session.get(PolarRAGUploadSession, upload_id) is None

    class RecoveryClient:
        calls = 0
        reject = rejected

        async def submit_document(self, *_args, **_kwargs):
            self.calls += 1
            if self.reject:
                raise PolarRAGUpstreamError(
                    PolarRAGErrorCode.INVALID_RESPONSE,
                    status_code=409,
                )
            return {"doc_id": "reconciled-doc", "status": "DISPATCHED"}

    recovery = RecoveryClient()
    monkeypatch.setattr(
        upload_cleanup,
        "client_from_instance",
        lambda _instance: recovery,
        raising=False,
    )
    store = FakeStore()
    result = await sweep_expired_uploads(
        factory,
        now=datetime.now(UTC) + timedelta(minutes=6),
        object_store_factory=lambda _space, _cleanup: store,
    )

    assert result == (
        {"cleaned": 0, "failed": 1}
        if rejected
        else {"cleaned": 1, "failed": 0}
    )
    assert recovery.calls == 1
    assert store.deleted == []
    async with factory() as session:
        cleanup = await session.get(PolarRAGUploadCleanup, upload_id)
        assert (cleanup is not None) is rejected
        if cleanup is not None:
            assert cleanup.reconcile_required is True
            assert cleanup.cleanup_error_code == "POLARRAG_RECONCILE_FAILED"

    if rejected:
        recovery.reject = False
        recovered = await sweep_expired_uploads(
            factory,
            now=datetime.now(UTC) + timedelta(minutes=8),
            object_store_factory=lambda _space, _cleanup: store,
        )
        assert recovered == {"cleaned": 1, "failed": 0}
        assert recovery.calls == 2
        assert store.deleted == []
        async with factory() as session:
            assert (
                await session.get(PolarRAGUploadCleanup, upload_id) is None
            )
