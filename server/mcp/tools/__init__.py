from __future__ import annotations

import base64
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.access_control import resolve_user_instance_access
from server.core.binding_manager import get_accessible_instances
from server.core.instance_manager import instance_category
from server.core.responses import error_response
from server.core.sql_gateway import SQLGateway
from server.models import (
    BindingCapability, Instance, InstanceStatus, InstanceTopology, User,
)

logger = logging.getLogger(__name__)

_gateway: SQLGateway | None = None


def set_gateway(gateway: SQLGateway) -> None:
    global _gateway
    _gateway = gateway


def get_gateway() -> SQLGateway:
    if _gateway is None:
        raise RuntimeError("SQLGateway not initialized")
    return _gateway


def reset_gateway() -> None:
    global _gateway
    _gateway = None


def _get_user_permission(accessible: list[dict], instance: Instance) -> str:
    for item in accessible:
        if item["instance"].id == instance.id:
            return str(item["permission"])
    from server.models import Permission
    logger.warning("No permission found for instance %s in accessible list, defaulting to READONLY", instance.id)
    return Permission.READONLY.value


def _decode_cursor(cursor: str) -> int:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor))
        offset = data.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int):
            return 0
        if offset < 0:
            return 0
        return offset
    except Exception:
        return 0


async def resolve_target_instance(
    user: User, session: AsyncSession, instance_id: str | None = None,
    *, session_factory=None, background_tasks=None,
) -> tuple[Instance, list[dict]] | dict:
    """Resolve the target instance for SQL execution.
    Returns an Instance on success, or an error dict on failure.
    """
    if instance_id:
        target = (await session.execute(
            select(Instance).where(Instance.id == instance_id)
        )).scalar_one_or_none()
        if target is None:
            return error_response("INSTANCE_NOT_FOUND", "Instance not found.")
        access = await resolve_user_instance_access(
            session, user.id, instance_id
        )
        if (
            access is None
            or access.permission is None
            or BindingCapability.SQL_READ not in access.capabilities
        ):
            return error_response("INSTANCE_NOT_ACCESSIBLE", "You don't have access to this instance.")
        accessible = await get_accessible_instances(session, user)
        return target, accessible

    # Check for a CREATING personal instance before querying accessible
    creating = (await session.execute(
        select(Instance).where(
            Instance.owner_user_id == user.id,
            Instance.topology == InstanceTopology.SINGLE_TENANT,
            Instance.status == InstanceStatus.CREATING,
        )
    )).scalar_one_or_none()
    if creating:
        from server.config import get_config
        retry_after = get_config().polardb.resource_pool.retry_after_seconds
        return error_response("INSTANCE_CREATING",
            "Instance is being provisioned, please retry later.",
            retry_after_seconds=retry_after)

    accessible = await get_accessible_instances(session, user)

    if len(accessible) == 0:
        # Check FAILED before auto-provision
        failed = (await session.execute(
            select(Instance).where(
                Instance.owner_user_id == user.id,
                Instance.topology == InstanceTopology.SINGLE_TENANT,
                Instance.status == InstanceStatus.FAILED,
            )
        )).scalar_one_or_none()
        if failed:
            return error_response("INSTANCE_PROVISION_FAILED",
                "Instance provisioning failed. Contact admin for assistance.",
                instance_id=failed.id)

        # Check if user expects multitenant but has no instance bound
        from server.core.provisioner import resolve_provisioning_mode
        from server.models.user import ProvisioningMode
        mode = resolve_provisioning_mode(user)
        if mode == ProvisioningMode.MULTITENANT:
            return error_response("NO_MULTITENANT_INSTANCE",
                "No multi-tenant instance configured. Contact admin.")

        # Auto-provision via orchestrator (cross-module: quota + pool + provisioner)
        if session_factory is not None and background_tasks is not None:
            from server.core.orchestrator import provision_personal_instance
            result = await provision_personal_instance(
                user, session, session_factory, background_tasks,
            )
            if isinstance(result, dict):
                return result
            return result, accessible

        return error_response("NO_INSTANCE_AVAILABLE", "No instances available. Contact admin to get access.")

    if len(accessible) == 1:
        inst: Instance = accessible[0]["instance"]
        return inst, accessible

    if user.default_instance_id:
        match: Instance | None = next(
            (a["instance"] for a in accessible if a["instance"].id == user.default_instance_id), None
        )
        if match:
            return match, accessible
        user.default_instance_id = None
        await session.flush()

    personal: list[Instance] = [
        item["instance"]
        for item in accessible
        if instance_category(item["instance"]) == "personal"
    ]
    if personal:
        return personal[0], accessible

    multitenant: list[Instance] = [
        item["instance"]
        for item in accessible
        if item["instance"].topology == InstanceTopology.MULTITENANT
    ]
    if multitenant:
        return multitenant[0], accessible

    instance_list = [
        {
            "instance_id": item["instance"].id,
            "name": item["instance"].name,
            "type": instance_category(item["instance"]),
        }
        for item in accessible
    ]
    return error_response(
        "MULTIPLE_INSTANCES",
        "Multiple instances available. Specify instance_id or use set_default_instance.",
        instances=instance_list,
    )


# Re-export handlers (imported at the bottom to avoid circular import at module load time)
from server.mcp.tools.handlers import (  # noqa: E402
    handle_list_instances,
    handle_run_sql,
    handle_run_sql_transaction,
    handle_set_default_instance,
)
from server.mcp.tools.schema_handler import handle_describe_schema  # noqa: E402
from server.mcp.tools.branch_handler import (  # noqa: E402
    handle_create_branch,
    handle_delete_branch,
    handle_list_branches,
)
from server.mcp.tools.db_instance_handler import (  # noqa: E402
    handle_create_db_instance,
    handle_delete_db_instance,
    handle_describe_db_instance,
    handle_list_db_instances,
    require_agent_principal,
    resolve_request_principal,
    serialize_db_instance_page,
    serialize_resource,
)

__all__ = [
    "set_gateway",
    "get_gateway",
    "reset_gateway",
    "resolve_target_instance",
    "handle_list_instances",
    "handle_run_sql",
    "handle_run_sql_transaction",
    "handle_set_default_instance",
    "handle_describe_schema",
    "handle_list_branches",
    "handle_create_branch",
    "handle_delete_branch",
    "handle_list_db_instances",
    "handle_create_db_instance",
    "handle_describe_db_instance",
    "handle_delete_db_instance",
    "resolve_request_principal",
    "require_agent_principal",
    "serialize_db_instance_page",
    "serialize_resource",
]
