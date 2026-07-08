from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core import instance_manager, binding_manager
from server.db.engine import get_session
from server.models import User, Instance, InstanceType, InstanceStatus, Permission

router = APIRouter(prefix="/instances", tags=["instances"])


class InstanceResponse(BaseModel):
    id: str
    cluster_id: str
    name: str
    type: str
    region: str | None
    host: str | None
    port: int | None
    status: str
    owner_user_id: str | None


class RegisterInstanceRequest(BaseModel):
    cluster_id: str
    name: str
    type: str = "shared"
    region: str | None = None
    host: str | None = None
    port: int | None = None


class BindUserRequest(BaseModel):
    user_id: str
    permission: str = "readwrite"


class BindDepartmentRequest(BaseModel):
    department_id: str
    tenant_name: str | None = None
    default_permission: str = "readwrite"


@router.get("", response_model=list[InstanceResponse])
async def list_instances(
    type: str | None = None,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance_type_enum = None
    if type is not None:
        try:
            instance_type_enum = InstanceType(type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid instance type: {type}")
    instances = await instance_manager.list_instances(session, instance_type=instance_type_enum)
    return [InstanceResponse(
        id=i.id, cluster_id=i.cluster_id, name=i.name, type=i.type.value,
        region=i.region, host=i.host, port=i.port, status=i.status.value,
        owner_user_id=i.owner_user_id,
    ) for i in instances]


@router.post("", response_model=InstanceResponse, status_code=201)
async def register_instance(
    body: RegisterInstanceRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        inst = await instance_manager.register_instance(
            session, cluster_id=body.cluster_id, name=body.name,
            instance_type=InstanceType(body.type), region=body.region,
            host=body.host, port=body.port,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return InstanceResponse(
        id=inst.id, cluster_id=inst.cluster_id, name=inst.name, type=inst.type.value,
        region=inst.region, host=inst.host, port=inst.port, status=inst.status.value,
        owner_user_id=inst.owner_user_id,
    )


@router.get("/{instance_id}", response_model=InstanceResponse)
async def get_instance(
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    inst = await instance_manager.get_instance(session, instance_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    return InstanceResponse(
        id=inst.id, cluster_id=inst.cluster_id, name=inst.name, type=inst.type.value,
        region=inst.region, host=inst.host, port=inst.port, status=inst.status.value,
        owner_user_id=inst.owner_user_id,
    )


class UpdateInstanceRequest(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    region: str | None = None


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
    if body.name is not None:
        inst.name = body.name
    if body.host is not None:
        inst.host = body.host
    if body.port is not None:
        inst.port = body.port
    if body.region is not None:
        inst.region = body.region
    await session.commit()
    await session.refresh(inst)
    return InstanceResponse(
        id=inst.id, cluster_id=inst.cluster_id, name=inst.name, type=inst.type.value,
        region=inst.region, host=inst.host, port=inst.port, status=inst.status.value,
        owner_user_id=inst.owner_user_id,
    )


@router.delete("/{instance_id}", status_code=204)
async def remove_instance(
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        await instance_manager.remove_instance(session, instance_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
    from server.models.binding import UserInstanceBinding, DepartmentInstanceBinding
    from server.models.db_account import DBAccount

    inst = await session.get(Instance, instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found")
    if inst.status != InstanceStatus.FAILED:
        raise HTTPException(400, "Only FAILED instances can be deleted via this endpoint")

    cluster_id = inst.cluster_id

    await decrement_quota(session, inst)

    # Delete related records to avoid FK violations
    for account in (await session.execute(
        sa_select(DBAccount).where(DBAccount.instance_id == instance_id)
    )).scalars().all():
        await session.delete(account)

    for binding in (await session.execute(
        sa_select(UserInstanceBinding).where(UserInstanceBinding.instance_id == instance_id)
    )).scalars().all():
        await session.delete(binding)

    for dept_binding in (await session.execute(
        sa_select(DepartmentInstanceBinding).where(DepartmentInstanceBinding.instance_id == instance_id)
    )).scalars().all():
        await session.delete(dept_binding)

    # Clear default_instance_id references so User FK doesn't block delete
    await session.execute(
        sa_update(User)
        .where(User.default_instance_id == instance_id)
        .values(default_instance_id=None)
    )

    await session.delete(inst)
    await session.commit()

    # Cloud cleanup last, so a cloud-side failure doesn't leave orphan DB state.
    if cluster_id and not cluster_id.startswith("pending-"):
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
    from server.models.db_account import DBAccount, AccountType

    inst = await session.get(Instance, instance_id)
    if not inst or inst.type != InstanceType.MULTITENANT:
        raise HTTPException(404, "Multitenant instance not found")

    accounts = (await session.execute(
        select(DBAccount).where(
            DBAccount.instance_id == instance_id,
            DBAccount.account_type == AccountType.NORMAL,
        )
    )).scalars().all()

    result = []
    for acct in accounts:
        user = acct.user
        result.append({
            "user_id": acct.user_id,
            "display_name": user.display_name if user else None,
            "tenant_name": acct.tenant_name,
            "provisioning_step": acct.provisioning_step.value if acct.provisioning_step else None,
            "created_at": acct.created_at.isoformat() if acct.created_at else None,
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
    if not inst or inst.type != InstanceType.MULTITENANT:
        raise HTTPException(404, "Multitenant instance not found")
    user = await session.get(UserModel, body.user_id)
    if not user:
        raise HTTPException(404, "User not found")
    try:
        account = await ensure_tenant(user, inst, session)
    except Exception as e:
        raise HTTPException(500, f"Tenant provisioning failed: {e}")
    return {
        "user_id": account.user_id,
        "tenant_name": account.tenant_name,
        "provisioning_step": account.provisioning_step.value if account.provisioning_step else None,
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
    if not inst or inst.type != InstanceType.MULTITENANT:
        raise HTTPException(404, "Multitenant instance not found")
    user = await session.get(UserModel, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    try:
        account = await ensure_tenant(user, inst, session)
    except Exception as e:
        raise HTTPException(500, f"Tenant provisioning retry failed: {e}")
    return {
        "user_id": account.user_id,
        "tenant_name": account.tenant_name,
        "provisioning_step": account.provisioning_step.value if account.provisioning_step else None,
    }


@router.delete("/{instance_id}/tenants/{user_id}", status_code=204)
async def delete_tenant(
    instance_id: str,
    user_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.models.db_account import DBAccount

    account = (await session.execute(
        select(DBAccount).where(
            DBAccount.instance_id == instance_id,
            DBAccount.user_id == user_id,
        )
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(404, "Tenant not found")
    await session.delete(account)
    await session.commit()
