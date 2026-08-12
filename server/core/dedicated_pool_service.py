from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.dedicated_pool_repository import CapacitySnapshot, capacity_snapshot
from server.core.dedicated_purchase_profile import (
    PROFILE_ID,
    PROFILE_REVISION,
    UnsupportedStorageType,
    build_purchase_config,
)
from server.models import (
    AllocationMode,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DedicatedMemberStatus,
    DedicatedPool,
    DedicatedPoolMember,
    DedicatedPoolStatus,
    DedicatedPreparationStep,
    Instance,
    InstanceCredential,
    InstanceStatus,
    InstanceTopology,
    LifecycleAdministratorPolicy,
    PermissionTemplateRevision,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningBackendType,
    ReadinessStatus,
)
from server.models.base import utc_now
from server.models.dedicated_pool import validate_readiness_schedule


class DedicatedPoolServiceError(ValueError):
    pass


class DedicatedPoolNotFound(DedicatedPoolServiceError):
    pass


class DedicatedPoolStateConflict(DedicatedPoolServiceError):
    pass


class DedicatedPoolConfigurationConflict(DedicatedPoolStateConflict):
    pass


class DedicatedPoolNetworkConfirmationRequired(DedicatedPoolServiceError):
    pass


@dataclass(frozen=True, slots=True)
class DedicatedPoolView:
    pool: DedicatedPool
    capacity: CapacitySnapshot

    @property
    def surplus(self) -> int:
        return max(0, self.capacity.planning - self.pool.target_size)


def _validate_pool_configuration(
    *,
    target_size: int,
    max_total_members: int,
    check_interval_seconds: int,
    stale_after_seconds: int,
    delete_cooldown_duration_hours: int | None,
) -> None:
    if target_size < 0 or max_total_members <= 0:
        raise DedicatedPoolServiceError("Pool sizes are invalid")
    if target_size > max_total_members:
        raise DedicatedPoolServiceError(
            "target_size must not exceed max_total_members"
        )
    try:
        validate_readiness_schedule(
            check_interval_seconds=check_interval_seconds,
            stale_after_seconds=stale_after_seconds,
        )
    except ValueError as error:
        raise DedicatedPoolServiceError(str(error)) from None
    if (
        delete_cooldown_duration_hours is not None
        and delete_cooldown_duration_hours < 1
    ):
        raise DedicatedPoolServiceError(
            "delete cooldown must be at least 1 hour"
        )


def _typed_purchase_config(values: dict[str, Any]) -> dict[str, str]:
    if values.get("purchase_profile_id") != PROFILE_ID:
        raise DedicatedPoolServiceError("Unsupported purchase profile ID")
    if values.get("purchase_profile_revision") != PROFILE_REVISION:
        raise DedicatedPoolServiceError("Unsupported purchase profile revision")
    storage_type = values.get("storage_type")
    if not isinstance(storage_type, str):
        raise DedicatedPoolServiceError("storage_type is required")
    try:
        return build_purchase_config(storage_type=storage_type)
    except UnsupportedStorageType as error:
        raise DedicatedPoolServiceError(str(error)) from None


_NETWORK_FIELDS = ("region_id", "zone_id", "vpc_id", "vswitch_id")


def _normalize_network_values(values: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(values)
    for field in _NETWORK_FIELDS:
        if field not in normalized:
            continue
        value = normalized[field]
        if not isinstance(value, str) or not value.strip():
            raise DedicatedPoolServiceError(f"{field} cannot be blank")
        normalized[field] = value.strip()
    return normalized


async def create_dedicated_pool(
    session: AsyncSession,
    *,
    values: dict[str, Any],
) -> DedicatedPool:
    values = _normalize_network_values(values)
    _validate_pool_configuration(
        target_size=values["target_size"],
        max_total_members=values["max_total_members"],
        check_interval_seconds=values[
            "available_health_check_interval_seconds"
        ],
        stale_after_seconds=values[
            "available_health_stale_after_seconds"
        ],
        delete_cooldown_duration_hours=values.get(
            "delete_cooldown_duration_hours"
        ),
    )
    revision = await session.get(
        PermissionTemplateRevision,
        values["permission_template_revision_id"],
    )
    if revision is None:
        raise DedicatedPoolNotFound("Permission template revision not found")
    purchase_config = _typed_purchase_config(values)
    pool = DedicatedPool(
        name=values["name"].strip(),
        target_size=values["target_size"],
        max_total_members=values["max_total_members"],
        max_member_purchases_per_hour=values[
            "max_member_purchases_per_hour"
        ],
        max_create_requests_per_agent_per_hour=values[
            "max_create_requests_per_agent_per_hour"
        ],
        max_delete_requests_per_agent_per_hour=values[
            "max_delete_requests_per_agent_per_hour"
        ],
        purchase_config_json=json.dumps(
            purchase_config, separators=(",", ":"), sort_keys=True
        ),
        purchase_profile_id=PROFILE_ID,
        purchase_profile_revision=PROFILE_REVISION,
        storage_type=values["storage_type"],
        region_id=values["region_id"],
        vpc_id=values["vpc_id"],
        vswitch_id=values["vswitch_id"],
        zone_id=values["zone_id"],
        security_ip_list=values.get("security_ip_list"),
        endpoint_net_type=values.get("endpoint_net_type", "Private"),
        reclaim_policy=values["reclaim_policy"],
        lifecycle_admin_policy=values["lifecycle_admin_policy"],
        permission_template_revision_id=revision.id,
        account_name_template=values.get("account_name_template", "agentic"),
        database_name_template=values.get("database_name_template", "agentic"),
        delete_cooldown_duration_hours=values.get(
            "delete_cooldown_duration_hours"
        ),
        available_health_check_interval_seconds=values[
            "available_health_check_interval_seconds"
        ],
        available_health_stale_after_seconds=values[
            "available_health_stale_after_seconds"
        ],
    )
    backend = ProvisioningBackend(
        backend_type=ProvisioningBackendType.DEDICATED_POOL,
        dedicated_pool=pool,
        max_active_resources=values["max_total_members"],
        permission_template_revision_id=revision.id,
    )
    backend.health = ProvisioningBackendHealth(
        healthy=True, checked_at=utc_now()
    )
    session.add(backend)
    await session.flush()
    return pool


async def update_dedicated_pool(
    session: AsyncSession,
    *,
    pool_id: str,
    changes: dict[str, Any],
    expected_config_revision: int | None = None,
    network_change_confirmed: bool = False,
) -> DedicatedPool:
    pool = await session.get(DedicatedPool, pool_id)
    if pool is None:
        raise DedicatedPoolNotFound("Dedicated pool not found")
    if (
        expected_config_revision is not None
        and pool.config_revision != expected_config_revision
    ):
        raise DedicatedPoolConfigurationConflict(
            "Dedicated pool configuration changed; reload before updating"
        )
    changes = _normalize_network_values(changes)
    network_changed = any(
        field in changes
        and changes[field] != str(getattr(pool, field) or "").strip()
        for field in _NETWORK_FIELDS
    )
    if network_changed and not network_change_confirmed:
        raise DedicatedPoolNetworkConfirmationRequired(
            "Confirm connectivity and availability risks before changing "
            "pool network placement"
        )
    merged = {
        "target_size": changes.get("target_size", pool.target_size),
        "max_total_members": changes.get(
            "max_total_members", pool.max_total_members
        ),
        "check_interval_seconds": changes.get(
            "available_health_check_interval_seconds",
            pool.available_health_check_interval_seconds,
        ),
        "stale_after_seconds": changes.get(
            "available_health_stale_after_seconds",
            pool.available_health_stale_after_seconds,
        ),
        "delete_cooldown_duration_hours": changes.get(
            "delete_cooldown_duration_hours",
            pool.delete_cooldown_duration_hours,
        ),
    }
    _validate_pool_configuration(**merged)
    if "permission_template_revision_id" in changes:
        revision = await session.get(
            PermissionTemplateRevision,
            changes["permission_template_revision_id"],
        )
        if revision is None:
            raise DedicatedPoolNotFound(
                "Permission template revision not found"
            )
    profile_fields = {
        "purchase_profile_id",
        "purchase_profile_revision",
        "storage_type",
    }
    if profile_fields.intersection(changes):
        profile_values = {
            "purchase_profile_id": changes.get(
                "purchase_profile_id", pool.purchase_profile_id
            ),
            "purchase_profile_revision": changes.get(
                "purchase_profile_revision", pool.purchase_profile_revision
            ),
            "storage_type": changes.get("storage_type", pool.storage_type),
        }
        purchase_config = _typed_purchase_config(profile_values)
        pool.purchase_config_json = json.dumps(
            purchase_config, separators=(",", ":"), sort_keys=True
        )
    for field, value in changes.items():
        setattr(pool, field, value)
    if pool.provisioning_backend is not None:
        pool.provisioning_backend.max_active_resources = (
            pool.max_total_members
        )
        pool.provisioning_backend.permission_template_revision_id = (
            pool.permission_template_revision_id
        )
    pool.config_revision += 1
    await session.flush()
    return pool


async def upgrade_purchase_profile(
    session: AsyncSession,
    *,
    pool_id: str,
    expected_config_revision: int,
) -> DedicatedPool:
    pool = await session.get(DedicatedPool, pool_id)
    if pool is None:
        raise DedicatedPoolNotFound("Dedicated pool not found")
    if pool.config_revision != expected_config_revision:
        raise DedicatedPoolStateConflict(
            "Dedicated pool configuration changed; reload before upgrading"
        )
    purchase_config = build_purchase_config(storage_type="essdpl1")
    pool.purchase_config_json = json.dumps(
        purchase_config, separators=(",", ":"), sort_keys=True
    )
    pool.purchase_profile_id = PROFILE_ID
    pool.purchase_profile_revision = PROFILE_REVISION
    pool.storage_type = "essdpl1"
    pool.config_revision += 1
    await session.flush()
    return pool


async def drain_dedicated_pool(
    session: AsyncSession, pool_id: str
) -> DedicatedPool:
    return await update_dedicated_pool(
        session,
        pool_id=pool_id,
        changes={
            "status": DedicatedPoolStatus.DRAINING,
            "target_size": 0,
        },
    )


async def register_external_member(
    session: AsyncSession,
    *,
    pool_id: str,
    instance_id: str,
    lifecycle_credential_id: str | None,
) -> DedicatedPoolMember:
    pool = await session.get(DedicatedPool, pool_id)
    if pool is None:
        raise DedicatedPoolNotFound("Dedicated pool not found")
    if pool.lifecycle_admin_policy != LifecycleAdministratorPolicy.ADMIN_PROVIDED:
        raise DedicatedPoolStateConflict(
            "External registration requires admin_provided lifecycle policy"
        )
    instance = await session.get(Instance, instance_id)
    if (
        instance is None
        or instance.topology != InstanceTopology.SINGLE_TENANT
        or instance.status != InstanceStatus.ACTIVE
        or instance.host is None
        or instance.port is None
    ):
        raise DedicatedPoolServiceError(
            "External member must be an active single-tenant instance with an endpoint"
        )
    if instance.allocation_mode != AllocationMode.REGISTERED:
        raise DedicatedPoolServiceError(
            "External member must be an administrator-registered instance"
        )
    credential = (
        await session.get(InstanceCredential, lifecycle_credential_id)
        if lifecycle_credential_id is not None
        else None
    )
    if (
        credential is None
        or credential.instance_id != instance.id
        or credential.purpose != CredentialPurpose.PROVISIONING_ADMIN
        or credential.capability != CredentialCapability.ADMIN
        or credential.status != CredentialStatus.ACTIVE
        or credential.username_ciphertext is None
        or credential.password_ciphertext is None
    ):
        raise DedicatedPoolServiceError(
            "An active lifecycle administrator credential for this instance is required"
        )
    existing = await session.scalar(
        select(DedicatedPoolMember).where(
            DedicatedPoolMember.instance_id == instance.id
        )
    )
    if existing is not None:
        raise DedicatedPoolStateConflict(
            "Instance is already registered in a Dedicated pool"
        )
    member = DedicatedPoolMember(
        pool=pool,
        instance_id=instance.id,
        status=DedicatedMemberStatus.REPLENISHING,
        readiness_status=ReadinessStatus.STALE,
        preparation_step=DedicatedPreparationStep.LIFECYCLE_ACCOUNT_CREATED,
        host=instance.host,
        port=instance.port,
        lifecycle_username_ciphertext=credential.username_ciphertext,
        lifecycle_password_ciphertext=credential.password_ciphertext,
    )
    session.add(member)
    await session.flush()
    return member


async def set_member_action(
    session: AsyncSession,
    *,
    member_id: str,
    action: str,
    allow_data_plane: bool = True,
) -> DedicatedPoolMember:
    member = await session.get(DedicatedPoolMember, member_id)
    if member is None:
        raise DedicatedPoolNotFound("Dedicated pool member not found")
    if action == "retry" and member.status in {
        DedicatedMemberStatus.REPLENISHING,
        DedicatedMemberStatus.QUARANTINED,
    }:
        if (
            member.preparation_step == DedicatedPreparationStep.OPENAPI_READY
            and not allow_data_plane
        ):
            raise DedicatedPoolStateConflict(
                "Member already reached the OpenAPI-only preparation boundary"
            )
        if member.status == DedicatedMemberStatus.QUARANTINED:
            member.status = DedicatedMemberStatus.REPLENISHING
        member.readiness_status = ReadinessStatus.STALE
        member.retry_count = 0
        member.next_retry_at = None
        member.failure_reason = None
    elif action == "quarantine" and member.status in {
        DedicatedMemberStatus.AVAILABLE,
        DedicatedMemberStatus.REPLENISHING,
    }:
        member.status = DedicatedMemberStatus.QUARANTINED
        member.readiness_status = ReadinessStatus.STALE
    elif action == "destroy" and member.allocated_resource_id is None and member.status in {
        DedicatedMemberStatus.AVAILABLE,
        DedicatedMemberStatus.REPLENISHING,
        DedicatedMemberStatus.QUARANTINED,
    }:
        member.status = DedicatedMemberStatus.DELETING
        member.worker_id = None
        member.worker_lease_until = None
        member.next_retry_at = None
    else:
        raise DedicatedPoolStateConflict(
            "Member action is not allowed from its current state"
        )
    await session.flush()
    return member


async def update_member_delete_cooldown(
    session: AsyncSession,
    *,
    member_id: str,
    delete_cooldown_duration_hours: int | None,
) -> DedicatedPoolMember:
    member = await session.get(DedicatedPoolMember, member_id)
    if member is None:
        raise DedicatedPoolNotFound("Dedicated pool member not found")
    if (
        delete_cooldown_duration_hours is not None
        and delete_cooldown_duration_hours < 1
    ):
        raise DedicatedPoolServiceError(
            "delete cooldown must be at least 1 hour"
        )
    member.delete_cooldown_duration_hours = delete_cooldown_duration_hours
    await session.flush()
    return member


async def dedicated_pool_view(
    session: AsyncSession,
    pool: DedicatedPool,
    *,
    now: datetime | None = None,
) -> DedicatedPoolView:
    return DedicatedPoolView(
        pool=pool,
        capacity=await capacity_snapshot(
            session, pool_id=pool.id, now=now or utc_now()
        ),
    )
