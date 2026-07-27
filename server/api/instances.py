from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.credentials import instance_router as credentials_router
from server.auth.dependencies import require_admin
from server.core import (
    binding_manager,
    credential_service,
    instance_connection,
    instance_manager,
    provisioning_backend_service,
)
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.models import (
    AllocationMode,
    InstanceEngine,
    BindingOrigin,
    Instance,
    InstanceCredential,
    InstanceStatus,
    InstanceTopology,
    Permission,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    AuditStatus,
    User,
)

router = APIRouter(prefix="/instances", tags=["instances"])

router.include_router(credentials_router)


def normalize_usage(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


class InstanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    cluster_id: str
    name: str
    usage: str | None
    engine: InstanceEngine
    topology: InstanceTopology
    allocation_mode: AllocationMode
    region: str | None
    host: str | None
    port: int | None
    status: InstanceStatus
    owner_user_id: str | None
    health: "InstanceHealthResponse | None"
    binding_counts: "InstanceBindingCounts"

    @classmethod
    def from_model(cls, instance: Instance) -> "InstanceResponse":
        backend = instance.provisioning_backend
        health = backend.health if backend is not None else None
        return cls(
            id=instance.id,
            cluster_id=instance.cluster_id,
            name=instance.name,
            usage=instance.usage,
            engine=instance.engine,
            topology=instance.topology,
            allocation_mode=instance.allocation_mode,
            region=instance.region,
            host=instance.host,
            port=instance.port,
            status=instance.status,
            owner_user_id=instance.owner_user_id,
            health=(
                InstanceHealthResponse.from_model(health)
                if health is not None
                else None
            ),
            binding_counts=InstanceBindingCounts(
                users=len(instance.user_bindings),
                departments=len(instance.department_bindings),
                agents=len(instance.agent_bindings),
            ),
        )

    @classmethod
    def from_summary(cls, row: dict) -> "InstanceResponse":
        health = None
        if row["health_checked_at"] is not None:
            health = InstanceHealthResponse(
                healthy=row["health_healthy"],
                checked_at=row["health_checked_at"],
                consecutive_failures=row["health_consecutive_failures"],
                error_code=row["health_error_code"],
            )
        return cls(
            id=row["id"],
            cluster_id=row["cluster_id"],
            name=row["name"],
            usage=row["usage"],
            engine=row["engine"],
            topology=row["topology"],
            allocation_mode=row["allocation_mode"],
            region=row["region"],
            host=row["host"],
            port=row["port"],
            status=row["status"],
            owner_user_id=row["owner_user_id"],
            health=health,
            binding_counts=InstanceBindingCounts(
                users=row["user_binding_count"],
                departments=row["department_binding_count"],
                agents=row["agent_binding_count"],
            ),
        )


class InstanceHealthResponse(BaseModel):
    healthy: bool
    checked_at: datetime
    consecutive_failures: int
    error_code: str | None

    @classmethod
    def from_model(
        cls, health: ProvisioningBackendHealth
    ) -> "InstanceHealthResponse":
        return cls.model_validate(health, from_attributes=True)


class InstanceBindingCounts(BaseModel):
    users: int = Field(ge=0)
    departments: int = Field(ge=0)
    agents: int = Field(ge=0)


class InstanceListResponse(BaseModel):
    items: list[InstanceResponse]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)


class InstanceConnectionFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=3306, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("host", "username")
    @classmethod
    def strip_required_connection_value(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value cannot be blank")
        return stripped


class RegisterInstanceRequest(InstanceConnectionFields):
    cluster_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    usage: str | None = Field(default=None, max_length=1024)
    engine: InstanceEngine
    topology: InstanceTopology
    region: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("cluster_id", "name")
    @classmethod
    def strip_required_identity_value(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value cannot be blank")
        return stripped

    @field_validator("usage", mode="before")
    @classmethod
    def strip_optional_usage(cls, value: str | None) -> str | None:
        return normalize_usage(value)

    @field_validator("region")
    @classmethod
    def strip_optional_region(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value cannot be blank")
        return stripped


class TestInstanceConnectionRequest(InstanceConnectionFields):
    topology: InstanceTopology


class BindUserRequest(BaseModel):
    user_id: str
    permission: str = "readwrite"


class BindDepartmentRequest(BaseModel):
    department_id: str
    tenant_name: str | None = None
    default_permission: str = "readwrite"


@router.get("", response_model=InstanceListResponse)
async def list_instances(
    engine: InstanceEngine | None = None,
    topology: InstanceTopology | None = None,
    allocation_mode: AllocationMode | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows, total = await instance_manager.list_instance_summaries(
        session,
        engine=engine,
        topology=topology,
        allocation_mode=allocation_mode,
        offset=offset,
        limit=limit,
    )
    return InstanceListResponse(
        items=[InstanceResponse.from_summary(row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post("", response_model=InstanceResponse, status_code=201)
async def register_instance(
    body: RegisterInstanceRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        await instance_connection.test_mysql_connection(
            host=body.host,
            port=body.port,
            username=body.username,
            password=body.password,
            require_multitenant=(
                body.topology == InstanceTopology.MULTITENANT
            ),
        )
        inst = await instance_manager.register_instance_with_credential(
            session, cluster_id=body.cluster_id, name=body.name,
            usage=body.usage,
            engine=body.engine,
            topology=body.topology,
            region=body.region,
            host=body.host, port=body.port,
            username=body.username,
            password=body.password,
            created_by_user_id=admin.id,
        )
    except instance_connection.ConnectionTestError as e:
        await session.rollback()
        raise HTTPException(
            status_code=422,
            detail={"code": e.code, "message": str(e)},
        ) from e
    except ValueError as e:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    return InstanceResponse.from_model(inst)


@router.post("/test-connection")
async def test_instance_connection(
    body: TestInstanceConnectionRequest,
    _admin: User = Depends(require_admin),
):
    try:
        await instance_connection.test_mysql_connection(
            host=body.host,
            port=body.port,
            username=body.username,
            password=body.password,
            require_multitenant=(
                body.topology == InstanceTopology.MULTITENANT
            ),
        )
    except instance_connection.ConnectionTestError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return {"ok": True}


@router.get("/{instance_id}", response_model=InstanceResponse)
async def get_instance(
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    inst = await instance_manager.get_instance(session, instance_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    return InstanceResponse.from_model(inst)


class UpdateInstanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    usage: str | None = Field(default=None, max_length=1024)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    region: str | None = Field(default=None, min_length=1, max_length=64)
    test_credential_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=36,
    )

    @field_validator("usage", mode="before")
    @classmethod
    def strip_optional_usage(cls, value: str | None) -> str | None:
        return normalize_usage(value)

    @model_validator(mode="after")
    def validate_update(self) -> "UpdateInstanceRequest":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        if any(
            getattr(self, field_name) is None
            for field_name in self.model_fields_set
            if field_name not in {"usage", "test_credential_id"}
        ):
            raise ValueError("Update fields cannot be null")
        return self


@router.put("/{instance_id}", response_model=InstanceResponse)
async def update_instance(
    instance_id: str,
    body: UpdateInstanceRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    inst = await instance_manager.get_instance(session, instance_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    merged_host = body.host if "host" in body.model_fields_set else inst.host
    merged_port = body.port if "port" in body.model_fields_set else inst.port
    if (merged_host is None) != (merged_port is None):
        raise HTTPException(
            status_code=422,
            detail="host and port must be configured together",
        )
    endpoint_changed = (
        body.host is not None
        and body.host != inst.host
    ) or (
        body.port is not None
        and body.port != inst.port
    )
    backend = None
    if endpoint_changed:
        if body.test_credential_id is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "TEST_CREDENTIAL_REQUIRED",
                    "message": (
                        "Select an active credential to validate the "
                        "proposed endpoint."
                    ),
                },
            )
        backend = (
            await session.execute(
                select(ProvisioningBackend)
                .where(ProvisioningBackend.instance_id == instance_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        assert merged_host is not None
        assert merged_port is not None
        try:
            await credential_service.test_stored_credential_connection(
                session,
                instance=inst,
                credential_id=body.test_credential_id,
                host=merged_host,
                port=merged_port,
                required_credential_id=(
                    backend.admin_credential_id
                    if backend is not None
                    else None
                ),
            )
        except credential_service.CredentialValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "TEST_CREDENTIAL_INVALID",
                    "message": str(exc),
                },
            ) from exc
        except instance_connection.ConnectionTestError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    if body.name is not None:
        inst.name = body.name
    if "usage" in body.model_fields_set:
        inst.usage = body.usage
    if body.host is not None:
        inst.host = body.host
    if body.port is not None:
        inst.port = body.port
    if body.region is not None:
        inst.region = body.region
    await session.flush()
    if endpoint_changed:
        if backend is not None:
            await provisioning_backend_service.bump_backend_config_revision(
                session, backend
            )
    await session.commit()
    await session.refresh(inst)
    return InstanceResponse.from_model(inst)


class TestStoredCredentialConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    credential_id: str = Field(min_length=1, max_length=36)


@router.post("/{instance_id}/test-connection")
async def test_stored_instance_connection(
    instance_id: str,
    body: TestStoredCredentialConnectionRequest,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    inst = await instance_manager.get_instance(session, instance_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    backend = (
        await session.execute(
            select(ProvisioningBackend).where(
                ProvisioningBackend.instance_id == instance_id
            )
        )
    ).scalar_one_or_none()
    try:
        await credential_service.test_stored_credential_connection(
            session,
            instance=inst,
            credential_id=body.credential_id,
            host=body.host,
            port=body.port,
            required_credential_id=(
                backend.admin_credential_id if backend is not None else None
            ),
        )
    except credential_service.CredentialValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "TEST_CREDENTIAL_INVALID", "message": str(exc)},
        ) from exc
    except instance_connection.ConnectionTestError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return {"ok": True}


@router.delete("/{instance_id}", status_code=204)
async def remove_instance(
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        await instance_manager.remove_instance(session, instance_id, commit=False)
        await log_audit(
            session,
            user_id=admin.id,
            action="instance.remove",
            status=AuditStatus.SUCCESS,
            user_name=admin.display_name,
            target_type="instance",
            target_id=instance_id,
            required=True,
            commit=False,
        )
        await session.commit()
    except instance_manager.InstanceRemovalConflict as e:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(e)) from e
    except IntegrityError as e:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Instance cannot be removed while it is referenced",
        ) from e
    except ValueError as e:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        await session.rollback()
        raise


@router.post("/discover")
async def discover_instances(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    clusters = await instance_manager.discover_instances(session)
    return {"clusters": clusters}


@router.post("/{instance_id}/bind-user", status_code=201)
async def bind_user(
    instance_id: str,
    body: BindUserRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        binding = await binding_manager.bind_user_to_instance(
            session, body.user_id, instance_id, Permission(body.permission)
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": binding.id, "user_id": binding.user_id, "instance_id": binding.instance_id}


@router.delete("/{instance_id}/unbind-user/{user_id}", status_code=204)
async def unbind_user(
    instance_id: str,
    user_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        await binding_manager.unbind_user_from_instance(session, user_id, instance_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{instance_id}/bind-department", status_code=201)
async def bind_department(
    instance_id: str,
    body: BindDepartmentRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        binding = await binding_manager.bind_department_to_instance(
            session, body.department_id, instance_id, body.tenant_name, Permission(body.default_permission)
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": binding.id, "department_id": binding.department_id, "instance_id": binding.instance_id}


@router.delete("/{instance_id}/unbind-department/{department_id}", status_code=204)
async def unbind_department(
    instance_id: str,
    department_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        await binding_manager.unbind_department_from_instance(session, department_id, instance_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{instance_id}/retry-provision")
async def retry_provision(
    instance_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.core.quota_manager import reincrement_quota_for_retry
    from server.core.provisioner import _launch_provisioning_task

    inst = await session.get(Instance, instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found")
    if inst.status != InstanceStatus.FAILED:
        raise HTTPException(400, "Only FAILED instances can be retried")

    error = await reincrement_quota_for_retry(session, inst)
    if error:
        raise HTTPException(409, detail=error)

    inst.status = InstanceStatus.CREATING
    await session.commit()

    session_factory = getattr(request.app.state, 'session_factory', None)
    background_tasks = getattr(request.app.state, 'background_tasks', None)
    if session_factory and background_tasks is not None and inst.owner_user_id:
        _launch_provisioning_task(inst.id, inst.owner_user_id, session_factory, background_tasks)
    return {"instance_id": inst.id, "status": "creating"}


@router.delete("/{instance_id}/failed", status_code=204)
async def delete_failed_instance(
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from sqlalchemy import select as sa_select, update as sa_update

    from server.core.quota_manager import decrement_quota
    inst = await session.get(Instance, instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found")
    if inst.status != InstanceStatus.FAILED:
        raise HTTPException(400, "Only FAILED instances can be deleted via this endpoint")

    cluster_id = inst.cluster_id

    await decrement_quota(session, inst)

    # Clear ORM-owned relationships once so the later Instance delete does not
    # schedule the same delete-orphan rows a second time.
    inst.user_bindings.clear()
    inst.department_bindings.clear()
    await session.flush()

    for credential in (await session.execute(
        sa_select(InstanceCredential).where(
            InstanceCredential.instance_id == instance_id
        )
    )).scalars().all():
        await session.delete(credential)

    # Clear default_instance_id references so User FK doesn't block delete
    await session.execute(
        sa_update(User)
        .where(User.default_instance_id == instance_id)
        .values(default_instance_id=None)
    )

    await session.delete(inst)
    await session.commit()

    # Cloud cleanup last, so a cloud-side failure doesn't leave orphan DB state.
    if cluster_id and not cluster_id.startswith(("pending-", "pool-pending-")):
        try:
            from server.aliyun.polardb_client import get_polardb_client_async
            client = await get_polardb_client_async(session)
            await client.delete_cluster(cluster_id)
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to delete cloud cluster %s, may need manual cleanup",
                cluster_id,
            )


class CreateTenantRequest(BaseModel):
    user_id: str


@router.get("/{instance_id}/tenants")
async def list_tenants(
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.models import UserInstanceBinding

    inst = await session.get(Instance, instance_id)
    if not inst or inst.topology != InstanceTopology.MULTITENANT:
        raise HTTPException(404, "Multitenant instance not found")

    bindings = (await session.execute(
        select(UserInstanceBinding).where(
            UserInstanceBinding.instance_id == instance_id,
            UserInstanceBinding.credential_id.is_not(None),
        )
    )).scalars().all()

    result = []
    for binding in bindings:
        user = binding.user
        result.append({
            "user_id": binding.user_id,
            "display_name": user.display_name if user else None,
            "tenant_name": binding.tenant_name,
            "provisioning_step": (
                binding.provisioning_step.value
                if binding.provisioning_step
                else None
            ),
            "created_at": (
                binding.created_at.isoformat() if binding.created_at else None
            ),
        })
    return result


@router.post("/{instance_id}/tenants", status_code=201)
async def create_tenant(
    instance_id: str,
    body: CreateTenantRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.core.tenant_provisioner import ensure_tenant
    from server.models import User as UserModel

    inst = await session.get(Instance, instance_id)
    if not inst or inst.topology != InstanceTopology.MULTITENANT:
        raise HTTPException(404, "Multitenant instance not found")
    user = await session.get(UserModel, body.user_id)
    if not user:
        raise HTTPException(404, "User not found")
    try:
        binding = await ensure_tenant(
            user, inst, session, origin=BindingOrigin.ADMIN
        )
    except Exception as e:
        raise HTTPException(500, f"Tenant provisioning failed: {e}")
    return {
        "user_id": binding.user_id,
        "tenant_name": binding.tenant_name,
        "provisioning_step": (
            binding.provisioning_step.value if binding.provisioning_step else None
        ),
    }


@router.post("/{instance_id}/tenants/{user_id}/retry")
async def retry_tenant(
    instance_id: str,
    user_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.core.tenant_provisioner import ensure_tenant
    from server.models import User as UserModel

    inst = await session.get(Instance, instance_id)
    if not inst or inst.topology != InstanceTopology.MULTITENANT:
        raise HTTPException(404, "Multitenant instance not found")
    user = await session.get(UserModel, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    try:
        binding = await ensure_tenant(
            user, inst, session, origin=BindingOrigin.ADMIN
        )
    except Exception as e:
        raise HTTPException(500, f"Tenant provisioning retry failed: {e}")
    return {
        "user_id": binding.user_id,
        "tenant_name": binding.tenant_name,
        "provisioning_step": (
            binding.provisioning_step.value if binding.provisioning_step else None
        ),
    }


@router.delete("/{instance_id}/tenants/{user_id}", status_code=204)
async def delete_tenant(
    instance_id: str,
    user_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.models import UserInstanceBinding

    binding = (await session.execute(
        select(UserInstanceBinding).where(
            UserInstanceBinding.instance_id == instance_id,
            UserInstanceBinding.user_id == user_id,
        )
    )).scalar_one_or_none()
    if binding is None:
        raise HTTPException(404, "Tenant not found")
    credential = (
        await session.get(InstanceCredential, binding.credential_id)
        if binding.credential_id
        else None
    )
    await session.delete(binding)
    await session.flush()
    if credential is not None:
        await session.delete(credential)
    await session.commit()
