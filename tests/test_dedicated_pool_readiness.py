from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from server.config import AppConfig, TenantProvisioningConfig
from server.configuration.types import EffectiveConfig, ModuleDocument, ModuleState
from server.models import (
    Base,
    DedicatedWorkerHeartbeat,
    PermissionTemplate,
    PermissionTemplateRevision,
    SystemConfig,
)
from server.core.dedicated_purchase_profile import PurchaseProfileStatus


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as value:
        yield value
    await engine.dispose()


def _readiness_module():
    return importlib.import_module("server.core.dedicated_pool_readiness")


@pytest.mark.parametrize(
    (
        "target",
        "allocatable",
        "planning",
        "blockers",
        "failures",
        "capacity_limited",
        "expected",
    ),
    [
        (1, 0, 0, ["DEDICATED_WORKER_NOT_RUNNING"], 0, False, "not_started"),
        (1, 1, 1, ["ALIYUN_ACCESS_NOT_CONFIGURED"], 0, False, "ready"),
        (2, 0, 0, [], 0, True, "capacity_limited"),
        (2, 0, 0, [], 1, False, "error"),
        (2, 0, 1, [], 0, False, "prewarming"),
        (2, 1, 2, [], 0, False, "partially_ready"),
    ],
)
def test_supply_state_has_stable_precedence(
    target: int,
    allocatable: int,
    planning: int,
    blockers: list[str],
    failures: int,
    capacity_limited: bool,
    expected: str,
) -> None:
    readiness = _readiness_module()

    actual = readiness.derive_supply_state(
        target=target,
        allocatable=allocatable,
        planning=planning,
        blockers=blockers,
        failures=failures,
        capacity_limited=capacity_limited,
    )

    assert actual.value == expected


def test_capacity_limited_snapshot_explains_the_hard_limit() -> None:
    readiness = _readiness_module()
    view = SimpleNamespace(
        pool=SimpleNamespace(
            permission_template_revision_id="revision-1",
            members=[],
            target_size=2,
            max_total_members=2,
        ),
        capacity=SimpleNamespace(
            allocatable=0,
            planning=1,
            billable_total=2,
        ),
    )
    aggregate = SimpleNamespace(blocking_reasons=())

    snapshot = readiness.pool_supply_snapshot(
        view,
        aggregate,
        purchase_profile_status=PurchaseProfileStatus.VALID,
    )

    assert snapshot.state.value == "capacity_limited"
    assert snapshot.blocking_reasons == ("POOL_CAPACITY_LIMIT_REACHED",)


def test_worker_heartbeat_defaults_and_floor() -> None:
    config = TenantProvisioningConfig()

    assert config.dedicated_worker_heartbeat_interval_seconds == 10
    assert config.dedicated_worker_heartbeat_stale_after_seconds == 30

    with pytest.raises(ValidationError, match="at least three heartbeat"):
        TenantProvisioningConfig(
            dedicated_worker_heartbeat_interval_seconds=11,
            dedicated_worker_heartbeat_stale_after_seconds=32,
        )
    with pytest.raises(ValidationError, match="at least 30 seconds"):
        TenantProvisioningConfig(
            dedicated_worker_heartbeat_interval_seconds=5,
            dedicated_worker_heartbeat_stale_after_seconds=20,
        )


async def test_worker_heartbeat_uses_database_clock(session: AsyncSession) -> None:
    readiness = _readiness_module()

    await readiness.record_worker_heartbeat(
        session,
        worker_id="worker-a",
        config_revision=1,
    )
    await session.commit()
    heartbeat = await session.get(DedicatedWorkerHeartbeat, "worker-a")
    assert heartbeat is not None
    started_at = heartbeat.started_at

    heartbeat.last_heartbeat_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    await session.commit()
    await readiness.record_worker_heartbeat(
        session,
        worker_id="worker-a",
        config_revision=2,
    )
    await session.commit()
    await session.refresh(heartbeat)
    database_now = await session.scalar(select(func.current_timestamp()))
    assert database_now is not None
    if database_now.tzinfo is None:
        database_now = database_now.replace(tzinfo=timezone.utc)
    last_heartbeat_at = heartbeat.last_heartbeat_at
    if last_heartbeat_at.tzinfo is None:
        last_heartbeat_at = last_heartbeat_at.replace(tzinfo=timezone.utc)

    assert abs((database_now - last_heartbeat_at).total_seconds()) < 2
    assert heartbeat.started_at == started_at
    assert heartbeat.config_revision == 2


async def test_active_worker_heartbeats_compare_against_database_clock(
    session: AsyncSession,
) -> None:
    readiness = _readiness_module()
    await readiness.record_worker_heartbeat(
        session,
        worker_id="worker-fresh",
        config_revision=1,
    )
    await readiness.record_worker_heartbeat(
        session,
        worker_id="worker-stale",
        config_revision=1,
    )
    await session.commit()
    database_now = await session.scalar(select(func.current_timestamp()))
    assert database_now is not None
    stale = await session.get(DedicatedWorkerHeartbeat, "worker-stale")
    assert stale is not None
    stale.last_heartbeat_at = database_now - timedelta(seconds=31)
    await session.commit()

    snapshot = await readiness.worker_readiness_snapshot(
        session,
        stale_after_seconds=30,
    )

    assert snapshot.active_worker_count == 1
    assert snapshot.last_heartbeat_at is not None
    assert snapshot.database_now == database_now


async def test_aggregate_readiness_reports_all_current_blockers(
    session: AsyncSession,
) -> None:
    readiness = _readiness_module()
    config = AppConfig.model_validate(
        {
            "polardb": {
                "tenant_provisioning": {"dedicated_pool_enabled": True}
            }
        }
    )

    blocked = await readiness.aggregate_readiness(session, config=config)

    assert blocked.purchase_profile.valid is True
    assert blocked.blocking_reasons == (
        "DEDICATED_WORKER_NOT_RUNNING",
        "ALIYUN_ACCESS_NOT_CONFIGURED",
        "PERMISSION_TEMPLATE_UNAVAILABLE",
    )

    template = PermissionTemplate(
        id="builtin-mysql-default",
        name="Agent MySQL default permissions",
    )
    session.add(
        PermissionTemplateRevision(
            id="builtin-mysql-default-v1",
            template=template,
            revision=1,
            privileges_json='["SELECT"]',
            grant_option=False,
        )
    )
    await readiness.record_worker_heartbeat(
        session,
        worker_id="worker-ready",
        config_revision=1,
    )
    await session.commit()
    configured = config.model_copy(
        update={
            "aliyun": config.aliyun.__class__.model_validate(
                {
                    "credential_mode": "direct_ak",
                    "direct_ak": {
                        "access_key_id": "test-ak",
                        "access_key_secret": "test-secret",
                    },
                }
            )
        }
    )

    ready = await readiness.aggregate_readiness(session, config=configured)

    assert ready.worker.active_worker_count == 1
    assert ready.aliyun_access.validated is True
    assert ready.permission_template.default_revision_id == (
        "builtin-mysql-default-v1"
    )
    assert ready.blocking_reasons == ()


async def test_aggregate_readiness_blocks_persisted_legacy_pool_target(
    session: AsyncSession,
) -> None:
    readiness = _readiness_module()
    session.add(
        SystemConfig(
            config_key="module.resource_pool",
            config_value=ModuleDocument(
                revision=1,
                workflow_state=ModuleState.ACTIVE,
                initial_state=ModuleState.SKIPPED,
                effective=EffectiveConfig(
                    revision=1,
                    state=ModuleState.ACTIVE,
                    config={"target_size": 2, "vpc_id": "vpc-legacy"},
                ),
            ).model_dump_json(),
            config_version=1,
        )
    )
    await session.commit()
    config = AppConfig.model_validate(
        {
            "polardb": {
                "tenant_provisioning": {"dedicated_pool_enabled": True}
            }
        }
    )

    blocked = await readiness.aggregate_readiness(session, config=config)

    assert "LEGACY_POOL_CONFIG_PRESENT" in blocked.blocking_reasons


async def test_readiness_never_requires_restart_for_runtime_activation(
    session: AsyncSession,
) -> None:
    readiness = _readiness_module()
    config = AppConfig.model_validate(
        {
            "polardb": {
                "tenant_provisioning": {"dedicated_pool_enabled": True}
            }
        }
    )

    snapshot = await readiness.aggregate_readiness(
        session,
        config=config,
    )

    assert snapshot.blocking_reasons[0] == "DEDICATED_WORKER_NOT_RUNNING"
    assert "DEDICATED_WORKER_RESTART_REQUIRED" not in snapshot.blocking_reasons


async def test_explicit_simulation_removes_cloud_credential_blocker(
    session: AsyncSession,
) -> None:
    readiness = _readiness_module()
    config = AppConfig.model_validate(
        {
            "polardb": {
                "tenant_provisioning": {
                    "dedicated_pool_simulation_enabled": True
                }
            }
        }
    )

    snapshot = await readiness.aggregate_readiness(session, config=config)

    assert snapshot.simulation_mode is True
    assert "ALIYUN_ACCESS_NOT_CONFIGURED" not in snapshot.blocking_reasons


async def test_active_cloud_credentials_take_precedence_over_simulation_flag(
    session: AsyncSession,
) -> None:
    readiness = _readiness_module()
    config = AppConfig.model_validate(
        {
            "aliyun": {
                "credential_mode": "direct_ak",
                "direct_ak": {
                    "access_key_id": "test-ak",
                    "access_key_secret": "test-secret",
                },
            },
            "polardb": {
                "tenant_provisioning": {
                    "dedicated_pool_simulation_enabled": True
                }
            },
        }
    )

    snapshot = await readiness.aggregate_readiness(session, config=config)

    assert snapshot.aliyun_access.configured is True
    assert snapshot.simulation_mode is False
    assert "ALIYUN_ACCESS_NOT_CONFIGURED" not in snapshot.blocking_reasons
