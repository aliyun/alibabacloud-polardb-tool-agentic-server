from __future__ import annotations

import copy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import Tool as MCPTool

from server.auth.principal import (
    Principal,
    PrincipalAuthenticationError,
    PrincipalKind,
    get_current_principal,
)
from server.core.access_control import (
    resolve_agent_instance_access,
    resolve_user_instance_access,
)
from server.core.credential_policy import (
    is_valid_direct_access_credential,
)
from server.core.db_instance_contract import resource_capabilities
from server.core.provisioning_backend_repository import list_candidates
from server.db.engine import get_session_factory
from server.models import (
    AgentInstanceBinding,
    BindingCapability,
    BindingOrigin,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    DBInstanceStatus,
    InstanceEngine,
    InstanceCredential,
    ProvisioningBackend,
    UserInstanceBinding,
)

DB_INSTANCE_TOOL_NAMES = frozenset(
    {
        "list_db_instances",
        "create_db_instance",
        "describe_db_instance",
        "delete_db_instance",
    }
)
USER_ONLY_TOOL_NAMES = frozenset(
    {
        "set_default_instance",
        "list_branches",
        "create_branch",
        "delete_branch",
    }
)
AGENT_SQL_TOOL_NAMES = frozenset(
    {
        "run_sql",
        "run_sql_transaction",
        "describe_schema",
    }
)


async def _direct_management_tools(
    session: AsyncSession,
    principal: Principal,
) -> set[str]:
    if principal.kind == PrincipalKind.USER:
        statement = select(UserInstanceBinding.instance_id).where(
            UserInstanceBinding.user_id == principal.id,
            UserInstanceBinding.enabled.is_(True),
        )
        instance_ids = (await session.execute(statement)).scalars().all()
        accesses = [
            await resolve_user_instance_access(
                session, principal.id, instance_id
            )
            for instance_id in instance_ids
        ]
    else:
        statement = select(AgentInstanceBinding.instance_id).where(
            AgentInstanceBinding.agent_id == principal.id,
            AgentInstanceBinding.enabled.is_(True),
        )
        instance_ids = (await session.execute(statement)).scalars().all()
        accesses = [
            await resolve_agent_instance_access(
                session, principal.id, instance_id
            )
            for instance_id in instance_ids
        ]

    allowed: set[str] = set()
    for access in accesses:
        if access is None or access.binding is None:
            continue
        binding = access.binding
        if isinstance(binding, UserInstanceBinding):
            if binding.origin != BindingOrigin.ADMIN:
                continue
        credential = binding.credential
        if (
            credential is None
            or binding.credential_id != credential.id
            or not is_valid_direct_access_credential(
                credential, access.instance.id
            )
        ):
            continue
        if BindingCapability.DB_INSTANCE_LIST in access.capabilities:
            allowed.add("list_db_instances")
        if BindingCapability.DB_INSTANCE_DESCRIBE in access.capabilities:
            allowed.update(
                {"list_db_instances", "describe_db_instance"}
            )
        if (
            principal.kind == PrincipalKind.AGENT
            and access.permission is not None
            and BindingCapability.SQL_READ in access.capabilities
        ):
            allowed.update(AGENT_SQL_TOOL_NAMES)
    return allowed


async def has_eligible_provisioning_backend(
    session: AsyncSession,
    agent_id: str,
) -> bool:
    candidates = await list_candidates(
        session,
        agent_id,
        InstanceEngine.POLARDB_MYSQL,
        "tool-catalog",
    )
    candidate_ids = [candidate.backend_id for candidate in candidates]
    if not candidate_ids:
        return False
    credential_id = (
        await session.execute(
            select(InstanceCredential.id)
            .join(
                ProvisioningBackend,
                ProvisioningBackend.admin_credential_id
                == InstanceCredential.id,
            )
            .where(
                ProvisioningBackend.id.in_(candidate_ids),
                InstanceCredential.instance_id
                == ProvisioningBackend.instance_id,
                InstanceCredential.resource_id.is_(None),
                InstanceCredential.purpose
                == CredentialPurpose.PROVISIONING_ADMIN,
                InstanceCredential.capability
                == CredentialCapability.ADMIN,
                InstanceCredential.status == CredentialStatus.ACTIVE,
                InstanceCredential.username_ciphertext.is_not(None),
                InstanceCredential.password_ciphertext.is_not(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return credential_id is not None


async def allowed_db_instance_tool_names(
    session: AsyncSession,
    principal: Principal,
) -> frozenset[str]:
    allowed = await _direct_management_tools(session, principal)
    if principal.kind != PrincipalKind.AGENT:
        return frozenset(allowed)

    owned_resources = (
        await session.execute(
            select(DBInstanceResource)
            .options(selectinload(DBInstanceResource.credentials))
            .where(
                DBInstanceResource.owner_agent_id == principal.id,
                DBInstanceResource.status != DBInstanceStatus.DELETED,
            )
        )
    ).scalars().all()
    if owned_resources:
        allowed.update(
            {
                "list_db_instances",
                "describe_db_instance",
                "delete_db_instance",
            }
        )
    if any(
        "run_sql_read" in resource_capabilities(resource)
        for resource in owned_resources
    ):
        allowed.update(AGENT_SQL_TOOL_NAMES)

    if await has_eligible_provisioning_backend(session, principal.id):
        allowed.update(DB_INSTANCE_TOOL_NAMES)
    return frozenset(allowed)


async def allowed_db_instance_tool_names_for_request() -> frozenset[str]:
    access_token = get_access_token()
    if access_token is None or not access_token.subject:
        return frozenset()
    try:
        async with get_session_factory()() as session:
            principal = await get_current_principal(
                session, access_token.subject
            )
            return await allowed_db_instance_tool_names(
                session, principal
            )
    except PrincipalAuthenticationError:
        return frozenset()


async def _request_catalog_policy(
) -> tuple[Principal | None, frozenset[str]]:
    access_token = get_access_token()
    if access_token is None or not access_token.subject:
        return None, frozenset()
    try:
        async with get_session_factory()() as session:
            principal = await get_current_principal(
                session, access_token.subject
            )
            allowed = await allowed_db_instance_tool_names(
                session, principal
            )
            return principal, allowed
    except PrincipalAuthenticationError:
        return None, frozenset()


class AuthorizedFastMCP(FastMCP):
    """Filter the request's Tool catalog without changing global registration."""

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        principal, allowed = await _request_catalog_policy()
        visible = [
            tool
            for tool in tools
            if (
                tool.name not in DB_INSTANCE_TOOL_NAMES
                or tool.name in allowed
            )
            and not (
                principal is not None
                and principal.kind == PrincipalKind.AGENT
                and tool.name in USER_ONLY_TOOL_NAMES
            )
            and not (
                principal is not None
                and principal.kind == PrincipalKind.AGENT
                and tool.name in AGENT_SQL_TOOL_NAMES
                and tool.name not in allowed
            )
        ]
        if (
            principal is None
            or principal.kind != PrincipalKind.AGENT
        ):
            return visible
        rewritten: list[MCPTool] = []
        for tool in visible:
            if tool.name not in AGENT_SQL_TOOL_NAMES:
                rewritten.append(tool)
                continue
            schema = copy.deepcopy(tool.inputSchema)
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))
            required.add("instance_id")
            if tool.name == "run_sql":
                properties.pop("branch", None)
                required.discard("branch")
            schema["required"] = [
                name for name in properties if name in required
            ]
            rewritten.append(
                tool.model_copy(update={"inputSchema": schema})
            )
        return rewritten
