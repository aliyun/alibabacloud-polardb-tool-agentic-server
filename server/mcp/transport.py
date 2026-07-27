from __future__ import annotations

import json
import logging
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, ContentBlock, TextContent, ToolAnnotations
from pydantic import AnyHttpUrl, Field
from starlette.applications import Starlette

from server.auth.auth_routes import handle_login_page, handle_login_callback, handle_sso_redirect, handle_oidc_callback
from server.auth.oauth_provider import PASAuthProvider
from server.auth.principal import (
    get_current_principal,
    PrincipalAuthenticationError,
    PrincipalDisabled,
    PrincipalKind,
    require_current_actor,
)
from server.config import get_config
from server.core.connection_cache import ConnectionCache
from server.core.db_instance_metrics import DBInstanceMetricsMiddleware
from server.core.sql_gateway import SQLGateway
from server.db.engine import get_session_factory
from server.mcp.authorized_server import AuthorizedFastMCP
from server.mcp.tools import (
    handle_create_branch,
    handle_delete_branch,
    handle_describe_schema,
    handle_list_branches,
    handle_run_sql,
    handle_run_sql_transaction,
    handle_set_default_instance,
    set_gateway,
    reset_gateway,
)
from server.mcp.tools.db_instance_handler import (
    db_instance_result,
    handle_create_db_instance,
    handle_delete_db_instance,
    handle_describe_db_instance,
    handle_list_db_instances,
    resolve_request_principal,
    reset_describe_rate_limiters,
)
from server.models import Agent, User

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


def set_background_tasks(tasks: set[asyncio.Task]) -> None:  # type: ignore[type-arg]
    global _background_tasks
    _background_tasks = tasks


async def _get_current_user(
    session, subject: str
) -> User | None:  # type: ignore[type-arg]
    try:
        actor = await require_current_actor(
            session, subject, PrincipalKind.USER
        )
    except PrincipalAuthenticationError:
        return None
    return actor if isinstance(actor, User) else None


async def _get_current_sql_actor(
    session,
    subject: str,
) -> User | Agent | None:  # type: ignore[type-arg]
    try:
        principal = await get_current_principal(session, subject)
    except PrincipalAuthenticationError:
        return None
    model = User if principal.kind == PrincipalKind.USER else Agent
    return await session.get(model, principal.id)


def _actor_kind(actor: User | Agent) -> PrincipalKind:
    return (
        PrincipalKind.AGENT
        if type(actor) is Agent
        else PrincipalKind.USER
    )


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


def _safe_tool_result_metadata(result: dict[str, Any]) -> dict[str, Any]:
    """Extract only allowlisted, non-sensitive execution metadata."""
    payload: dict[str, Any] = {}
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            try:
                decoded = json.loads(first["text"])
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, dict):
                payload = decoded
    error_code = payload.get("error")
    row_count = payload.get("row_count")
    truncated = payload.get("truncated")
    return {
        "tool_status": (
            "error"
            if result.get("isError") is True or isinstance(error_code, str)
            else "success"
        ),
        "error_code": error_code if isinstance(error_code, str) else None,
        "row_count": (
            row_count
            if isinstance(row_count, int) and not isinstance(row_count, bool)
            else None
        ),
        "truncated": truncated if isinstance(truncated, bool) else None,
    }


def _log_user_tool_completion(
    *,
    tool: str,
    user: User,
    started_at: float,
    result: dict[str, Any],
    instance_id: str | None = None,
    **safe_fields: Any,
) -> None:
    logger.info(
        f"tool.{tool}.completed",
        extra={
            "actor_kind": PrincipalKind.USER.value,
            "actor_id": user.id,
            "instance_id": instance_id,
            "duration_ms": int(
                (time.perf_counter() - started_at) * 1000
            ),
            **_safe_tool_result_metadata(result),
            **safe_fields,
        },
    )


def _forbid_extra_tool_arguments(
    mcp: AuthorizedFastMCP, tool_names: set[str]
) -> None:
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


def _configure_db_instance_tool_schemas(
    mcp: AuthorizedFastMCP,
) -> None:
    manager = cast(Any, getattr(mcp, "_tool_manager", None))
    if manager is None:
        return
    create = manager.get_tool("create_db_instance")
    if create is not None:
        properties = create.parameters["properties"]
        properties["client_token"].update(
            {
                "minLength": 1,
                "maxLength": 128,
                "pattern": r"^[A-Za-z0-9._:-]+$",
            }
        )
        properties["db_type"].pop("const", None)
        properties["db_type"]["enum"] = ["polardb_mysql"]
        name_items = properties["name"].get("anyOf", [])
        for item in name_items:
            if item.get("type") == "string":
                item.update({"minLength": 1, "maxLength": 128})
    listing = manager.get_tool("list_db_instances")
    if listing is not None:
        list_properties = listing.parameters["properties"]
        list_properties["limit"].update(
            {"minimum": 1, "maximum": 200}
        )
        for item in list_properties["db_type"].get("anyOf", []):
            if item.get("type") == "string":
                item["enum"] = ["polardb_mysql"]
        for item in list_properties["source"].get("anyOf", []):
            if item.get("type") == "string":
                item["enum"] = [
                    "auto_provisioned",
                    "bound",
                    "provisioned",
                ]
    for name in ("describe_db_instance", "delete_db_instance"):
        tool = manager.get_tool(name)
        if tool is not None:
            tool.parameters["properties"]["db_instance_id"][
                "minLength"
            ] = 1


def _build_mcp_server() -> AuthorizedFastMCP:
    from server.configuration.runtime import RuntimeSectionProxy

    config = RuntimeSectionProxy(get_config)
    # The MCP surface is request-gated during SETUP. A syntactically valid
    # internal placeholder lets routes remain installed before an external
    # URL is configured.
    base_url = (
        config.server.public_base_url or "https://pas.invalid"
    )

    provider = PASAuthProvider(
        session_factory=get_session_factory(),
        config=config,
    )

    mcp = AuthorizedFastMCP(
        "alibabacloud polardb tool agentic server",
        instructions=(
            "MCP server for PolarDB MySQL instances. Use run_sql to execute "
            "SQL and list_db_instances when that Tool is authorized."
        ),
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
            actor = await _get_current_sql_actor(
                session,
                access_token.subject,
            )
            if actor is None:
                if branch is not None:
                    return _json_error_result(
                        "AUTH_REQUIRED",
                        "Principal not found or disabled.",
                    )
                return _USER_ERROR

            started_at = time.perf_counter()
            result = await handle_run_sql(
                actor, session,
                sql=sql, instance_id=instance_id, database=database, branch=branch,
                max_rows=max_rows, cursor=cursor, confirm=confirm,
                session_factory=factory, background_tasks=_background_tasks,
            )
            response: Any = _handler_result(result)
            safe_metadata = _safe_tool_result_metadata(result)
            logger.info(
                "tool.run_sql.completed",
                extra={
                    "actor_kind": _actor_kind(actor).value,
                    "actor_id": actor.id,
                    "instance_id": instance_id,
                    "statement_count": 1,
                    "confirm": confirm,
                    "duration_ms": int(
                        (time.perf_counter() - started_at) * 1000
                    ),
                    **safe_metadata,
                },
            )
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

            started_at = time.perf_counter()
            result = await handle_list_branches(
                user, session, instance_id=instance_id,
                session_factory=factory, background_tasks=_background_tasks,
            )
            response = _handler_result(result)
            _log_user_tool_completion(
                tool="list_branches",
                user=user,
                started_at=started_at,
                result=result,
                instance_id=instance_id,
            )
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

            started_at = time.perf_counter()
            result = await handle_create_branch(
                user, session, branch_name=branch_name,
                include_databases=include_databases, instance_id=instance_id,
                session_factory=factory, background_tasks=_background_tasks,
            )
            response = _handler_result(result)
            _log_user_tool_completion(
                tool="create_branch",
                user=user,
                started_at=started_at,
                result=result,
                instance_id=instance_id,
            )
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

            started_at = time.perf_counter()
            result = await handle_delete_branch(
                user, session, branch_name=branch_name, instance_id=instance_id,
                session_factory=factory, background_tasks=_background_tasks,
            )
            response = _handler_result(result)
            _log_user_tool_completion(
                tool="delete_branch",
                user=user,
                started_at=started_at,
                result=result,
                instance_id=instance_id,
            )
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

            started_at = time.perf_counter()
            result = await handle_set_default_instance(user, session, instance_id=instance_id)
            response = _handler_result(result)
            _log_user_tool_completion(
                tool="set_default_instance",
                user=user,
                started_at=started_at,
                result=result,
                instance_id=instance_id,
            )
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
    ) -> Any:
        access_token = get_access_token()
        if not access_token or not access_token.subject:
            return _AUTH_ERROR

        factory = get_session_factory()
        async with factory() as session:
            actor = await _get_current_sql_actor(
                session,
                access_token.subject,
            )
            if actor is None:
                return _USER_ERROR

            started_at = time.perf_counter()
            result = await handle_run_sql_transaction(
                actor, session,
                sql_statements=sql_statements,
                instance_id=instance_id, database=database, confirm=confirm,
                session_factory=factory, background_tasks=_background_tasks,
            )
            response = _handler_result(result)
            safe_metadata = _safe_tool_result_metadata(result)
            logger.info(
                "tool.run_sql_transaction.completed",
                extra={
                    "actor_kind": _actor_kind(actor).value,
                    "actor_id": actor.id,
                    "instance_id": instance_id,
                    "statement_count": len(sql_statements),
                    "confirm": confirm,
                    "duration_ms": int(
                        (time.perf_counter() - started_at) * 1000
                    ),
                    **safe_metadata,
                },
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
    ) -> Any:
        access_token = get_access_token()
        if not access_token or not access_token.subject:
            return _AUTH_ERROR

        factory = get_session_factory()
        async with factory() as session:
            actor = await _get_current_sql_actor(
                session,
                access_token.subject,
            )
            if actor is None:
                return _USER_ERROR

            started_at = time.perf_counter()
            result = await handle_describe_schema(
                actor, session,
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
                "tool.describe_schema.completed",
                extra={
                    "actor_kind": _actor_kind(actor).value,
                    "actor_id": actor.id,
                    "instance_id": instance_id,
                    "duration_ms": int(
                        (time.perf_counter() - started_at) * 1000
                    ),
                    **_safe_tool_result_metadata(result),
                    "include_columns": include_columns,
                    "max_tables": max_tables,
                },
            )
            return response

    async def _resolve_db_instance_principal(
        session,
    ) -> Any | CallToolResult:
        try:
            return await resolve_request_principal(session)
        except PrincipalDisabled:
            return db_instance_result(
                {
                    "error": "AUTH_REQUIRED",
                    "message": "Principal has been disabled.",
                },
                is_error=True,
            )
        except PrincipalAuthenticationError:
            return db_instance_result(
                {
                    "error": "AUTH_REQUIRED",
                    "message": "Authentication required.",
                },
                is_error=True,
            )

    @mcp.tool(
        description="List database instances authorized for this principal.",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_db_instances(
        cursor: str | None = None,
        limit: int = 50,
        db_type: str | None = None,
        source: str | None = None,
        status: str | None = None,
    ) -> CallToolResult:
        factory = get_session_factory()
        async with factory() as session:
            principal = await _resolve_db_instance_principal(session)
            if isinstance(principal, CallToolResult):
                return principal
            result = await handle_list_db_instances(
                session,
                principal,
                cursor=cursor,
                limit=limit,
                db_type=db_type,
                source=source,
                status=status,
            )
            logger.info(
                "tool.list_db_instances | principal_kind=%s "
                "principal_id=%s is_error=%s",
                principal.kind.value,
                principal.id,
                result.isError,
            )
            return result

    @mcp.tool(
        description="Create a persistent PolarDB MySQL database resource.",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def create_db_instance(
        client_token: str,
        db_type: str,
        name: str | None = None,
    ) -> CallToolResult:
        factory = get_session_factory()
        async with factory() as session:
            principal = await _resolve_db_instance_principal(session)
            if isinstance(principal, CallToolResult):
                return principal
            result = await handle_create_db_instance(
                session,
                principal,
                client_token=client_token,
                db_type=db_type,
                name=name,
            )
            logger.info(
                "tool.create_db_instance | principal_kind=%s "
                "principal_id=%s is_error=%s",
                principal.kind.value,
                principal.id,
                result.isError,
            )
            return result

    @mcp.tool(
        description="Describe an authorized database instance.",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def describe_db_instance(
        db_instance_id: str,
    ) -> CallToolResult:
        factory = get_session_factory()
        async with factory() as session:
            principal = await _resolve_db_instance_principal(session)
            if isinstance(principal, CallToolResult):
                return principal
            result = await handle_describe_db_instance(
                session,
                principal,
                db_instance_id,
            )
            logger.info(
                "tool.describe_db_instance | principal_kind=%s "
                "principal_id=%s db_instance_id=%s is_error=%s",
                principal.kind.value,
                principal.id,
                db_instance_id,
                result.isError,
            )
            return result

    @mcp.tool(
        description="Delete an owned provisioned database resource.",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def delete_db_instance(
        db_instance_id: str,
    ) -> CallToolResult:
        factory = get_session_factory()
        async with factory() as session:
            principal = await _resolve_db_instance_principal(session)
            if isinstance(principal, CallToolResult):
                return principal
            result = await handle_delete_db_instance(
                session, principal, db_instance_id
            )
            logger.info(
                "tool.delete_db_instance | principal_kind=%s "
                "principal_id=%s db_instance_id=%s is_error=%s",
                principal.kind.value,
                principal.id,
                db_instance_id,
                result.isError,
            )
            return result

    _forbid_extra_tool_arguments(mcp, {
        "list_branches",
        "create_branch",
        "delete_branch",
        "list_db_instances",
        "create_db_instance",
        "describe_db_instance",
        "delete_db_instance",
    })
    _configure_db_instance_tool_schemas(mcp)

    return mcp


_mcp_server: AuthorizedFastMCP | None = None
_mcp_app: Starlette | None = None


class LazyMCPApplication:
    """Build the MCP app only after the runtime snapshot is installed."""

    async def __call__(self, scope, receive, send) -> None:
        await create_mcp_app()(scope, receive, send)


def _get_mcp_server() -> AuthorizedFastMCP:
    global _mcp_server
    if _mcp_server is None:
        _mcp_server = _build_mcp_server()
    return _mcp_server


def create_mcp_app() -> Starlette | DBInstanceMetricsMiddleware:
    global _mcp_app
    if _mcp_app is None:
        mcp = _get_mcp_server()
        _mcp_app = cast(Any, DBInstanceMetricsMiddleware(mcp.streamable_http_app()))
    return _mcp_app


def get_session_manager() -> StreamableHTTPSessionManager:
    create_mcp_app()  # ensures session manager is created
    return _get_mcp_server().session_manager


def reset_mcp() -> None:
    global _mcp_server, _mcp_app
    _mcp_server = None
    _mcp_app = None
    reset_describe_rate_limiters()


@asynccontextmanager
async def mcp_lifespan():
    from server.configuration.runtime import RuntimeSectionProxy

    cache = ConnectionCache(
        RuntimeSectionProxy(
            lambda: get_config().polardb.connection_pool
        )
    )
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
        reset_mcp()
