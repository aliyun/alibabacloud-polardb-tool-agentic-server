from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import TenantProvisioningConfig
from server.core.dedicated_mysql import DedicatedGrantVerificationError
from server.core.dedicated_pool_worker import DedicatedPoolWorker
from server.core.dedicated_purchase_profile import (
    PROFILE_ID,
    PROFILE_REVISION,
    build_purchase_config,
)
from server.models import (
    AllocationMode,
    Base,
    DedicatedMemberStatus,
    DedicatedPool,
    DedicatedPoolMember,
    DedicatedPreparationStep,
    DedicatedWorkerHeartbeat,
    Instance,
    InstanceStatus,
    InstanceTopology,
    PermissionTemplate,
    PermissionTemplateRevision,
    ReadinessStatus,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
async def worker_context(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/pool-worker.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = MutableClock()
    async with factory() as session:
        template = PermissionTemplate(name="worker-template")
        revision = PermissionTemplateRevision(
            template=template,
            revision=1,
            privileges_json='["SELECT"]',
        )
        pool = DedicatedPool(
            name="worker-pool",
            target_size=2,
            max_total_members=4,
            max_member_purchases_per_hour=4,
            max_create_requests_per_agent_per_hour=10,
            max_delete_requests_per_agent_per_hour=10,
            purchase_config_json=json.dumps(
                build_purchase_config(storage_type="essdpl1"),
                separators=(",", ":"),
                sort_keys=True,
            ),
            purchase_profile_id=PROFILE_ID,
            purchase_profile_revision=PROFILE_REVISION,
            storage_type="essdpl1",
            region_id="cn-hangzhou",
            vpc_id="vpc-worker",
            vswitch_id="vsw-worker",
            available_health_check_interval_seconds=300,
            available_health_stale_after_seconds=600,
            permission_template_revision=revision,
        )
        session.add(pool)
        await session.flush()
        for index in range(2):
            instance = Instance(
                cluster_id=f"pc-worker-{index}",
                name=f"Worker member {index}",
                topology=InstanceTopology.SINGLE_TENANT,
                allocation_mode=AllocationMode.DEDICATED_POOL,
                status=InstanceStatus.ACTIVE,
            )
            session.add(instance)
            await session.flush()
            session.add(
                DedicatedPoolMember(
                    pool_id=pool.id,
                    instance_id=instance.id,
                    status=DedicatedMemberStatus.AVAILABLE,
                    readiness_status=ReadinessStatus.FRESH,
                    last_ready_verified_at=clock.value
                    - timedelta(seconds=601),
                )
            )
        await session.commit()
        pool_id = pool.id
    mysql = AsyncMock()
    provisioner = AsyncMock()
    config = TenantProvisioningConfig(
        worker_poll_interval_seconds=1,
        worker_claim_ttl_seconds=10,
        worker_claim_renew_seconds=1,
        worker_initial_backoff_seconds=1,
        worker_max_backoff_seconds=4,
    )
    yield factory, pool_id, mysql, provisioner, clock, config
    await engine.dispose()


def _worker(context, worker_id="dedicated-a"):
    factory, _pool_id, mysql, provisioner, clock, config = context
    return DedicatedPoolWorker(
        factory,
        config,
        provisioner,
        mysql,
        worker_id=worker_id,
        clock=clock,
    )


async def test_run_once_records_a_durable_worker_heartbeat(worker_context):
    factory, _pool_id, _mysql, _provisioner, _clock, _config = worker_context

    await _worker(worker_context).run_once()

    async with factory() as session:
        heartbeat = await session.get(
            DedicatedWorkerHeartbeat, "dedicated-a"
        )
        assert heartbeat is not None
        assert heartbeat.config_revision == 1


async def test_manual_request_wakes_an_idle_worker(worker_context):
    worker = _worker(worker_context)
    stop_event = asyncio.Event()
    idle = asyncio.Event()
    retried = asyncio.Event()
    calls = 0

    async def run_once() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            idle.set()
            return False
        retried.set()
        stop_event.set()
        return False

    worker.run_once = AsyncMock(side_effect=run_once)
    task = asyncio.create_task(worker.run_forever(stop_event))
    await asyncio.wait_for(idle.wait(), timeout=1)

    worker.request_run()

    await asyncio.wait_for(retried.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)


async def test_worker_claims_request_owned_member_preparation(worker_context):
    factory, pool_id, _mysql, _provisioner, _clock, _config = worker_context
    async with factory() as session:
        members = list(
            (
                await session.execute(
                    select(DedicatedPoolMember)
                    .where(DedicatedPoolMember.pool_id == pool_id)
                    .order_by(DedicatedPoolMember.id)
                )
            )
            .scalars()
            .all()
        )
        members[0].status = DedicatedMemberStatus.ALLOCATED_PREPARING
        members[1].status = DedicatedMemberStatus.DELETED
        await session.commit()
        expected = members[0].id

    claimed = await _worker(worker_context)._claim_replenishing_member()

    assert claimed == expected


async def test_openapi_only_worker_leaves_data_plane_ready_member_paused(
    worker_context,
):
    factory, pool_id, _mysql, provisioner, _clock, config = worker_context
    members = await _members(factory, pool_id)
    async with factory() as session:
        paused = await session.get(DedicatedPoolMember, members[0].id)
        other = await session.get(DedicatedPoolMember, members[1].id)
        assert paused is not None and other is not None
        paused.status = DedicatedMemberStatus.REPLENISHING
        paused.preparation_step = DedicatedPreparationStep.OPENAPI_READY
        other.status = DedicatedMemberStatus.DELETED
        await session.commit()

    config.dedicated_pool_preparation_mode = "openapi_only"
    worker = _worker(worker_context)
    assert await worker._claim_replenishing_member() is None
    provisioner.advance.assert_not_awaited()

    config.dedicated_pool_preparation_mode = "full"
    assert await worker._claim_replenishing_member() == members[0].id


async def test_replenishing_member_without_progress_is_polled_later(
    worker_context,
):
    factory, pool_id, _mysql, provisioner, clock, config = worker_context
    members = await _members(factory, pool_id)
    async with factory() as session:
        waiting = await session.get(DedicatedPoolMember, members[0].id)
        other = await session.get(DedicatedPoolMember, members[1].id)
        assert waiting is not None and other is not None
        waiting.status = DedicatedMemberStatus.REPLENISHING
        waiting.preparation_step = DedicatedPreparationStep.PURCHASE_REQUESTED
        other.status = DedicatedMemberStatus.DELETED
        await session.commit()

    provisioner.advance.return_value = DedicatedPreparationStep.PURCHASE_REQUESTED
    worker = _worker(worker_context)
    claimed = await worker._claim_replenishing_member()
    assert claimed == waiting.id

    await worker._advance_replenishing_member(claimed)

    async with factory() as session:
        persisted = await session.get(DedicatedPoolMember, waiting.id)
        assert persisted is not None
        assert persisted.worker_id is None
        assert persisted.next_retry_at is not None
        assert persisted.next_retry_at.replace(tzinfo=timezone.utc) == (
            clock.value
            + timedelta(seconds=config.worker_poll_interval_seconds)
        )
    assert await worker._claim_replenishing_member() is None

    clock.value += timedelta(seconds=config.worker_poll_interval_seconds)
    assert await worker._claim_replenishing_member() == waiting.id


async def test_replenishing_member_progress_remains_immediately_runnable(
    worker_context,
):
    factory, pool_id, _mysql, provisioner, _clock, _config = worker_context
    members = await _members(factory, pool_id)
    async with factory() as session:
        progressing = await session.get(DedicatedPoolMember, members[0].id)
        other = await session.get(DedicatedPoolMember, members[1].id)
        assert progressing is not None and other is not None
        progressing.status = DedicatedMemberStatus.REPLENISHING
        progressing.preparation_step = DedicatedPreparationStep.PURCHASE_REQUESTED
        other.status = DedicatedMemberStatus.DELETED
        await session.commit()

    provisioner.advance.return_value = DedicatedPreparationStep.CLUSTER_READY
    worker = _worker(worker_context)
    claimed = await worker._claim_replenishing_member()
    assert claimed == progressing.id

    await worker._advance_replenishing_member(claimed)

    async with factory() as session:
        persisted = await session.get(DedicatedPoolMember, progressing.id)
        assert persisted is not None
        assert persisted.next_retry_at is None


async def test_busy_worker_throttles_heartbeat_to_independent_interval(
    worker_context, monkeypatch
):
    _factory, _pool_id, _mysql, _provisioner, clock, config = worker_context
    heartbeat = AsyncMock()
    monkeypatch.setattr(
        "server.core.dedicated_pool_worker.record_worker_heartbeat",
        heartbeat,
    )
    worker = _worker(worker_context)

    await worker.run_once()
    await worker.run_once()
    assert heartbeat.await_count == 1

    clock.value += timedelta(
        seconds=config.dedicated_worker_heartbeat_interval_seconds
    )
    await worker.run_once()
    assert heartbeat.await_count == 2


async def _members(factory, pool_id):
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(DedicatedPoolMember)
                    .where(DedicatedPoolMember.pool_id == pool_id)
                    .order_by(DedicatedPoolMember.id)
                )
            )
            .scalars()
            .all()
        )


async def test_evidence_expiry_marks_stale_without_replacement(worker_context):
    factory, pool_id, mysql, _provisioner, _clock, _config = worker_context

    assert await _worker(worker_context).run_once() is True

    members = await _members(factory, pool_id)
    assert len(members) == 2
    assert {member.readiness_status for member in members} == {
        ReadinessStatus.STALE
    }
    assert {member.status for member in members} == {
        DedicatedMemberStatus.AVAILABLE
    }
    mysql.verify.assert_not_awaited()


async def test_successful_forced_recheck_restores_freshness(worker_context):
    factory, pool_id, mysql, _provisioner, clock, _config = worker_context
    worker = _worker(worker_context)
    await worker.run_once()

    assert await worker.run_once() is True

    members = await _members(factory, pool_id)
    refreshed = [
        member
        for member in members
        if member.readiness_status == ReadinessStatus.FRESH
    ]
    assert len(refreshed) == 1
    assert refreshed[0].last_ready_verified_at.replace(
        tzinfo=timezone.utc
    ) == clock.value
    mysql.verify.assert_awaited_once()


async def test_periodic_check_runs_before_evidence_becomes_stale(worker_context):
    factory, pool_id, mysql, _provisioner, clock, _config = worker_context
    async with factory() as session:
        members = list(
            (
                await session.execute(
                    select(DedicatedPoolMember).where(
                        DedicatedPoolMember.pool_id == pool_id
                    )
                )
            )
            .scalars()
            .all()
        )
        for member in members:
            member.last_ready_verified_at = clock.value - timedelta(seconds=301)
        await session.commit()

    assert await _worker(worker_context).run_once() is True

    mysql.verify.assert_awaited_once()
    members = await _members(factory, pool_id)
    assert len(members) == 2
    assert all(member.status == DedicatedMemberStatus.AVAILABLE for member in members)
    assert all(member.readiness_status == ReadinessStatus.FRESH for member in members)


async def test_inconclusive_recheck_stays_stale_without_purchase(worker_context):
    factory, pool_id, mysql, _provisioner, _clock, _config = worker_context
    worker = _worker(worker_context)
    await worker.run_once()
    mysql.verify.side_effect = TimeoutError("data plane unavailable")

    assert await worker.run_once() is True

    members = await _members(factory, pool_id)
    assert len(members) == 2
    assert all(member.status == DedicatedMemberStatus.AVAILABLE for member in members)
    assert all(member.readiness_status == ReadinessStatus.STALE for member in members)
    assert any(member.next_retry_at is not None for member in members)


async def test_conclusive_failure_quarantines_then_requests_one_replacement(
    worker_context,
):
    factory, pool_id, mysql, _provisioner, _clock, _config = worker_context
    async with factory() as session:
        pool = await session.get(DedicatedPool, pool_id)
        pool.target_size = 1
        members = list(
            (
                await session.execute(
                    select(DedicatedPoolMember).where(
                        DedicatedPoolMember.pool_id == pool_id
                    )
                )
            )
            .scalars()
            .all()
        )
        await session.delete(members[1])
        await session.delete(members[1].instance)
        await session.commit()
    worker = _worker(worker_context)
    await worker.run_once()
    mysql.verify.side_effect = DedicatedGrantVerificationError("mismatch")

    assert await worker.run_once() is True
    assert (await _members(factory, pool_id))[0].status == (
        DedicatedMemberStatus.QUARANTINED
    )
    assert await worker.run_once() is True

    members = await _members(factory, pool_id)
    assert [member.status for member in members].count(
        DedicatedMemberStatus.REPLENISHING
    ) == 1
    assert len(members) == 2


async def test_live_check_lease_prevents_duplicate_health_work(worker_context):
    factory, pool_id, mysql, _provisioner, _clock, _config = worker_context
    first = _worker(worker_context, "dedicated-a")
    second = _worker(worker_context, "dedicated-b")
    await first.run_once()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_verify(_member):
        if not entered.is_set():
            entered.set()
            await release.wait()

    mysql.verify.side_effect = slow_verify
    task = asyncio.create_task(first.run_once())
    await entered.wait()

    assert await second.run_once() is True
    assert mysql.verify.await_count == 2
    release.set()
    assert await task is True

    members = await _members(factory, pool_id)
    assert all(member.readiness_status == ReadinessStatus.FRESH for member in members)


async def test_expired_check_lease_is_recovered(worker_context):
    factory, pool_id, mysql, _provisioner, clock, _config = worker_context
    await _worker(worker_context).run_once()
    async with factory() as session:
        member = (
            await session.execute(
                select(DedicatedPoolMember)
                .where(DedicatedPoolMember.pool_id == pool_id)
                .limit(1)
            )
        ).scalar_one()
        member.readiness_status = ReadinessStatus.CHECKING
        member.worker_id = "crashed-worker"
        member.worker_lease_until = clock.value - timedelta(seconds=1)
        await session.commit()

    assert await _worker(worker_context, "replacement-worker").run_once() is True
    mysql.verify.assert_awaited_once()
