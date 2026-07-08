# server/api/pool.py
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core.settings_manager import get_setting
from server.db.engine import get_session
from server.models import Instance, InstanceStatus, InstanceType, User

router = APIRouter(prefix="/pool", tags=["pool"])


@router.get("/status")
async def get_pool_status(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    target = int(await get_setting(session, "pool_target_size", default="0") or "0")
    available = (await session.execute(
        select(func.count()).select_from(Instance).where(Instance.status == InstanceStatus.POOLED)
    )).scalar() or 0
    pool_creating = (await session.execute(
        select(func.count()).select_from(Instance).where(Instance.status == InstanceStatus.POOL_CREATING)
    )).scalar() or 0
    failed = (await session.execute(
        select(func.count()).select_from(Instance).where(
            Instance.status == InstanceStatus.FAILED, Instance.owner_user_id.is_(None),
        )
    )).scalar() or 0
    vpc_id = await get_setting(session, "pool_vpc_id", default="")
    return {
        "target": target, "available": available,
        "pool_creating": pool_creating, "failed": failed,
        "network_ready": bool(vpc_id),
    }


@router.get("/instances")
async def list_pool_instances(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(
        select(Instance).where(
            Instance.status.in_([InstanceStatus.POOLED, InstanceStatus.POOL_CREATING, InstanceStatus.FAILED]),
            Instance.owner_user_id.is_(None),
        )
    )).scalars().all()
    return [{
        "id": r.id, "cluster_id": r.cluster_id, "status": r.status.value,
        "provisioning_step": r.provisioning_step.value if r.provisioning_step else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


class AddPoolInstanceRequest(BaseModel):
    cluster_id: str


@router.post("/instances")
async def add_pool_instance(
    body: AddPoolInstanceRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    existing = (await session.execute(
        select(Instance).where(Instance.cluster_id == body.cluster_id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"Cluster {body.cluster_id} is already registered as instance {existing.id}")
    from server.aliyun.polardb_client import get_polardb_client_async
    client = await get_polardb_client_async(session)
    attr = await client.describe_cluster_attribute(body.cluster_id)
    status = InstanceStatus.POOLED if attr["status"] == "Running" else InstanceStatus.POOL_CREATING
    inst = Instance(
        cluster_id=body.cluster_id,
        name=f"pool-{body.cluster_id[-8:]}",
        type=InstanceType.SHARED,
        status=status,
    )
    session.add(inst)
    await session.commit()
    return {"id": inst.id, "cluster_id": inst.cluster_id, "status": inst.status.value}


@router.delete("/instances/{instance_id}", status_code=204)
async def remove_pool_instance(
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    inst = await session.get(Instance, instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found")
    if inst.status != InstanceStatus.POOLED:
        raise HTTPException(400, "Only POOLED instances can be removed from pool")
    await session.delete(inst)
    await session.commit()


@router.post("/replenish")
async def replenish(
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    target = int(await get_setting(session, "pool_target_size", default="0") or "0")
    if target <= 0:
        raise HTTPException(400, "Set pool_target_size > 0 first")
    from server.core.pool_manager import _replenish_once
    session_factory = getattr(request.app.state, 'session_factory', None)
    background_tasks = getattr(request.app.state, 'background_tasks', None)
    if session_factory is None or background_tasks is None:
        raise HTTPException(500, "Server not fully initialized")
    await _replenish_once(session_factory, background_tasks)
    available = (await session.execute(
        select(func.count()).select_from(Instance).where(Instance.status == InstanceStatus.POOLED)
    )).scalar() or 0
    pool_creating = (await session.execute(
        select(func.count()).select_from(Instance).where(Instance.status == InstanceStatus.POOL_CREATING)
    )).scalar() or 0
    return {"triggered": True, "target": target, "available": available, "pool_creating": pool_creating}
