from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.agent_dependencies import AgentPrincipal, get_current_agent
from server.core.db_instance_application_service import (
    CreateDBInstanceCommand,
    DBInstanceApplicationService,
    DBInstanceView,
)
from server.core.db_instance_service import (
    CapacityExhausted,
    DBInstanceNotFound,
    IdempotencyConflict,
    InvalidClientToken,
    NoProvisioningBackend,
    UnsupportedDBType,
)
from server.core.provisioning_operation_budget import RateLimited
from server.db.engine import get_session
from server.logging import normalize_request_id, trace_id_var
from server.models import ProvisioningMode

RETRY_AFTER_SECONDS = 5
RETRYABLE_STATUSES = {"CREATING", "RESTORING", "DELETING"}


class CreateDBInstanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_token: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=128)
    db_type: Literal["polardb_mysql"]
    provisioning_mode: ProvisioningMode


class DBConnectionResponse(BaseModel):
    host: str
    port: int
    database: str
    username: str
    password: str = Field(
        json_schema_extra={"writeOnly": True, "x-sensitive": True}
    )


class DBInstanceResponse(BaseModel):
    resource_id: str
    status: Literal[
        "CREATING",
        "READY",
        "FAILED",
        "DELETING",
        "COOLING_DOWN",
        "RESTORING",
        "DELETED",
        "DELETE_FAILED",
    ]
    provisioning_mode: ProvisioningMode
    name: str | None = None
    db_type: str
    source: str
    connection: DBConnectionResponse | None = None
    retry_after_seconds: int | None = None
    failure_reason: str | None = None
    delete_requested_at: str | None = None
    disconnected_at: str | None = None
    cooldown_until: str | None = None
    delete_cooldown_duration_hours: int | None = None
    reclaim_policy: str | None = None


class AgentRESTError(BaseModel):
    code: Literal[
        "INVALID_ARGUMENT",
        "UNSUPPORTED_PROVISIONING_MODE",
        "NO_ELIGIBLE_BACKEND",
        "POOL_CAPACITY_LIMIT_REACHED",
        "IDEMPOTENCY_CONFLICT",
        "RESOURCE_STATE_CONFLICT",
        "PROVISIONING_FAILED",
        "DISCONNECT_FAILED",
        "RESOURCE_NOT_FOUND",
        "RATE_LIMITED",
        "UNAUTHORIZED",
    ]
    message: str
    request_id: str
    retry_after_seconds: int | None = None


def _request_id() -> str:
    return trace_id_var.get("") or normalize_request_id(None)


def _error(
    status_code: int,
    code: str,
    message: str,
    *,
    retry_after_seconds: int | None = None,
) -> JSONResponse:
    content: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": _request_id(),
    }
    headers = {"Cache-Control": "no-store"}
    if retry_after_seconds is not None:
        content["retry_after_seconds"] = retry_after_seconds
        headers["Retry-After"] = str(retry_after_seconds)
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers=headers,
    )


def _view_payload(view: DBInstanceView) -> dict[str, Any]:
    metadata = view.metadata or {}
    payload: dict[str, Any] = {
        "resource_id": view.resource_id,
        "status": view.status,
        "provisioning_mode": view.provisioning_mode.value,
        "name": view.name,
        "db_type": view.db_type,
        "source": view.source,
        "connection": None,
        "retry_after_seconds": (
            RETRY_AFTER_SECONDS
            if view.status in RETRYABLE_STATUSES
            else None
        ),
        "failure_reason": view.failure_reason,
        "delete_requested_at": metadata.get("delete_requested_at"),
        "disconnected_at": metadata.get("disconnected_at"),
        "cooldown_until": metadata.get("cooldown_until"),
        "delete_cooldown_duration_hours": metadata.get(
            "delete_cooldown_duration_hours"
        ),
        "reclaim_policy": metadata.get("reclaim_policy"),
    }
    if view.connection is not None:
        payload["connection"] = {
            "host": view.connection.host,
            "port": view.connection.port,
            "database": view.connection.database,
            "username": view.connection.username,
            "password": view.connection.password,
        }
    return payload


def _service_error(error: Exception) -> JSONResponse:
    if isinstance(error, DBInstanceNotFound):
        return _error(404, "RESOURCE_NOT_FOUND", "Database resource not found.")
    if isinstance(error, IdempotencyConflict):
        return _error(409, "IDEMPOTENCY_CONFLICT", str(error))
    if isinstance(error, NoProvisioningBackend):
        return _error(409, "NO_ELIGIBLE_BACKEND", str(error))
    if isinstance(error, CapacityExhausted):
        return _error(
            429,
            "POOL_CAPACITY_LIMIT_REACHED",
            str(error),
            retry_after_seconds=RETRY_AFTER_SECONDS,
        )
    if isinstance(error, RateLimited):
        return _error(
            429,
            "RATE_LIMITED",
            "The Agent operation budget is exhausted.",
            retry_after_seconds=RETRY_AFTER_SECONDS,
        )
    if isinstance(error, (InvalidClientToken, UnsupportedDBType)):
        return _error(422, "INVALID_ARGUMENT", str(error))
    if isinstance(error, ValueError):
        return _error(
            422,
            "INVALID_ARGUMENT",
            "The database request is invalid.",
        )
    return _error(500, "PROVISIONING_FAILED", "Database provisioning failed.")


class AgentRESTRoute(APIRoute):
    """Keep dependency and validation failures in the stable Agent shape."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return _error(
                    422,
                    "INVALID_ARGUMENT",
                    "The request does not match the Agent REST contract.",
                )
            except HTTPException as error:
                detail = error.detail if isinstance(error.detail, dict) else {}
                return _error(
                    error.status_code,
                    str(detail.get("code") or "UNAUTHORIZED"),
                    str(detail.get("message") or "Authentication failed."),
                )

        return handler


def build_agent_db_instance_router(
    *, include_in_schema: bool
) -> APIRouter:
    router = APIRouter(
        prefix="/mcp/rest",
        tags=["agent-db-instances"],
        route_class=AgentRESTRoute,
    )

    @router.post(
        "/db-instances",
        response_model=DBInstanceResponse,
        status_code=201,
        include_in_schema=include_in_schema,
        responses={
            201: {"model": DBInstanceResponse},
            202: {"model": DBInstanceResponse},
            409: {"model": AgentRESTError},
            422: {"model": AgentRESTError},
            429: {"model": AgentRESTError},
        },
    )
    async def create_db_instance(
        body: CreateDBInstanceRequest,
        principal: AgentPrincipal = Depends(get_current_agent),
        session: AsyncSession = Depends(get_session),
    ) -> Response:
        try:
            view = await DBInstanceApplicationService(session).create(
                CreateDBInstanceCommand(
                    agent_id=principal.agent_id,
                    mode=body.provisioning_mode,
                    idempotency_key=body.client_token,
                    name=body.name,
                    db_type=body.db_type,
                )
            )
        except Exception as error:
            return _service_error(error)
        response_status = (
            status.HTTP_201_CREATED
            if view.status == "READY"
            else status.HTTP_202_ACCEPTED
            if view.status == "CREATING"
            else status.HTTP_200_OK
        )
        headers = {
            "Location": f"/mcp/rest/db-instances/{view.resource_id}",
            "Cache-Control": "no-store",
        }
        if view.status == "CREATING":
            headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
        return JSONResponse(
            status_code=response_status,
            content=_view_payload(view),
            headers=headers,
        )

    @router.get(
        "/db-instances/{resource_id}",
        response_model=DBInstanceResponse,
        status_code=200,
        include_in_schema=include_in_schema,
        responses={404: {"model": AgentRESTError}},
    )
    async def describe_db_instance(
        resource_id: str,
        principal: AgentPrincipal = Depends(get_current_agent),
        session: AsyncSession = Depends(get_session),
    ) -> Response:
        try:
            view = await DBInstanceApplicationService(session).describe(
                agent_id=principal.agent_id,
                resource_id=resource_id,
            )
        except Exception as error:
            return _service_error(error)
        headers = {"Cache-Control": "no-store"}
        if view.status in RETRYABLE_STATUSES:
            headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
        return JSONResponse(content=_view_payload(view), headers=headers)

    @router.delete(
        "/db-instances/{resource_id}",
        response_model=DBInstanceResponse,
        status_code=202,
        include_in_schema=include_in_schema,
        responses={
            202: {"model": DBInstanceResponse},
            204: {"description": "Resource is permanently deleted."},
            404: {"model": AgentRESTError},
        },
    )
    async def delete_db_instance(
        resource_id: str,
        principal: AgentPrincipal = Depends(get_current_agent),
        session: AsyncSession = Depends(get_session),
    ) -> Response:
        service = DBInstanceApplicationService(session)
        try:
            current = await service.get_resource(
                agent_id=principal.agent_id,
                resource_id=resource_id,
            )
            current_status = current.status.value
            await session.rollback()
            if current_status == "deleted":
                return Response(status_code=204)
            view = await service.delete(
                agent_id=principal.agent_id,
                resource_id=resource_id,
            )
            # Read the authoritative delete metadata after the mutation commits.
            view = await service.describe(
                agent_id=principal.agent_id,
                resource_id=view.resource_id,
            )
        except Exception as error:
            return _service_error(error)
        return JSONResponse(
            status_code=202,
            content=_view_payload(view),
            headers={"Cache-Control": "no-store"},
        )

    return router


router = build_agent_db_instance_router(include_in_schema=False)
