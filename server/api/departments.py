from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core import binding_manager, department_manager
from server.db.engine import get_session
from server.models import User

router = APIRouter(prefix="/departments", tags=["departments"])


class DepartmentResponse(BaseModel):
    id: str
    name: str
    description: str | None
    max_instances: int | None = None
    agentic_db_cluster_id: str | None = None
    agentic_db_cluster_description: str | None = None


class CreateDepartmentRequest(BaseModel):
    name: str
    description: str | None = None


class UpdateDepartmentRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    max_instances: int | None = None
    agentic_db_cluster_id: str | None = None
    agentic_db_cluster_description: str | None = None


class DepartmentUserResponse(BaseModel):
    id: str
    display_name: str
    email: str | None
    role: str


@router.get("", response_model=list[DepartmentResponse])
async def list_departments(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    depts = await department_manager.list_departments(session)
    return [
        DepartmentResponse(
            id=d.id, name=d.name, description=d.description,
            max_instances=d.max_instances,
            agentic_db_cluster_id=d.agentic_db_cluster_id,
            agentic_db_cluster_description=d.agentic_db_cluster_description,
        )
        for d in depts
    ]


@router.post("", response_model=DepartmentResponse, status_code=201)
async def create_department(
    body: CreateDepartmentRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    dept = await department_manager.create_department(session, body.name, body.description)
    return DepartmentResponse(
        id=dept.id, name=dept.name, description=dept.description,
        max_instances=dept.max_instances,
        agentic_db_cluster_id=dept.agentic_db_cluster_id,
        agentic_db_cluster_description=dept.agentic_db_cluster_description,
    )


@router.put("/{department_id}", response_model=DepartmentResponse)
async def update_department(
    department_id: str,
    body: UpdateDepartmentRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        dept = await department_manager.update_department(session, department_id, body.name, body.description)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if body.max_instances is not None:
        from sqlalchemy import select, func

        from server.models.binding import UserDepartment
        from server.models.instance import (
            AllocationMode,
            Instance,
            InstanceStatus,
        )
        from server.models.quota_counter import QuotaCounter

        dept.max_instances = body.max_instances
        row = await session.execute(
            select(QuotaCounter).where(QuotaCounter.scope == f"dept:{department_id}")
        )
        counter = row.scalar_one_or_none()
        if counter is None:
            count_result = await session.execute(
                select(func.count()).select_from(Instance).join(
                    UserDepartment, Instance.owner_user_id == UserDepartment.user_id
                ).where(
                    UserDepartment.department_id == department_id,
                    Instance.allocation_mode.in_(
                        [
                            AllocationMode.AUTO_PROVISIONED,
                            AllocationMode.POOLED,
                        ]
                    ),
                    Instance.status.in_([InstanceStatus.CREATING, InstanceStatus.ACTIVE, InstanceStatus.STOPPED]),
                )
            )
            current = count_result.scalar() or 0
            counter = QuotaCounter(scope=f"dept:{department_id}", current_count=current, max_limit=body.max_instances)
            session.add(counter)
        else:
            counter.max_limit = body.max_instances
        await session.commit()

    if body.agentic_db_cluster_id is not None:
        dept.agentic_db_cluster_id = body.agentic_db_cluster_id
    if body.agentic_db_cluster_description is not None:
        dept.agentic_db_cluster_description = body.agentic_db_cluster_description
    if body.agentic_db_cluster_id is not None or body.agentic_db_cluster_description is not None:
        await session.commit()
        await session.refresh(dept)

    return DepartmentResponse(
        id=dept.id, name=dept.name, description=dept.description,
        max_instances=dept.max_instances,
        agentic_db_cluster_id=dept.agentic_db_cluster_id,
        agentic_db_cluster_description=dept.agentic_db_cluster_description,
    )


@router.delete("/{department_id}", status_code=204)
async def delete_department(
    department_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        await department_manager.delete_department(session, department_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"code": "DEPARTMENT_NOT_EMPTY", "message": str(e)})


@router.get("/{department_id}/users", response_model=list[DepartmentUserResponse])
async def list_department_users(
    department_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    users = await department_manager.list_department_users(session, department_id)
    return [DepartmentUserResponse(id=u.id, display_name=u.display_name, email=u.email, role=u.role.value) for u in users]


class BindMultitenantInstanceRequest(BaseModel):
    instance_id: str


@router.post("/{department_id}/multitenant-instance", status_code=201)
async def bind_multitenant_instance(
    department_id: str,
    body: BindMultitenantInstanceRequest,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.models import (
        AllocationMode,
        Department,
        DepartmentInstanceBinding,
        Instance,
        InstanceEngine,
        InstanceStatus,
        InstanceTopology,
    )

    dept = (
        await session.execute(
            select(Department)
            .where(Department.id == department_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if dept is None:
        raise HTTPException(404, "Department not found")

    inst = await session.get(Instance, body.instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    if (
        inst.engine != InstanceEngine.POLARDB_MYSQL
        or inst.topology != InstanceTopology.MULTITENANT
        or inst.allocation_mode != AllocationMode.REGISTERED
        or inst.status != InstanceStatus.ACTIVE
    ):
        raise HTTPException(
            400,
            "Only active registered PolarDB MySQL multitenant instances "
            "can be bound",
        )
    existing = (
        await session.execute(
            select(DepartmentInstanceBinding)
            .join(Instance)
            .where(
                DepartmentInstanceBinding.department_id == department_id,
                Instance.topology == InstanceTopology.MULTITENANT,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            400,
            "Department already has a multitenant instance",
        )
    binding = await binding_manager.bind_department_to_instance(
        session,
        department_id,
        inst.id,
    )

    return {
        "instance": {
            "id": inst.id, "cluster_id": inst.cluster_id, "name": inst.name,
            "type": "multitenant", "status": inst.status.value,
            "host": inst.host, "port": inst.port, "region": inst.region,
        },
        "binding": {
            "department_id": department_id, "instance_id": inst.id,
            "default_permission": binding.default_permission.value,
        },
    }


class MtInstanceResponse(BaseModel):
    id: str
    cluster_id: str
    name: str
    host: str | None
    port: int | None
    status: str
    default_permission: str


@router.get("/{department_id}/multitenant-instance", response_model=MtInstanceResponse | None)
async def get_multitenant_instance(
    department_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.models import (
        DepartmentInstanceBinding,
        Instance,
        InstanceTopology,
    )

    result = (await session.execute(
        select(Instance, DepartmentInstanceBinding.default_permission)
        .join(DepartmentInstanceBinding, DepartmentInstanceBinding.instance_id == Instance.id)
        .where(
            DepartmentInstanceBinding.department_id == department_id,
            Instance.topology == InstanceTopology.MULTITENANT,
        )
        .limit(1)
    )).one_or_none()
    if result is None:
        return None
    inst, permission = result
    return MtInstanceResponse(
        id=inst.id, cluster_id=inst.cluster_id, name=inst.name,
        host=inst.host, port=inst.port, status=inst.status.value,
        default_permission=permission.value,
    )


@router.delete("/{department_id}/multitenant-instance/{instance_id}", status_code=204)
async def unbind_multitenant_instance(
    department_id: str,
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.models import DepartmentInstanceBinding

    binding = (await session.execute(
        select(DepartmentInstanceBinding).where(
            DepartmentInstanceBinding.department_id == department_id,
            DepartmentInstanceBinding.instance_id == instance_id,
        )
    )).scalar_one_or_none()
    if binding is None:
        raise HTTPException(404, "Binding not found")
    await session.delete(binding)
    await session.commit()
