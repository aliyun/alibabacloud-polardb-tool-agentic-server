from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp import FastMCP
from mcp.types import ContentBlock
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
from server.auth.personal_access import is_personal_access
from server.auth.token_claims import (
    access_token_agent_id,
    is_legacy_agent_user_token,
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
    ProvisioningMode,
    UserInstanceBinding,
)
from server.mcp.knowledge_tools import (
    POLARRAG_TOOL_NAMES,
    POLARRAG_UPLOAD_TOOL_NAMES,
)
from server.mcp.workspace_context import (
    MCPWorkspaceUnavailable,
    resolve_mcp_workspace_context,
)

DB_INSTANCE_TOOL_NAMES = frozenset(
    {
        "list_db_instances",
        "create_db_instance",
        "describe_db_instance",
        "delete_db_instance",
    }
)
WORKSPACE_UNSUPPORTED_TOOL_NAMES = frozenset(
    {
        "set_default_instance",
        "list_branches",
        "create_branch",
        "delete_branch",
    }
)
PERSONAL_UNSUPPORTED_TOOL_NAMES = WORKSPACE_UNSUPPORTED_TOOL_NAMES | frozenset({
    "create_db_instance", "delete_db_instance", "doc_delete", "doc_rechunk",
})
AGENT_SQL_TOOL_NAMES = frozenset(
    {
        "run_sql",
        "run_sql_transaction",
        "describe_schema",
    }
)


def _is_agent_user_token(access_token: Any) -> bool:
    return bool(
        access_token is not None
        and is_legacy_agent_user_token(access_token)
    )


async def _direct_management_tools(
    session: AsyncSession,
    principal: Principal,
    *, personal: bool = False,
) -> set[str]:
    if principal.kind == PrincipalKind.USER:
        statement = select(UserInstanceBinding.instance_id).where(
            UserInstanceBinding.user_id == principal.id,
            UserInstanceBinding.enabled.is_(True),
        )
        if personal:
            from server.core.db_instance_query import personal_instance_ids
            statement = personal_instance_ids(principal.id)
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
        if personal and access is not None:
            if access.permission is not None and BindingCapability.SQL_READ in access.capabilities:
                allowed.update({"list_db_instances", "describe_db_instance", *AGENT_SQL_TOOL_NAMES})
            if BindingCapability.DB_INSTANCE_LIST in access.capabilities:
                allowed.add("list_db_instances")
            if BindingCapability.DB_INSTANCE_DESCRIBE in access.capabilities:
                allowed.add("describe_db_instance")
            continue
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
            access.permission is not None
            and BindingCapability.SQL_READ in access.capabilities
        ):
            allowed.update(AGENT_SQL_TOOL_NAMES)
    return allowed


async def has_eligible_provisioning_backend(
    session: AsyncSession,
    agent_id: str,
) -> bool:
    dedicated_candidates = await list_candidates(
        session,
        agent_id,
        InstanceEngine.POLARDB_MYSQL,
        "tool-catalog",
        mode=ProvisioningMode.DEDICATED,
    )
    if dedicated_candidates:
        return True
    candidates = await list_candidates(
        session,
        agent_id,
        InstanceEngine.POLARDB_MYSQL,
        "tool-catalog",
        mode=ProvisioningMode.MULTITENANT,
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
    *, personal: bool = False,
) -> frozenset[str]:
    allowed = await _direct_management_tools(session, principal, personal=personal)
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
            context = await resolve_mcp_workspace_context(
                session,
                access_token.subject,
                access_token_agent_id(access_token),
                personal=is_personal_access(access_token),
            )
            return await allowed_db_instance_tool_names(
                session, context.resource_principal, personal=context.agent is None
            )
    except (PrincipalAuthenticationError, MCPWorkspaceUnavailable):
        return frozenset()


async def _request_catalog_policy(
) -> tuple[Principal | None, Principal | None, frozenset[str]]:
    access_token = get_access_token()
    if access_token is None or not access_token.subject:
        return None, None, frozenset()
    try:
        async with get_session_factory()() as session:
            authenticated = await get_current_principal(
                session, access_token.subject
            )
            try:
                context = await resolve_mcp_workspace_context(
                    session,
                    access_token.subject,
                    access_token_agent_id(access_token),
                    personal=is_personal_access(access_token),
                )
            except MCPWorkspaceUnavailable:
                return authenticated, None, frozenset()
            allowed = await allowed_db_instance_tool_names(
                session, context.resource_principal, personal=context.agent is None
            )
            return (
                context.authenticated_principal,
                context.resource_principal,
                allowed,
            )
    except PrincipalAuthenticationError:
        return None, None, frozenset()


class AuthorizedFastMCP(FastMCP):
    """Filter the request's Tool catalog without changing global registration."""

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        from server.features.knowledge import runtime
        feature = runtime()
        if feature is not None and not await feature.available():
            tools = [tool for tool in tools if tool.name not in POLARRAG_TOOL_NAMES]
        access_token = get_access_token()
        if _is_agent_user_token(access_token):
            return [
                tool for tool in tools if tool.name in POLARRAG_TOOL_NAMES
            ]
        authenticated, resource, allowed = await _request_catalog_policy()
        visible = [
            tool
            for tool in tools
            if (
                tool.name not in DB_INSTANCE_TOOL_NAMES
                or tool.name in allowed
            )
            and tool.name not in POLARRAG_UPLOAD_TOOL_NAMES
            and not (is_personal_access(access_token) and tool.name in PERSONAL_UNSUPPORTED_TOOL_NAMES)
            and not (
                authenticated is not None
                and (
                    resource is None
                    or resource.kind == PrincipalKind.AGENT
                    or is_personal_access(access_token)
                )
                and tool.name in WORKSPACE_UNSUPPORTED_TOOL_NAMES
            )
            and not (
                authenticated is not None
                and authenticated.kind == PrincipalKind.USER
                and resource is None
                and tool.name
                in AGENT_SQL_TOOL_NAMES | POLARRAG_TOOL_NAMES
            )
            and not (
                authenticated is not None
                and authenticated.kind == PrincipalKind.AGENT
                and tool.name in POLARRAG_TOOL_NAMES
            )
            and not (
                resource is not None
                and (resource.kind == PrincipalKind.AGENT or is_personal_access(access_token))
                and tool.name in AGENT_SQL_TOOL_NAMES
                and tool.name not in allowed
            )
        ]
        if (
            resource is None
            or (resource.kind != PrincipalKind.AGENT and not is_personal_access(access_token))
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

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        access_token = get_access_token()
        if (
            _is_agent_user_token(access_token)
            and name not in POLARRAG_TOOL_NAMES
        ):
            raise ToolError(f"Unknown tool: {name}")
        if (
            not _is_agent_user_token(access_token)
            and name in POLARRAG_UPLOAD_TOOL_NAMES
        ):
            raise ToolError(f"Unknown tool: {name}")
        if is_personal_access(access_token):
            if name in PERSONAL_UNSUPPORTED_TOOL_NAMES:
                raise ToolError(f"Unknown tool: {name}")
            if name in AGENT_SQL_TOOL_NAMES and not arguments.get("instance_id"):
                raise ToolError("Personal SQL access requires instance_id")
            if name == "run_sql" and arguments.get("branch") is not None:
                raise ToolError("Branch access is unavailable on personal MCP")
        from server.features.knowledge import runtime, KnowledgeUnavailable
        feature = runtime()
        if name in POLARRAG_TOOL_NAMES and feature is not None:
            try:
                async with feature.operation(name):
                    return await super().call_tool(name, arguments)
            except KnowledgeUnavailable as error:
                raise ToolError(error.code) from error
        return await super().call_tool(name, arguments)
