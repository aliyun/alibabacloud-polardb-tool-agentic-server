from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.configuration.repository import ConfigRepository
from server.core.config_crypto import ConfigCrypto
from server.models import Base, ConfigOperationReceipt, SystemConfig


ROOT_KEY = b"01234567890123456789012345678901"


@pytest.fixture
async def coordinator(tmp_path):
    from server.configuration.idempotency import ManagedCommandIdempotency

    database = tmp_path / "managed-idempotency.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ConfigRepository(factory)
    await repository.ensure_setup_status()
    value = ManagedCommandIdempotency(repository, ConfigCrypto(ROOT_KEY))
    yield value
    await engine.dispose()


def _request(*, username: str = "admin") -> dict[str, object]:
    return {
        "protocol_version": 1,
        "target": {"instance_id": "pmcp-a", "generation": 1},
        "command": {
            "module": "core_admin",
            "action": "save_draft",
            "parameter": "username",
            "value": username,
        },
    }


async def test_claim_persists_in_progress_lease_without_plaintext(
    coordinator,
) -> None:
    claim = await coordinator.claim(
        actor_scope="managed_initializer",
        idempotency_key="stable-operation-key",
        request=_request(username="sensitive-admin"),
        action="save_draft",
        module="core_admin",
        instance_id="pmcp-a",
        instance_generation=1,
        lease_owner="worker-a",
    )

    receipt = await coordinator.repository.get_receipt(
        actor_scope="managed_initializer",
        idempotency_key_hash=claim.idempotency_key_hash,
    )
    assert claim.status == "CLAIMED"
    assert receipt is not None
    assert receipt.status == "IN_PROGRESS"
    assert receipt.lease_owner == claim.lease_token
    assert receipt.lease_owner != "worker-a"
    assert receipt.lease_expires_at is not None
    stored = " ".join(
        [
            receipt.idempotency_key_hash,
            receipt.request_digest,
            receipt.response_json,
        ]
    )
    assert "stable-operation-key" not in stored
    assert "sensitive-admin" not in stored
    assert receipt.request_digest != hashlib.sha256(
        b"sensitive-admin"
    ).hexdigest()


async def test_password_envelope_receipt_uses_keyed_digest_only(
    coordinator,
) -> None:
    old_password = "old-managed-password"
    new_password = "new-managed-password"
    request = {
        "protocol_version": 1,
        "target": {"instance_id": "pmcp-a", "generation": 1},
        "actor": {"type": "control_user", "subject_id": "opaque-subject"},
        "operation": "MODIFY",
        "old_password": old_password,
        "new_password": new_password,
        "idempotency_key": "password-request-id",
    }

    claim = await coordinator.claim(
        actor_scope="control:pmcp-a:1",
        idempotency_key="password-request-id",
        request=request,
        action="account.password.modify",
        module=None,
        instance_id="pmcp-a",
        instance_generation=1,
        lease_owner="node-a",
    )

    receipt = await coordinator.repository.get_receipt(
        actor_scope="control:pmcp-a:1",
        idempotency_key_hash=claim.idempotency_key_hash,
    )
    assert receipt is not None
    stored = " ".join(
        (
            receipt.idempotency_key_hash,
            receipt.request_digest,
            receipt.response_json,
        )
    )
    assert old_password not in stored
    assert new_password not in stored
    assert str(request) not in stored
    assert receipt.request_digest != hashlib.sha256(
        new_password.encode()
    ).hexdigest()


async def test_same_key_other_request_conflicts(coordinator) -> None:
    from server.configuration.idempotency import ManagedIdempotencyError

    arguments = {
        "actor_scope": "managed_initializer",
        "idempotency_key": "same-key",
        "action": "save_draft",
        "module": "core_admin",
        "instance_id": "pmcp-a",
        "instance_generation": 1,
        "lease_owner": "worker-a",
    }
    await coordinator.claim(request=_request(), **arguments)

    with pytest.raises(ManagedIdempotencyError) as captured:
        await coordinator.claim(
            request=_request(username="different"),
            **arguments,
        )

    assert captured.value.code == "IDEMPOTENCY_CONFLICT"


async def test_unexpired_lease_reports_operation_in_progress(
    coordinator,
) -> None:
    from server.configuration.idempotency import ManagedIdempotencyError

    arguments = {
        "actor_scope": "managed_initializer",
        "idempotency_key": "same-key",
        "request": _request(),
        "action": "save_draft",
        "module": "core_admin",
        "instance_id": "pmcp-a",
        "instance_generation": 1,
    }
    await coordinator.claim(lease_owner="worker-a", **arguments)

    with pytest.raises(ManagedIdempotencyError) as captured:
        await coordinator.claim(lease_owner="worker-b", **arguments)

    assert captured.value.code == "OPERATION_IN_PROGRESS"
    assert captured.value.retry_after_seconds > 0


async def test_expired_lease_can_be_reclaimed(coordinator) -> None:
    now = datetime.now(timezone.utc)
    arguments = {
        "actor_scope": "managed_initializer",
        "idempotency_key": "reclaim-key",
        "request": _request(),
        "action": "save_draft",
        "module": "core_admin",
        "instance_id": "pmcp-a",
        "instance_generation": 1,
    }
    await coordinator.claim(
        lease_owner="worker-a",
        now=now,
        lease_duration=timedelta(seconds=1),
        **arguments,
    )

    reclaimed = await coordinator.claim(
        lease_owner="worker-b",
        now=now + timedelta(seconds=2),
        **arguments,
    )

    assert reclaimed.status == "CLAIMED"
    receipt = await coordinator.repository.get_receipt(
        actor_scope="managed_initializer",
        idempotency_key_hash=reclaimed.idempotency_key_hash,
    )
    assert receipt is not None
    assert receipt.lease_owner == reclaimed.lease_token
    assert receipt.lease_owner != "worker-b"


async def test_expired_same_owner_claim_is_fenced_from_reclaimed_completion(
    coordinator,
) -> None:
    from server.configuration.idempotency import ManagedIdempotencyError

    now = datetime.now(timezone.utc)
    arguments = {
        "actor_scope": "managed_initializer",
        "idempotency_key": "same-owner-reclaim-key",
        "request": _request(),
        "action": "save_draft",
        "module": "core_admin",
        "instance_id": "pmcp-a",
        "instance_generation": 1,
        "lease_owner": "worker-a",
    }
    stale = await coordinator.claim(
        now=now,
        lease_duration=timedelta(seconds=1),
        **arguments,
    )
    reclaimed = await coordinator.claim(
        now=now + timedelta(seconds=2),
        **arguments,
    )
    assert stale.lease_token != reclaimed.lease_token
    calls = 0

    async def mutate(_session):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"status": "ok"}

    outcomes = await asyncio.gather(
        coordinator.complete(
            stale,
            lease_owner="worker-a",
            mutation=mutate,
        ),
        coordinator.complete(
            reclaimed,
            lease_owner="worker-a",
            mutation=mutate,
        ),
        return_exceptions=True,
    )

    assert calls == 1
    assert sum(item == {"status": "ok"} for item in outcomes) == 1
    assert [
        item.code
        for item in outcomes
        if isinstance(item, ManagedIdempotencyError)
    ] == ["OPERATION_IN_PROGRESS"]


async def test_concurrent_completers_reload_after_sqlite_write_lock(
    coordinator,
) -> None:
    from server.configuration.idempotency import ManagedIdempotencyError

    claim = await coordinator.claim(
        actor_scope="managed_initializer",
        idempotency_key="duplicate-completion-key",
        request=_request(),
        action="save_draft",
        module="core_admin",
        instance_id="pmcp-a",
        instance_generation=1,
        lease_owner="worker-a",
    )
    calls = 0

    async def mutate(_session):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"status": "ok"}

    outcomes = await asyncio.gather(
        coordinator.complete(
            claim,
            lease_owner="worker-a",
            mutation=mutate,
        ),
        coordinator.complete(
            claim,
            lease_owner="worker-a",
            mutation=mutate,
        ),
        return_exceptions=True,
    )

    assert calls == 1
    assert sum(item == {"status": "ok"} for item in outcomes) == 1
    assert [
        item.code
        for item in outcomes
        if isinstance(item, ManagedIdempotencyError)
    ] == ["OPERATION_IN_PROGRESS"]


async def test_mutation_and_success_receipt_commit_atomically(
    coordinator,
) -> None:
    claim = await coordinator.claim(
        actor_scope="managed_initializer",
        idempotency_key="atomic-key",
        request=_request(),
        action="save_draft",
        module="core_admin",
        instance_id="pmcp-a",
        instance_generation=1,
        lease_owner="worker-a",
    )

    async def mutate(session):
        session.add(
            SystemConfig(
                config_key="test.atomic",
                config_value='{"written":true}',
                config_version=2,
            )
        )
        return {"status": "ok", "revision": 1}

    result = await coordinator.complete(
        claim,
        lease_owner="worker-a",
        mutation=mutate,
    )

    assert result == {"status": "ok", "revision": 1}
    async with coordinator.repository.session_factory() as session:
        assert await session.get(SystemConfig, "test.atomic") is not None
        receipt = await session.scalar(
            select(ConfigOperationReceipt).where(
                ConfigOperationReceipt.id == claim.receipt_id
            )
        )
        assert receipt is not None
        assert receipt.status == "SUCCEEDED"
        assert receipt.lease_owner is None


async def test_failed_transaction_has_no_side_effect_or_success(
    coordinator,
) -> None:
    claim = await coordinator.claim(
        actor_scope="managed_initializer",
        idempotency_key="failed-key",
        request=_request(),
        action="save_draft",
        module="core_admin",
        instance_id="pmcp-a",
        instance_generation=1,
        lease_owner="worker-a",
    )

    async def mutate(session):
        session.add(
            SystemConfig(
                config_key="test.must_rollback",
                config_value="{}",
                config_version=2,
            )
        )
        raise RuntimeError("simulated failure")

    with pytest.raises(RuntimeError, match="simulated failure"):
        await coordinator.complete(
            claim,
            lease_owner="worker-a",
            mutation=mutate,
        )

    async with coordinator.repository.session_factory() as session:
        assert await session.get(SystemConfig, "test.must_rollback") is None
        receipt = await session.get(ConfigOperationReceipt, claim.receipt_id)
        assert receipt is not None
        assert receipt.status == "IN_PROGRESS"


async def test_timeout_after_commit_replays_without_second_mutation(
    coordinator,
) -> None:
    calls = 0
    arguments = {
        "actor_scope": "managed_initializer",
        "idempotency_key": "timeout-key",
        "request": _request(),
        "action": "save_draft",
        "module": "core_admin",
        "instance_id": "pmcp-a",
        "instance_generation": 1,
    }
    claim = await coordinator.claim(lease_owner="worker-a", **arguments)

    async def mutate(_session):
        nonlocal calls
        calls += 1
        return {"status": "ok"}

    await coordinator.complete(
        claim,
        lease_owner="worker-a",
        mutation=mutate,
    )
    replay = await coordinator.claim(lease_owner="worker-b", **arguments)

    assert replay.status == "REPLAY"
    assert replay.response == {"status": "ok"}
    assert calls == 1


async def test_concurrent_claim_has_one_owner(coordinator) -> None:
    from server.configuration.idempotency import ManagedIdempotencyError

    arguments = {
        "actor_scope": "managed_initializer",
        "idempotency_key": "concurrent-key",
        "request": _request(),
        "action": "save_draft",
        "module": "core_admin",
        "instance_id": "pmcp-a",
        "instance_generation": 1,
    }

    async def claim(owner: str):
        try:
            return await coordinator.claim(lease_owner=owner, **arguments)
        except ManagedIdempotencyError as error:
            return error

    results = await asyncio.gather(claim("worker-a"), claim("worker-b"))

    assert sum(getattr(item, "status", None) == "CLAIMED" for item in results) == 1
    assert sum(
        getattr(item, "code", None) == "OPERATION_IN_PROGRESS"
        for item in results
    ) == 1
