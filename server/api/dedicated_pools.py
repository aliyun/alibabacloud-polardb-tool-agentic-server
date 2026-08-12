from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.aliyun.diagnostics import safe_error_code, safe_error_detail
from server.auth.dependencies import require_admin
from server.config import get_config
from server.core.audit_logger import log_audit
from server.core.dedicated_pool_service import (
    DedicatedPoolConfigurationConflict,
    DedicatedPoolNetworkConfirmationRequired,
    DedicatedPoolNotFound,
    DedicatedPoolServiceError,
    DedicatedPoolStateConflict,
    DedicatedPoolView,
    create_dedicated_pool,
    dedicated_pool_view,
    drain_dedicated_pool,
    register_external_member,
    set_member_action,
    update_member_delete_cooldown,
    update_dedicated_pool,
    upgrade_purchase_profile,
)
from server.core.dedicated_pool_readiness import (
    DedicatedReadiness,
    aggregate_readiness,
    pool_supply_snapshot,
)
from server.core.dedicated_purchase_profile import (
    AGENTIC_DEDICATED_PROFILE,
    PurchaseProfileStatus,
    classify_legacy_purchase_config,
)
from server.db.engine import get_session
from server.models import (
    AuditStatus,
    AgentProvisioningBinding,
    DedicatedMemberStatus,
    DedicatedPool,
    DedicatedPoolMember,
    DedicatedPreparationStep,
    LifecycleAdministratorPolicy,
    ProvisioningBackend,
    ReclaimPolicy,
    User,
)
from server.models.base import utc_now

router = APIRouter(prefix="/dedicated-pools", tags=["dedicated-pools"])

_SAFE_FAILURE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")


class CreateDedicatedPoolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    target_size: int = Field(ge=0)
    max_total_members: int = Field(gt=0)
    max_member_purchases_per_hour: int = Field(gt=0)
    max_create_requests_per_agent_per_hour: int = Field(gt=0)
    max_delete_requests_per_agent_per_hour: int = Field(gt=0)
    purchase_profile_id: str | None = Field(default=None, max_length=64)
    purchase_profile_revision: int | None = Field(default=None, gt=0)
    storage_type: str | None = Field(default=None, max_length=64)
    purchase_config: dict[str, Any] | None = Field(default=None, exclude=True)
    region_id: str = Field(min_length=1, max_length=64)
    vpc_id: str = Field(min_length=1, max_length=64)
    vswitch_id: str = Field(min_length=1, max_length=64)
    zone_id: str = Field(min_length=1, max_length=64)
    security_ip_list: str | None = Field(default=None, max_length=2048)
    endpoint_net_type: str = Field(default="Private", max_length=32)
    reclaim_policy: ReclaimPolicy = ReclaimPolicy.DESTROY
    lifecycle_admin_policy: LifecycleAdministratorPolicy = (
        LifecycleAdministratorPolicy.PAS_MANAGED
    )
    permission_template_revision_id: str = Field(min_length=1, max_length=36)
    account_name_template: str = Field(default="agentic", min_length=1, max_length=64)
    database_name_template: str = Field(default="agentic", min_length=1, max_length=64)
    delete_cooldown_duration_hours: int | None = Field(default=None, ge=1)
    available_health_check_interval_seconds: int = Field(default=300, gt=0)
    available_health_stale_after_seconds: int = Field(default=600, gt=0)

    @field_validator(
        "region_id", "zone_id", "vpc_id", "vswitch_id", mode="before"
    )
    @classmethod
    def strip_network_identifier(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("security_ip_list", mode="before")
    @classmethod
    def strip_security_ip_list(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return value.strip() or None


class UpdateDedicatedPoolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    target_size: int | None = Field(default=None, ge=0)
    max_total_members: int | None = Field(default=None, gt=0)
    max_member_purchases_per_hour: int | None = Field(default=None, gt=0)
    max_create_requests_per_agent_per_hour: int | None = Field(default=None, gt=0)
    max_delete_requests_per_agent_per_hour: int | None = Field(default=None, gt=0)
    purchase_profile_id: str | None = Field(default=None, max_length=64)
    purchase_profile_revision: int | None = Field(default=None, gt=0)
    storage_type: str | None = Field(default=None, max_length=64)
    purchase_config: dict[str, Any] | None = Field(default=None, exclude=True)
    region_id: str | None = Field(default=None, min_length=1, max_length=64)
    vpc_id: str | None = Field(default=None, min_length=1, max_length=64)
    vswitch_id: str | None = Field(default=None, min_length=1, max_length=64)
    zone_id: str | None = Field(default=None, min_length=1, max_length=64)
    security_ip_list: str | None = Field(default=None, max_length=2048)
    expected_config_revision: int = Field(gt=0)
    network_change_confirmed: bool = False
    reclaim_policy: ReclaimPolicy | None = None
    permission_template_revision_id: str | None = Field(
        default=None, min_length=1, max_length=36
    )
    delete_cooldown_duration_hours: int | None = Field(default=None, ge=1)
    available_health_check_interval_seconds: int | None = Field(default=None, gt=0)
    available_health_stale_after_seconds: int | None = Field(default=None, gt=0)

    @field_validator(
        "region_id", "zone_id", "vpc_id", "vswitch_id", mode="before"
    )
    @classmethod
    def strip_network_identifier(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("security_ip_list", mode="before")
    @classmethod
    def strip_security_ip_list(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return value.strip() or None


class RegisterDedicatedMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: str = Field(min_length=1, max_length=36)
    lifecycle_credential_id: str | None = Field(default=None, max_length=36)


class UpdateDedicatedMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delete_cooldown_duration_hours: int | None = Field(default=None, ge=1)


class UpgradePurchaseProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_config_revision: int = Field(gt=0)


class DedicatedMemberResponse(BaseModel):
    id: str
    instance_id: str
    allocated_resource_id: str | None
    status: str
    readiness_status: str
    preparation_step: str
    cloud_request_id: str | None
    last_ready_verified_at: datetime | None
    readiness_evidence_age_seconds: int | None
    delete_cooldown_duration_hours: int
    delete_cooldown_source: str
    failure_reason: str | None
    failure_detail: str | None
    failure_occurred_at: datetime | None
    failure_operation: str | None
    actions: dict[str, bool]


class DedicatedPoolRouteUsageResponse(BaseModel):
    agent_id: str
    agent_name: str
    binding_id: str
    enabled: bool
    routing_order: int | None
    role: Literal["primary", "fallback", "paused"]


class DedicatedPoolResponse(BaseModel):
    id: str
    name: str
    status: str
    target_size: int
    max_total_members: int
    max_member_purchases_per_hour: int
    max_create_requests_per_agent_per_hour: int
    max_delete_requests_per_agent_per_hour: int
    purchase_profile_id: str | None
    purchase_profile_revision: int | None
    purchase_profile_status: PurchaseProfileStatus
    storage_type: str | None
    region_id: str
    vpc_id: str
    vswitch_id: str
    zone_id: str | None
    security_ip_list: str | None
    reclaim_policy: str
    lifecycle_admin_policy: str
    permission_template_revision_id: str
    delete_cooldown_duration_hours: int | None
    effective_delete_cooldown_duration_hours: int
    available_health_check_interval_seconds: int
    available_health_stale_after_seconds: int
    config_revision: int
    allocatable: int
    planning: int
    billable_total: int
    surplus: int
    supply_state: str
    supply_current: int
    supply_target: int
    blocking_reasons: list[str]
    route_usage: list[DedicatedPoolRouteUsageResponse]
    members: list[DedicatedMemberResponse]


def _member_response(
    member: DedicatedPoolMember, *, now: datetime
) -> DedicatedMemberResponse:
    verified = member.last_ready_verified_at
    if verified is not None and verified.tzinfo is None:
        verified = verified.replace(tzinfo=timezone.utc)
    evidence_age = (
        max(0, int((now - verified).total_seconds()))
        if verified is not None
        else None
    )
    global_default = (
        get_config()
        .polardb.tenant_provisioning.delete_cooldown_duration_hours
    )
    if member.delete_cooldown_duration_hours is not None:
        cooldown = member.delete_cooldown_duration_hours
        cooldown_source = "member"
    elif member.pool.delete_cooldown_duration_hours is not None:
        cooldown = member.pool.delete_cooldown_duration_hours
        cooldown_source = "pool"
    else:
        cooldown = global_default
        cooldown_source = "global"
    unallocated = member.allocated_resource_id is None
    failure_code, failure_detail, failure_occurred_at, failure_operation = (
        _member_failure(member.failure_reason)
    )
    preparation_mode = (
        get_config()
        .polardb.tenant_provisioning.dedicated_pool_preparation_mode
    )
    retry_paused_at_openapi_boundary = (
        preparation_mode == "openapi_only"
        and member.preparation_step == DedicatedPreparationStep.OPENAPI_READY
    )
    return DedicatedMemberResponse(
        id=member.id,
        instance_id=member.instance_id,
        allocated_resource_id=member.allocated_resource_id,
        status=member.status.value,
        readiness_status=member.readiness_status.value,
        preparation_step=member.preparation_step.value,
        cloud_request_id=member.cloud_request_id,
        last_ready_verified_at=member.last_ready_verified_at,
        readiness_evidence_age_seconds=evidence_age,
        delete_cooldown_duration_hours=cooldown,
        delete_cooldown_source=cooldown_source,
        failure_reason=failure_code,
        failure_detail=failure_detail,
        failure_occurred_at=failure_occurred_at,
        failure_operation=failure_operation,
        actions={
            "retry": member.status
            in {
                DedicatedMemberStatus.REPLENISHING,
                DedicatedMemberStatus.QUARANTINED,
            }
            and not retry_paused_at_openapi_boundary,
            "quarantine": member.status
            in {
                DedicatedMemberStatus.AVAILABLE,
                DedicatedMemberStatus.REPLENISHING,
            },
            "destroy": unallocated
            and member.status
            in {
                DedicatedMemberStatus.AVAILABLE,
                DedicatedMemberStatus.REPLENISHING,
                DedicatedMemberStatus.QUARANTINED,
            },
        },
    )


def _member_failure(
    value: str | None,
) -> tuple[str | None, str | None, datetime | None, str | None]:
    if value is None:
        return None, None, None, None
    if _SAFE_FAILURE_CODE.fullmatch(value):
        return value, None, None, None
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return "DEDICATED_MEMBER_FAILURE", None, None, None
    if not isinstance(payload, dict):
        return "DEDICATED_MEMBER_FAILURE", None, None, None
    code = safe_error_code(payload.get("code"))
    if code is None or not _SAFE_FAILURE_CODE.fullmatch(code):
        return "DEDICATED_MEMBER_FAILURE", None, None, None
    detail = safe_error_detail(payload.get("detail"))
    operation = safe_error_code(payload.get("operation"))
    occurred_at = None
    occurred_at_value = payload.get("occurred_at")
    if isinstance(occurred_at_value, str):
        try:
            occurred_at = datetime.fromisoformat(
                occurred_at_value.replace("Z", "+00:00")
            )
        except ValueError:
            occurred_at = None
    return code, detail, occurred_at, operation


def _pool_response(
    view: DedicatedPoolView,
    readiness: DedicatedReadiness,
    *,
    route_usage: list[DedicatedPoolRouteUsageResponse] | None = None,
) -> DedicatedPoolResponse:
    pool = view.pool
    now = utc_now()
    effective_cooldown = (
        pool.delete_cooldown_duration_hours
        if pool.delete_cooldown_duration_hours is not None
        else get_config()
        .polardb.tenant_provisioning.delete_cooldown_duration_hours
    )
    profile_status = classify_legacy_purchase_config(
        pool.purchase_config_json
    )
    supply = pool_supply_snapshot(
        view,
        readiness,
        purchase_profile_status=profile_status,
    )
    return DedicatedPoolResponse(
        id=pool.id,
        name=pool.name,
        status=pool.status.value,
        target_size=pool.target_size,
        max_total_members=pool.max_total_members,
        max_member_purchases_per_hour=pool.max_member_purchases_per_hour,
        max_create_requests_per_agent_per_hour=(
            pool.max_create_requests_per_agent_per_hour
        ),
        max_delete_requests_per_agent_per_hour=(
            pool.max_delete_requests_per_agent_per_hour
        ),
        purchase_profile_id=pool.purchase_profile_id,
        purchase_profile_revision=pool.purchase_profile_revision,
        purchase_profile_status=profile_status,
        storage_type=pool.storage_type,
        region_id=pool.region_id,
        vpc_id=pool.vpc_id,
        vswitch_id=pool.vswitch_id,
        zone_id=pool.zone_id,
        security_ip_list=pool.security_ip_list,
        reclaim_policy=pool.reclaim_policy.value,
        lifecycle_admin_policy=pool.lifecycle_admin_policy.value,
        permission_template_revision_id=pool.permission_template_revision_id,
        delete_cooldown_duration_hours=pool.delete_cooldown_duration_hours,
        effective_delete_cooldown_duration_hours=effective_cooldown,
        available_health_check_interval_seconds=(
            pool.available_health_check_interval_seconds
        ),
        available_health_stale_after_seconds=(
            pool.available_health_stale_after_seconds
        ),
        config_revision=pool.config_revision,
        allocatable=view.capacity.allocatable,
        planning=view.capacity.planning,
        billable_total=view.capacity.billable_total,
        surplus=view.surplus,
        supply_state=supply.state.value,
        supply_current=supply.current,
        supply_target=supply.target,
        blocking_reasons=list(supply.blocking_reasons),
        route_usage=route_usage or [],
        members=[_member_response(member, now=now) for member in pool.members],
    )


async def _pool_route_usage(
    session: AsyncSession,
    *,
    pool_id: str,
) -> list[DedicatedPoolRouteUsageResponse]:
    bindings = list(
        (
            await session.scalars(
                select(AgentProvisioningBinding)
                .join(
                    ProvisioningBackend,
                    ProvisioningBackend.id
                    == AgentProvisioningBinding.backend_id,
                )
                .where(ProvisioningBackend.dedicated_pool_id == pool_id)
                .order_by(
                    AgentProvisioningBinding.enabled.desc(),
                    AgentProvisioningBinding.routing_order.is_(None),
                    AgentProvisioningBinding.routing_order,
                    AgentProvisioningBinding.created_at,
                    AgentProvisioningBinding.id,
                )
            )
        ).all()
    )
    return [
        DedicatedPoolRouteUsageResponse(
            agent_id=binding.agent_id,
            agent_name=binding.agent.name,
            binding_id=binding.id,
            enabled=binding.enabled,
            routing_order=binding.routing_order,
            role=(
                "paused"
                if not binding.enabled
                else "primary"
                if binding.routing_order == 0
                else "fallback"
            ),
        )
        for binding in bindings
    ]


def _pool_error(error: DedicatedPoolServiceError) -> HTTPException:
    if isinstance(error, DedicatedPoolNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, DedicatedPoolConfigurationConflict):
        return HTTPException(
            status_code=409,
            detail={
                "code": "POOL_CONFIGURATION_CONFLICT",
                "message": str(error),
            },
        )
    if isinstance(error, DedicatedPoolStateConflict):
        return HTTPException(
            status_code=409,
            detail={"code": "RESOURCE_STATE_CONFLICT", "message": str(error)},
        )
    code = (
        "NETWORK_CHANGE_CONFIRMATION_REQUIRED"
        if isinstance(error, DedicatedPoolNetworkConfirmationRequired)
        else "INVALID_ARGUMENT"
    )
    return HTTPException(
        status_code=422,
        detail={"code": code, "message": str(error)},
    )


async def _audit_pool(
    session: AsyncSession,
    *,
    admin: User,
    action: str,
    target_id: str,
) -> None:
    await log_audit(
        session,
        user_id=admin.id,
        action=action,
        status=AuditStatus.SUCCESS,
        target_type="dedicated_pool",
        target_id=target_id,
        required=True,
        commit=False,
    )


@router.get("", response_model=list[DedicatedPoolResponse])
async def list_dedicated_pools(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    pools = list(
        (
            await session.scalars(
                select(DedicatedPool).order_by(DedicatedPool.name)
            )
        ).all()
    )
    readiness = await aggregate_readiness(
        session,
        config=get_config(),
    )
    return [
        _pool_response(await dedicated_pool_view(session, pool), readiness)
        for pool in pools
    ]


@router.get("/readiness")
async def get_dedicated_readiness(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    readiness = await aggregate_readiness(
        session,
        config=get_config(),
    )
    return {
        "worker": {
            "configured": get_config()
            .polardb.tenant_provisioning.dedicated_pool_enabled,
            "active_worker_count": readiness.worker.active_worker_count,
            "last_heartbeat_at": readiness.worker.last_heartbeat_at,
        },
        "aliyun_access": {
            "configured": readiness.aliyun_access.configured,
            "validated": readiness.aliyun_access.validated,
            "credential_mode": readiness.aliyun_access.credential_mode,
        },
        "purchase_profile": {
            "valid": readiness.purchase_profile.valid,
            "profile_id": readiness.purchase_profile.profile_id,
            "revision": readiness.purchase_profile.revision,
            "default_storage_type": (
                readiness.purchase_profile.default_storage_type
            ),
            "supported_storage_types": list(
                readiness.purchase_profile.supported_storage_types
            ),
        },
        "permission_template": {
            "valid": readiness.permission_template.valid,
            "default_revision_id": (
                readiness.permission_template.default_revision_id
            ),
        },
        "simulation_mode": readiness.simulation_mode,
        "preparation_mode": get_config()
        .polardb.tenant_provisioning.dedicated_pool_preparation_mode,
        "blocking_reasons": list(readiness.blocking_reasons),
    }


@router.get("/purchase-profile")
async def get_purchase_profile(_admin: User = Depends(require_admin)):
    profile = AGENTIC_DEDICATED_PROFILE
    return {
        "profile_id": profile.profile_id,
        "revision": profile.revision,
        "default_storage_type": profile.default_storage_type,
        "supported_storage_types": list(profile.supported_storage_types),
        "fixed_parameters": dict(profile.fixed_parameters),
    }


@router.get("/{pool_id}", response_model=DedicatedPoolResponse)
async def get_dedicated_pool(
    pool_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    pool = await session.get(DedicatedPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Dedicated pool not found")
    readiness = await aggregate_readiness(
        session,
        config=get_config(),
    )
    return _pool_response(
        await dedicated_pool_view(session, pool),
        readiness,
        route_usage=await _pool_route_usage(session, pool_id=pool_id),
    )


def _reject_raw_purchase_config(
    body: CreateDedicatedPoolRequest | UpdateDedicatedPoolRequest,
) -> None:
    if body.purchase_config is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PURCHASE_CONFIG_NOT_SUPPORTED",
                "field": "body.purchase_config",
                "message": "Raw purchase_config is not supported",
            },
        )


@router.post(
    "",
    response_model=DedicatedPoolResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_pool(
    body: CreateDedicatedPoolRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    _reject_raw_purchase_config(body)
    try:
        pool = await create_dedicated_pool(
            session,
            values=body.model_dump(exclude={"purchase_config"}),
        )
        await _audit_pool(
            session,
            admin=admin,
            action="dedicated_pool.create",
            target_id=pool.id,
        )
        await session.commit()
        await session.refresh(
            pool, attribute_names=["members", "provisioning_backend"]
        )
        return _pool_response(
            await dedicated_pool_view(session, pool),
            await aggregate_readiness(
                session,
                config=get_config(),
            ),
        )
    except DedicatedPoolServiceError as error:
        await session.rollback()
        raise _pool_error(error) from None
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Dedicated pool already exists")


@router.patch(
    "/{pool_id}", response_model=DedicatedPoolResponse
)
async def update_pool(
    pool_id: str,
    body: UpdateDedicatedPoolRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    _reject_raw_purchase_config(body)
    metadata_fields = {
        "expected_config_revision",
        "network_change_confirmed",
    }
    changes: dict[str, Any] = {}
    for field in body.model_fields_set - metadata_fields:
        value = getattr(body, field)
        if value is None and field not in {
            "delete_cooldown_duration_hours",
            "security_ip_list",
        }:
            raise HTTPException(
                status_code=422, detail=f"{field} cannot be null"
            )
        changes[field] = value
    if not changes:
        raise HTTPException(status_code=422, detail="No pool changes supplied")
    try:
        pool = await update_dedicated_pool(
            session,
            pool_id=pool_id,
            changes=changes,
            expected_config_revision=body.expected_config_revision,
            network_change_confirmed=body.network_change_confirmed,
        )
        await _audit_pool(
            session,
            admin=admin,
            action="dedicated_pool.update",
            target_id=pool.id,
        )
        await session.commit()
        return _pool_response(
            await dedicated_pool_view(session, pool),
            await aggregate_readiness(
                session,
                config=get_config(),
            ),
        )
    except DedicatedPoolServiceError as error:
        await session.rollback()
        raise _pool_error(error) from None


@router.post(
    "/{pool_id}/purchase-profile/upgrade",
    response_model=DedicatedPoolResponse,
)
async def upgrade_pool_purchase_profile(
    pool_id: str,
    body: UpgradePurchaseProfileRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        pool = await upgrade_purchase_profile(
            session,
            pool_id=pool_id,
            expected_config_revision=body.expected_config_revision,
        )
        await _audit_pool(
            session,
            admin=admin,
            action="dedicated_pool.purchase_profile.upgrade",
            target_id=pool.id,
        )
        await session.commit()
        await session.refresh(
            pool, attribute_names=["members", "provisioning_backend"]
        )
        return _pool_response(
            await dedicated_pool_view(session, pool),
            await aggregate_readiness(
                session,
                config=get_config(),
            ),
        )
    except DedicatedPoolServiceError as error:
        await session.rollback()
        raise _pool_error(error) from None


@router.post("/{pool_id}/drain", response_model=DedicatedPoolResponse)
async def drain_pool(
    pool_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        pool = await drain_dedicated_pool(session, pool_id)
        await _audit_pool(
            session,
            admin=admin,
            action="dedicated_pool.drain",
            target_id=pool.id,
        )
        await session.commit()
        return _pool_response(
            await dedicated_pool_view(session, pool),
            await aggregate_readiness(
                session,
                config=get_config(),
            ),
        )
    except DedicatedPoolServiceError as error:
        await session.rollback()
        raise _pool_error(error) from None


@router.post(
    "/{pool_id}/members",
    response_model=DedicatedMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_member(
    pool_id: str,
    body: RegisterDedicatedMemberRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        member = await register_external_member(
            session,
            pool_id=pool_id,
            instance_id=body.instance_id,
            lifecycle_credential_id=body.lifecycle_credential_id,
        )
        await _audit_pool(
            session,
            admin=admin,
            action="dedicated_pool.member.register",
            target_id=member.id,
        )
        await session.commit()
        return _member_response(member, now=utc_now())
    except DedicatedPoolServiceError as error:
        await session.rollback()
        raise _pool_error(error) from None


@router.patch(
    "/{pool_id}/members/{member_id}",
    response_model=DedicatedMemberResponse,
)
async def update_member(
    pool_id: str,
    member_id: str,
    body: UpdateDedicatedMemberRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if "delete_cooldown_duration_hours" not in body.model_fields_set:
        raise HTTPException(status_code=422, detail="No member changes supplied")
    try:
        member = await update_member_delete_cooldown(
            session,
            member_id=member_id,
            delete_cooldown_duration_hours=(
                body.delete_cooldown_duration_hours
            ),
        )
        if member.pool_id != pool_id:
            raise DedicatedPoolNotFound("Dedicated pool member not found")
        await _audit_pool(
            session,
            admin=admin,
            action="dedicated_pool.member.update",
            target_id=member.id,
        )
        await session.commit()
        return _member_response(member, now=utc_now())
    except DedicatedPoolServiceError as error:
        await session.rollback()
        raise _pool_error(error) from None


@router.post(
    "/{pool_id}/members/{member_id}/actions/{action}",
    response_model=DedicatedMemberResponse,
)
async def member_action(
    pool_id: str,
    member_id: str,
    action: Literal["retry", "quarantine", "destroy"],
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        member = await set_member_action(
            session,
            member_id=member_id,
            action=action,
            allow_data_plane=(
                get_config()
                .polardb.tenant_provisioning
                .dedicated_pool_preparation_mode
                == "full"
            ),
        )
        if member.pool_id != pool_id:
            raise DedicatedPoolNotFound("Dedicated pool member not found")
        await _audit_pool(
            session,
            admin=admin,
            action=f"dedicated_pool.member.{action}",
            target_id=member.id,
        )
        await session.commit()
        supervisor = getattr(
            request.app.state,
            "dedicated_pool_worker_supervisor",
            None,
        )
        if supervisor is not None:
            supervisor.request_run()
        return _member_response(member, now=utc_now())
    except DedicatedPoolServiceError as error:
        await session.rollback()
        raise _pool_error(error) from None
