from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import get_current_user
from server.db.engine import get_session
from server.mcp.tools import (
    handle_create_branch,
    handle_delete_branch,
    handle_describe_schema,
    handle_list_branches,
    handle_run_sql,
    handle_run_sql_transaction,
    handle_set_default_instance,
)
from server.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp/rest", tags=["mcp-rest"])


class RunSQLRequest(BaseModel):
    sql: str = Field(min_length=1)
    instance_id: str | None = None
    database: str | None = None
    branch: str | None = None
    max_rows: int = Field(default=1000, ge=1, strict=True)
    cursor: str | None = None
    confirm: bool = False


class RunSQLTransactionRequest(BaseModel):
    sql_statements: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    instance_id: str | None = None
    database: str | None = None
    confirm: bool = False


class SetDefaultInstanceRequest(BaseModel):
    instance_id: str


class DescribeSchemaRequest(BaseModel):
    database: str | None = None
    instance_id: str | None = None
    table_pattern: str | None = None
    include_columns: bool = True
    cursor: str | None = None
    max_tables: int = Field(default=20, ge=1, strict=True)


class BranchToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListBranchesRequest(BranchToolRequest):
    instance_id: str | None = None


class CreateBranchRequest(BranchToolRequest):
    branch_name: str = Field(min_length=1)
    instance_id: str | None = None
    include_databases: list[Annotated[str, Field(min_length=1)]] | None = None


class DeleteBranchRequest(BranchToolRequest):
    branch_name: str = Field(min_length=1)
    instance_id: str | None = None


class ToolCallRequest(BaseModel):
    method: str = "tools/call"
    params: dict = {}


def _nullable_string_schema(description: str) -> dict:
    return {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
        "description": description,
    }


@router.post("/run_sql")
async def run_sql(
    body: RunSQLRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Execute SQL against a PolarDB instance."""
    return await handle_run_sql(
        user, session,
        sql=body.sql,
        instance_id=body.instance_id,
        database=body.database,
        branch=body.branch,
        max_rows=body.max_rows,
        cursor=body.cursor,
        confirm=body.confirm,
        session_factory=getattr(request.app.state, "session_factory", None),
        background_tasks=getattr(request.app.state, "background_tasks", None),
    )


@router.post("/run_sql_transaction")
async def run_sql_transaction(
    body: RunSQLTransactionRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Execute multiple SQL statements in a single transaction."""
    return await handle_run_sql_transaction(
        user, session,
        sql_statements=body.sql_statements,
        instance_id=body.instance_id,
        database=body.database,
        confirm=body.confirm,
        session_factory=getattr(request.app.state, "session_factory", None),
        background_tasks=getattr(request.app.state, "background_tasks", None),
    )


@router.post("/set_default_instance")
async def set_default_instance(
    body: SetDefaultInstanceRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Set the user's default PolarDB instance."""
    return await handle_set_default_instance(user, session, instance_id=body.instance_id)


@router.post("/describe_schema")
async def describe_schema(
    request: Request,
    body: DescribeSchemaRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Discover database tables and their COMMENTs."""
    body = body or DescribeSchemaRequest()
    return await handle_describe_schema(
        user, session,
        instance_id=body.instance_id,
        database=body.database,
        table_pattern=body.table_pattern,
        include_columns=body.include_columns,
        cursor=body.cursor,
        max_tables=body.max_tables,
        session_factory=getattr(request.app.state, "session_factory", None),
        background_tasks=getattr(request.app.state, "background_tasks", None),
    )


@router.post("/list_branches")
async def list_branches(
    request: Request,
    body: ListBranchesRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List branches on a PolarDB instance."""
    body = body or ListBranchesRequest()
    return await handle_list_branches(
        user, session,
        instance_id=body.instance_id,
        session_factory=getattr(request.app.state, "session_factory", None),
        background_tasks=getattr(request.app.state, "background_tasks", None),
    )


@router.post("/create_branch")
async def create_branch(
    body: CreateBranchRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Create a branch on a PolarDB instance."""
    return await handle_create_branch(
        user, session,
        branch_name=body.branch_name,
        include_databases=body.include_databases,
        instance_id=body.instance_id,
        session_factory=getattr(request.app.state, "session_factory", None),
        background_tasks=getattr(request.app.state, "background_tasks", None),
    )


@router.post("/delete_branch")
async def delete_branch(
    body: DeleteBranchRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Delete a branch on a PolarDB instance."""
    return await handle_delete_branch(
        user, session,
        branch_name=body.branch_name,
        instance_id=body.instance_id,
        session_factory=getattr(request.app.state, "session_factory", None),
        background_tasks=getattr(request.app.state, "background_tasks", None),
    )


@router.get("/tools")
async def list_tools():
    """List available MCP tools."""
    return {
        "tools": [
            {
                "name": "run_sql",
                "description": "Execute a SQL statement against a PolarDB MySQL instance.",
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "SQL statement to execute",
                            "minLength": 1,
                        },
                        "instance_id": {
                            "type": "string",
                            "description": (
                                "Optional. When omitted, the server resolves the target instance "
                                "automatically. Required only when the user has multiple instances "
                                "and wants to target a specific one."
                            ),
                        },
                        "database": {"type": "string", "description": "Target database name."},
                        "branch": _nullable_string_schema("Optional branch name to execute SQL on."),
                        "max_rows": {
                            "type": "integer",
                            "description": "Maximum rows to return (default 1000).",
                            "default": 1000,
                            "minimum": 1,
                        },
                        "cursor": {"type": "string", "description": "Pagination cursor from previous response."},
                        "confirm": {
                            "type": "boolean",
                            "description": (
                                "Set to true to confirm execution of destructive SQL "
                                "(DROP/TRUNCATE/ALTER). Required after receiving a destructive warning."
                            ),
                            "default": False,
                        },
                    },
                    "required": ["sql"],
                },
            },
            {
                "name": "set_default_instance",
                "description": "Set the default PolarDB instance for the current user. Subsequent run_sql calls without instance_id will route to this instance.",
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "instance_id": {
                            "type": "string",
                            "description": "The instance ID to set as default.",
                        },
                    },
                    "required": ["instance_id"],
                },
            },
            {
                "name": "run_sql_transaction",
                "description": (
                    "Execute multiple SQL statements in a single transaction. "
                    "The server wraps them in BEGIN/COMMIT automatically. "
                    "If any statement fails, the entire transaction is rolled back."
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sql_statements": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                            "description": "List of SQL statements to execute in a transaction.",
                        },
                        "instance_id": {
                            "type": "string",
                            "description": (
                                "Optional. When omitted, the server resolves the target instance "
                                "automatically."
                            ),
                        },
                        "database": {"type": "string", "description": "Target database name."},
                        "confirm": {
                            "type": "boolean",
                            "description": (
                                "Set to true to confirm execution of destructive SQL "
                                "(DROP/TRUNCATE/ALTER). Required after receiving a destructive warning."
                            ),
                            "default": False,
                        },
                    },
                    "required": ["sql_statements"],
                },
            },
            {
                "name": "list_branches",
                "description": "List branches on a PolarDB instance.",
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "instance_id": _nullable_string_schema("Optional instance ID."),
                    },
                },
            },
            {
                "name": "create_branch",
                "description": "Create a PolarDB branch from the default branch.",
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "branch_name": {
                            "type": "string",
                            "description": "Name of the branch to create.",
                            "minLength": 1,
                        },
                        "include_databases": {
                            "anyOf": [
                                {"type": "array", "items": {"type": "string", "minLength": 1}},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Optional database names for WITH DATABASE.",
                        },
                        "instance_id": _nullable_string_schema("Optional instance ID."),
                    },
                    "required": ["branch_name"],
                },
            },
            {
                "name": "delete_branch",
                "description": (
                    "Delete a PolarDB branch. This is destructive; never call it "
                    "autonomously and ask the user first."
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "branch_name": {
                            "type": "string",
                            "description": "Name of the branch to delete.",
                            "minLength": 1,
                        },
                        "instance_id": _nullable_string_schema("Optional instance ID."),
                    },
                    "required": ["branch_name"],
                },
            },
            {
                "name": "describe_schema",
                "description": (
                    "Discover database tables and their semantic descriptions (COMMENTs). "
                    "Use this to understand what data already exists before creating new tables."
                ),
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "database": {"type": "string", "description": "Target database name. Defaults to resolved database."},
                        "instance_id": {"type": "string", "description": "Optional instance ID."},
                        "table_pattern": {"type": "string", "description": "Filter tables by name using SQL LIKE pattern (e.g. '%cars%')."},
                        "include_columns": {"type": "boolean", "description": "Include column details (default true).", "default": True},
                        "cursor": {"type": "string", "description": "Pagination cursor from previous response."},
                        "max_tables": {
                            "type": "integer",
                            "description": "Max tables per page (default 20, max 100).",
                            "default": 20,
                            "minimum": 1,
                        },
                    },
                },
            },
        ],
    }
