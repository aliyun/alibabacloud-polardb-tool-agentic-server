from __future__ import annotations

import json
import logging
import asyncio
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, ContentBlock, TextContent, ToolAnnotations
from pydantic import AnyHttpUrl, Field
from sqlalchemy import select
from starlette.applications import Starlette

from server.auth.auth_routes import handle_login_page, handle_login_callback, handle_sso_redirect, handle_oidc_callback
from server.auth.oauth_provider import PASAuthProvider
from server.config import get_config
from server.core.connection_cache import ConnectionCache
from server.core.sql_gateway import SQLGateway
from server.db.engine import get_session_factory
from server.mcp.tools import (
    handle_create_branch,
    handle_delete_branch,
    handle_describe_schema,
    handle_list_branches,
    handle_list_instances,
    handle_run_sql,
    handle_run_sql_transaction,
    handle_set_default_instance,
    set_gateway,
    reset_gateway,
)
from server.models import User, UserStatus

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


def set_background_tasks(tasks: set[asyncio.Task]) -> None:  # type: ignore[type-arg]
    global _background_tasks
    _background_tasks = tasks


async def _get_current_user(session, user_id: str) -> User | None:  # type: ignore[type-arg]
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not isinstance(user, User) or user.status == UserStatus.DISABLED:
        return None
    return user


_AUTH_ERROR = json.dumps({"error": "AUTH_REQUIRED", "message": "Authentication required."})
_USER_ERROR = json.dumps({"error": "AUTH_REQUIRED", "message": "User not found or disabled."})


def _text_result(text: str, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=is_error)


def _json_error_result(error: str, message: str) -> CallToolResult:
    return _text_result(json.dumps({"error": error, "message": message}), is_error=True)


def _handler_result(result: dict) -> CallToolResult:
    if "content" in result and result["content"]:
        content: list[ContentBlock] = []
        for item in result["content"]:
            if item.get("type") == "text":
                content.append(TextContent(type="text", text=str(item.get("text", ""))))
        if content:
            return CallToolResult(content=content, isError=bool(result.get("isError", False)))
    return _text_result(json.dumps(result), is_error=bool(result.get("isError", False)))


def _handler_text(result: dict) -> str:
    if "content" in result and result["content"]:
        return str(result["content"][0].get("text", json.dumps(result)))
    return json.dumps(result)


def _log_text(result: CallToolResult) -> str:
    if not result.content:
        return ""
    first = result.content[0]
    return first.text if isinstance(first, TextContent) else str(first)


def _forbid_extra_tool_arguments(mcp: FastMCP, tool_names: set[str]) -> None:
    # FastMCP currently exposes no public hook for additionalProperties=false;
    # keep this compatibility shim small and migrate when the SDK provides one.
    manager = cast(Any, getattr(mcp, "_tool_manager", None))
    if manager is None:
        return
    for tool_name in tool_names:
        tool = manager.get_tool(tool_name)
        if tool is None:
            continue
        arg_model = tool.fn_metadata.arg_model
        arg_model.model_config["extra"] = "forbid"
        arg_model.model_rebuild(force=True)
        tool.parameters["additionalProperties"] = False


def _build_mcp_server() -> FastMCP:
    config = get_config()
    base_url = config.server.public_base_url

    provider = PASAuthProvider(
        session_factory=get_session_factory(),
        config=config,
    )

    mcp = FastMCP(
        "alibabacloud polardb tool agentic server",
        instructions="MCP server for PolarDB MySQL instances. Use run_sql to execute SQL, list_instances to see available databases.",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=cast(AnyHttpUrl, base_url),
            resource_server_url=cast(AnyHttpUrl, f"{base_url}/mcp"),
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
        host=config.server.host,
        streamable_http_path="/mcp",
        stateless_http=True,
    )

    # Register builtin login routes as custom routes (bypass SDK auth middleware)
    @mcp.custom_route("/mcp-auth/login", methods=["GET"])
    async def login_page(request):
        return await handle_login_page(request)

    @mcp.custom_route("/mcp-auth/login/callback", methods=["POST"])
    async def login_callback(request):
        return await handle_login_callback(request)

    if config.auth.mode == "oidc":
        @mcp.custom_route("/mcp-auth/sso-redirect", methods=["GET"])
        async def sso_redirect(request):
            return await handle_sso_redirect(request)

        @mcp.custom_route("/auth/oidc/callback", methods=["GET"])
        async def oidc_callback(request):
            return await handle_oidc_callback(request)

    @mcp.tool(
        description="Execute a SQL statement against a PolarDB MySQL instance.",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def run_sql(
        sql: str,
        instance_id: str | None = None,
        database: str | None = None,
        branch: str | None = None,
        max_rows: int = 1000,
        cursor: str | None = None,
        confirm: bool = False,
    ) -> Any:
        access_token = get_access_token()
        if not access_token or not access_token.subject:
            if branch is not None:
                return _json_error_result("AUTH_REQUIRED", "Authentication required.")
            return _AUTH_ERROR

        factory = get_session_factory()
        async with factory() as session:
            user = await _get_current_user(session, access_token.subject)
            if user is None:
                if branch is not None:
                    return _json_error_result("AUTH_REQUIRED", "User not found or disabled.")
                return _USER_ERROR

            logger.info("tool.run_sql.request | user=%s sql=%r instance_id=%s database=%s branch=%s max_rows=%d confirm=%s",
                        user.external_id, sql, instance_id, database, branch, max_rows, confirm)
            result = await handle_run_sql(
                user, session,
                sql=sql, instance_id=instance_id, database=database, branch=branch,
                max_rows=max_rows, cursor=cursor, confirm=confirm,
                session_factory=factory, background_tasks=_background_tasks,
            )
            response: Any
            if branch is not None:
                response = _handler_result(result)
                response_text = _log_text(response)
            else:
                response = _handler_text(result)
                response_text = response
            logger.info("tool.run_sql.response | user=%s len=%d body=%.500s",
                        user.external_id, len(response_text), response_text)
            return response

    @mcp.tool(
        description="List branches on a PolarDB instance.",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_branches(instance_id: str | None = None) -> CallToolResult:
        access_token = get_access_token()
        if not access_token or not access_token.subject:
            return _json_error_result("AUTH_REQUIRED", "Authentication required.")

        factory = get_session_factory()
        async with factory() as session:
            user = await _get_current_user(session, access_token.subject)
            if user is None:
                return _json_error_result("AUTH_REQUIRED", "User not found or disabled.")

            logger.info("tool.list_branches.request | user=%s instance_id=%s",
                        user.external_id, instance_id)
            result = await handle_list_branches(
                user, session, instance_id=instance_id,
                session_factory=factory, background_tasks=_background_tasks,
            )
            response = _handler_result(result)
            response_text = _log_text(response)
            logger.info("tool.list_branches.response | user=%s body=%.500s",
                        user.external_id, response_text)
            return response

    @mcp.tool(
        description="Create a PolarDB branch from the default branch.",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def create_branch(
        branch_name: Annotated[str, Field(min_length=1)],
        include_databases: list[Annotated[str, Field(min_length=1)]] | None = None,
        instance_id: str | None = None,
    ) -> CallToolResult:
        access_token = get_access_token()
        if not access_token or not access_token.subject:
            return _json_error_result("AUTH_REQUIRED", "Authentication required.")

        factory = get_session_factory()
        async with factory() as session:
            user = await _get_current_user(session, access_token.subject)
            if user is None:
                return _json_error_result("AUTH_REQUIRED", "User not found or disabled.")

            logger.info("tool.create_branch.request | user=%s instance_id=%s branch_name=%s",
                        user.external_id, instance_id, branch_name)
            result = await handle_create_branch(
                user, session, branch_name=branch_name,
                include_databases=include_databases, instance_id=instance_id,
                session_factory=factory, background_tasks=_background_tasks,
            )
            response = _handler_result(result)
            response_text = _log_text(response)
            logger.info("tool.create_branch.response | user=%s body=%.500s",
                        user.external_id, response_text)
            return response

    @mcp.tool(
        description=(
            "Delete a PolarDB branch. This is destructive; never call it "
            "autonomously and ask the user first."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def delete_branch(
        branch_name: Annotated[str, Field(min_length=1)],
        instance_id: str | None = None,
    ) -> CallToolResult:
        access_token = get_access_token()
        if not access_token or not access_token.subject:
            return _json_error_result("AUTH_REQUIRED", "Authentication required.")

        factory = get_session_factory()
        async with factory() as session:
            user = await _get_current_user(session, access_token.subject)
            if user is None:
                return _json_error_result("AUTH_REQUIRED", "User not found or disabled.")

            logger.info("tool.delete_branch.request | user=%s instance_id=%s branch_name=%s",
                        user.external_id, instance_id, branch_name)
            result = await handle_delete_branch(
                user, session, branch_name=branch_name, instance_id=instance_id,
                session_factory=factory, background_tasks=_background_tasks,
            )
            response = _handler_result(result)
            response_text = _log_text(response)
            logger.info("tool.delete_branch.response | user=%s body=%.500s",
                        user.external_id, response_text)
            return response

    @mcp.tool(
        description="List all PolarDB instances accessible to the current user.",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_instances() -> str:
        access_token = get_access_token()
        if not access_token or not access_token.subject:
            return _AUTH_ERROR

        factory = get_session_factory()
        async with factory() as session:
            user = await _get_current_user(session, access_token.subject)
            if user is None:
                return _USER_ERROR

            logger.info("tool.list_instances.request | user=%s", user.external_id)
            result = await handle_list_instances(user, session)
            response = json.dumps(result)
            logger.info("tool.list_instances.response | user=%s body=%.500s",
                        user.external_id, response)
            return response

    @mcp.tool(
        description="Set the default PolarDB instance for the current user. Subsequent run_sql calls without instance_id will route to this instance.",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def set_default_instance(instance_id: str) -> str:
        access_token = get_access_token()
        if not access_token or not access_token.subject:
            return _AUTH_ERROR

        factory = get_session_factory()
        async with factory() as session:
            user = await _get_current_user(session, access_token.subject)
            if user is None:
                return _USER_ERROR

            logger.info("tool.set_default_instance.request | user=%s instance_id=%s",
                        user.external_id, instance_id)
            result = await handle_set_default_instance(user, session, instance_id=instance_id)
            if "content" in result and result["content"]:
                response = str(result["content"][0].get("text", json.dumps(result)))
            else:
                response = json.dumps(result)
            logger.info("tool.set_default_instance.response | user=%s body=%.500s",
                        user.external_id, response)
            return response

    @mcp.tool(
        description=(
            "Execute multiple SQL statements in a single transaction. "
            "The server wraps them in BEGIN/COMMIT automatically. "
            "If any statement fails, the entire transaction is rolled back. "
            "For single statements, use run_sql instead."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def run_sql_transaction(
        sql_statements: list[str],
        instance_id: str | None = None,
        database: str | None = None,
        confirm: bool = False,
    ) -> str:
        access_token = get_access_token()
        if not access_token or not access_token.subject:
            return _AUTH_ERROR

        factory = get_session_factory()
        async with factory() as session:
            user = await _get_current_user(session, access_token.subject)
            if user is None:
                return _USER_ERROR

            logger.info(
                "tool.run_sql_transaction.request | user=%s statements=%d instance_id=%s database=%s confirm=%s",
                user.external_id, len(sql_statements), instance_id, database, confirm,
            )
            result = await handle_run_sql_transaction(
                user, session,
                sql_statements=sql_statements,
                instance_id=instance_id, database=database, confirm=confirm,
                session_factory=factory, background_tasks=_background_tasks,
            )
            if "content" in result and result["content"]:
                response = str(result["content"][0].get("text", json.dumps(result)))
            else:
                response = json.dumps(result)
            logger.info(
                "tool.run_sql_transaction.response | user=%s len=%d body=%.500s",
                user.external_id, len(response), response,
            )
            return response

    @mcp.tool(
        description=(
            "Discover database tables and their semantic descriptions (COMMENTs). "
            "Use this to understand what data already exists before creating new tables."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def describe_schema(
        database: str | None = None,
        instance_id: str | None = None,
        table_pattern: str | None = None,
        include_columns: bool = True,
        cursor: str | None = None,
        max_tables: int = 20,
    ) -> str:
        access_token = get_access_token()
        if not access_token or not access_token.subject:
            return _AUTH_ERROR

        factory = get_session_factory()
        async with factory() as session:
            user = await _get_current_user(session, access_token.subject)
            if user is None:
                return _USER_ERROR

            logger.info(
                "tool.describe_schema.request | user=%s database=%s instance_id=%s pattern=%s",
                user.external_id, database, instance_id, table_pattern,
            )
            result = await handle_describe_schema(
                user, session,
                instance_id=instance_id, database=database,
                table_pattern=table_pattern, include_columns=include_columns,
                cursor=cursor, max_tables=max_tables,
                session_factory=factory, background_tasks=_background_tasks,
            )
            if "content" in result and result["content"]:
                response = str(result["content"][0].get("text", json.dumps(result)))
            else:
                response = json.dumps(result)
            logger.info(
                "tool.describe_schema.response | user=%s len=%d body=%.500s",
                user.external_id, len(response), response,
            )
            return response

    _forbid_extra_tool_arguments(mcp, {
        "list_branches",
        "create_branch",
        "delete_branch",
    })

    return mcp


_mcp_server: FastMCP | None = None
_mcp_app: Starlette | None = None


def _get_mcp_server() -> FastMCP:
    global _mcp_server
    if _mcp_server is None:
        _mcp_server = _build_mcp_server()
    return _mcp_server


def create_mcp_app() -> Starlette:
    global _mcp_app
    if _mcp_app is None:
        mcp = _get_mcp_server()
        _mcp_app = mcp.streamable_http_app()
    return _mcp_app


def get_session_manager() -> StreamableHTTPSessionManager:
    create_mcp_app()  # ensures session manager is created
    return _get_mcp_server().session_manager


def reset_mcp() -> None:
    global _mcp_server, _mcp_app
    _mcp_server = None
    _mcp_app = None


@asynccontextmanager
async def mcp_lifespan():
    config = get_config()
    cache = ConnectionCache(config.polardb.connection_pool)
    gateway = SQLGateway(cache)
    set_gateway(gateway)
    cleanup_task = asyncio.create_task(cache.run_cleanup_loop())

    sm = get_session_manager()
    started = asyncio.Event()

    async def _run():
        async with sm.run():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

    task = asyncio.create_task(_run())
    await started.wait()
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await cache.close_all()
        reset_gateway()
