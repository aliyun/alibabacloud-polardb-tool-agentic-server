from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from server.management.features import KnowledgeEnvelope, knowledge_command
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from server.management.auth import management_authenticator
from server.management.accounts import (
    ManagedAccountError,
    ManagedAccountService,
)
from server.management.account_types import (
    ManagedAccount,
    ManagedAccountMutationResult,
    ManagedAccountPasswordEnvelope,
)
from server.management.identity import (
    ManagedIdentityBinder,
    ManagedIdentityError,
)
from server.management.settings import ListenerSettings
from server.management.status import build_management_status
from server.management.types import (
    ManagedEnvelope,
    ManagedTarget,
    ManagementStatus,
)
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ConfigError,
    ConfigResult,
)
from server.configuration.service import ConfigService


def create_management_app(
    runtime: Any,
    settings: ListenerSettings,
) -> FastAPI:
    app = FastAPI(
        title="PAS management",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    authenticate = management_authenticator(settings)

    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        error_types = {item["type"] for item in error.errors()}
        error_locations = {tuple(item["loc"]) for item in error.errors()}
        password_validation = _request.url.path == "/api/internal/v1/accounts/admin/password" and (
            "invalid_account_password_parameters" in error_types
            or any(location and location[-1] in {"old_password", "new_password"} for location in error_locations)
        )
        if password_validation:
            code = "INVALID_ACCOUNT_PASSWORD"
        elif "multiple_parameters_not_supported" in error_types:
            code = "MULTIPLE_PARAMETERS_NOT_SUPPORTED"
        else:
            code = "CONFIG_OPERATION_NOT_ALLOWED"
        return JSONResponse(
            status_code=400,
            content={
                "detail": {
                    "code": code,
                    "message": "Management command validation failed",
                }
            },
        )

    @app.exception_handler(ConfigError)
    async def config_error(
        _request: Request,
        error: ConfigError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": error.code,
                    "message": "Management configuration command failed",
                }
            },
        )

    @app.exception_handler(ManagedIdentityError)
    async def identity_error(
        _request: Request,
        error: ManagedIdentityError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": error.code,
                    "message": "Managed instance identity does not match",
                }
            },
        )

    @app.exception_handler(ManagedAccountError)
    async def account_error(
        _request: Request,
        error: ManagedAccountError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=(404 if error.code == "ACCOUNT_NOT_FOUND" else 409),
            content={
                "detail": {
                    "code": error.code,
                    "message": "Managed account operation failed",
                }
            },
        )

    async def require_config_runtime() -> None:
        state = getattr(runtime, "application_state", None)
        if getattr(state, "config_service", None) is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "MANAGEMENT_RUNTIME_UNAVAILABLE",
                    "message": "PAS configuration runtime is unavailable",
                },
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/api/internal/v1/status",
        response_model=ManagementStatus,
        dependencies=[Depends(authenticate)],
    )
    async def status() -> ManagementStatus:
        return await build_management_status(runtime, settings)

    @app.get("/api/internal/v1/schema", dependencies=[Depends(authenticate)])
    async def schema_inspection(instance_id: str, generation: int):
        from server.management.schema import inspect_runtime_schema
        identity = settings.managed_identity
        if identity is None or instance_id != identity.instance_id or generation != identity.generation:
            raise ManagedIdentityError("INSTANCE_IDENTITY_MISMATCH")
        return {**await inspect_runtime_schema(runtime), "instance_id": instance_id, "instance_generation": generation}

    @app.post("/api/internal/v1/features/knowledge", dependencies=[Depends(authenticate), Depends(require_config_runtime)])
    async def managed_knowledge(envelope: KnowledgeEnvelope):
        return await knowledge_command(runtime, settings, envelope)

    @app.post(
        "/api/internal/v1/config",
        dependencies=[
            Depends(authenticate),
            Depends(require_config_runtime),
        ],
    )
    async def config_command(
        envelope: ManagedEnvelope,
    ) -> ConfigResult:
        state = runtime.application_state
        service = state.config_service
        if not isinstance(service, ConfigService):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "MANAGEMENT_RUNTIME_UNAVAILABLE",
                    "message": "PAS configuration runtime is unavailable",
                },
            )
        identity = settings.managed_identity
        if identity is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "MANAGEMENT_RUNTIME_UNAVAILABLE",
                    "message": "Managed instance identity is unavailable",
                },
            )
        binder = ManagedIdentityBinder(service.repository, identity)
        if envelope.command.action == "describe":
            await binder.verify(envelope.target)
        else:
            await binder.bind(envelope.target)
        action = ConfigAction(envelope.command.action)
        config = (
            {
                str(envelope.command.parameter): envelope.command.value,
            }
            if envelope.command.parameter is not None
            else None
        )
        actor = ConfigActor(
            scope=(f"managed:{identity.instance_id}:{identity.generation}"),
            actor_type="managed_initializer",
            instance_id=identity.instance_id,
            instance_generation=identity.generation,
            lease_owner=f"management:{id(runtime)}",
        )
        return await service.execute(
            ConfigCommand(
                action=action,
                module=envelope.command.module,
                expected_revision=envelope.command.expected_revision,
                validation_id=envelope.command.validation_id,
                idempotency_key=envelope.command.idempotency_key,
                config=config,
            ),
            actor,
        )

    def account_service() -> ManagedAccountService:
        state = runtime.application_state
        service = state.config_service
        if not isinstance(service, ConfigService):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "MANAGEMENT_RUNTIME_UNAVAILABLE",
                    "message": "PAS configuration runtime is unavailable",
                },
            )
        identity = settings.managed_identity
        if identity is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "MANAGEMENT_RUNTIME_UNAVAILABLE",
                    "message": "Managed instance identity is unavailable",
                },
            )
        return ManagedAccountService(
            service,
            ManagedIdentityBinder(service.repository, identity),
        )

    @app.get(
        "/api/internal/v1/accounts",
        response_model=ManagedAccount,
        dependencies=[
            Depends(authenticate),
            Depends(require_config_runtime),
        ],
    )
    async def describe_account(
        instance_id: str,
        generation: int,
        account_name: str | None = None,
    ) -> ManagedAccount:
        if account_name not in {None, "admin"}:
            raise ManagedAccountError("ACCOUNT_NOT_FOUND")
        return await account_service().describe_admin(
            ManagedTarget(
                instance_id=instance_id,
                generation=generation,
            )
        )

    @app.post(
        "/api/internal/v1/accounts/admin/password",
        response_model=ManagedAccountMutationResult,
        dependencies=[
            Depends(authenticate),
            Depends(require_config_runtime),
        ],
    )
    async def mutate_account_password(
        envelope: ManagedAccountPasswordEnvelope,
    ) -> ManagedAccountMutationResult:
        return await account_service().mutate_admin_password(
            envelope,
            lease_owner=f"management:{id(runtime)}:{uuid4().hex}",
        )

    return app
