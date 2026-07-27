from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from server.core.access_control import resolve_agent_instance_access
from server.core.credential_policy import (
    is_valid_direct_access_credential,
)
from server.core.db_instance_contract import (
    usable_resource_access_credential,
)
from server.core.responses import error_response
from server.models import (
    Agent,
    AgentInstanceBinding,
    BindingCapability,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    Permission,
    ProvisioningBackend,
)


@dataclass(frozen=True)
class AgentSQLAccess:
    agent: Agent
    public_instance_id: str
    source: Literal["bound", "provisioned"]
    instance: Instance
    credential: InstanceCredential
    permission: Permission
    database: str | None
    binding: AgentInstanceBinding | None = None
    resource: DBInstanceResource | None = None


_NOT_ACCESSIBLE_MESSAGE = (
    "The supplied instance_id is not accessible to this Agent. "
    "Call list_db_instances and use a returned db_instance_id with "
    "run_sql_read capability."
)


def _resource_status_error(
    status: DBInstanceStatus,
) -> dict | None:
    if status == DBInstanceStatus.CREATING:
        return error_response(
            "INSTANCE_STARTING",
            "This database resource is still being created. "
            "Call list_db_instances again and retry when its status is READY.",
        )
    if status == DBInstanceStatus.FAILED:
        return error_response(
            "INSTANCE_FAILED",
            "This database resource could not be created and cannot execute "
            "SQL. Call describe_db_instance for its safe status details, "
            "then delete or recreate it.",
        )
    if status == DBInstanceStatus.DELETING:
        return error_response(
            "INSTANCE_DELETING",
            "This database resource is being deleted and cannot execute SQL. "
            "Choose another instance returned by list_db_instances.",
        )
    if status == DBInstanceStatus.DELETE_FAILED:
        return error_response(
            "INSTANCE_DELETE_FAILED",
            "Deletion of this database resource requires administrator "
            "attention. It cannot execute SQL. Choose another READY instance "
            "returned by list_db_instances.",
        )
    if status == DBInstanceStatus.DELETED:
        return error_response(
            "INSTANCE_NOT_ACCESSIBLE",
            _NOT_ACCESSIBLE_MESSAGE,
        )
    return None


async def _resolve_provisioned_access(
    agent: Agent,
    session: AsyncSession,
    *,
    instance_id: str,
    database: str | None,
) -> AgentSQLAccess | dict:
    row = (
        await session.execute(
            select(DBInstanceResource, Instance)
            .join(
                ProvisioningBackend,
                ProvisioningBackend.id == DBInstanceResource.backend_id,
            )
            .join(
                Instance,
                Instance.id == ProvisioningBackend.instance_id,
            )
            .options(selectinload(DBInstanceResource.credentials))
            .where(
                DBInstanceResource.id == instance_id,
                DBInstanceResource.owner_agent_id == agent.id,
            )
        )
    ).one_or_none()
    if row is None:
        return error_response(
            "INSTANCE_NOT_ACCESSIBLE",
            _NOT_ACCESSIBLE_MESSAGE,
        )
    resource, instance = row
    status_error = _resource_status_error(resource.status)
    if status_error is not None:
        return status_error

    credential = usable_resource_access_credential(resource)
    if credential is None or resource.database_name is None:
        return error_response(
            "INSTANCE_NOT_ACCESSIBLE",
            _NOT_ACCESSIBLE_MESSAGE,
        )
    if (
        database is not None
        and database != resource.database_name
    ):
        return error_response(
            "INVALID_ARGUMENT",
            "database must be omitted or must equal the provisioned resource "
            f"database '{resource.database_name}'. Use describe_db_instance "
            "to obtain the correct database name.",
        )
    return AgentSQLAccess(
        agent=agent,
        public_instance_id=resource.id,
        source="provisioned",
        instance=instance,
        credential=credential,
        permission=Permission.READWRITE,
        database=resource.database_name,
        resource=resource,
    )


async def resolve_agent_sql_access(
    agent: Agent,
    session: AsyncSession,
    *,
    instance_id: str | None,
    database: str | None,
) -> AgentSQLAccess | dict:
    if not instance_id:
        return error_response(
            "INVALID_ARGUMENT",
            "instance_id is required. Call list_db_instances and pass the "
            "db_instance_id of an ACTIVE or READY instance with "
            "run_sql_read capability.",
        )

    if instance_id.startswith("dbi-"):
        return await _resolve_provisioned_access(
            agent,
            session,
            instance_id=instance_id,
            database=database,
        )

    access = await resolve_agent_instance_access(
        session,
        agent.id,
        instance_id,
    )
    if (
        access is None
        or access.permission is None
        or BindingCapability.SQL_READ not in access.capabilities
        or not isinstance(access.binding, AgentInstanceBinding)
    ):
        return error_response(
            "INSTANCE_NOT_ACCESSIBLE",
            _NOT_ACCESSIBLE_MESSAGE,
        )

    binding = access.binding
    credential = binding.credential
    if not is_valid_direct_access_credential(
        credential,
        access.instance.id,
    ):
        return error_response(
            "INSTANCE_NOT_ACCESSIBLE",
            _NOT_ACCESSIBLE_MESSAGE,
        )

    return AgentSQLAccess(
        agent=agent,
        public_instance_id=access.instance.id,
        source="bound",
        instance=access.instance,
        credential=credential,
        permission=access.permission,
        database=(database if database is not None else credential.database_name),
        binding=binding,
    )
