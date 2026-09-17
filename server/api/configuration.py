from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from server.auth.dependencies import validate_access_token_user
from server.configuration.bootstrap import verify_bootstrap_token
from server.configuration.service import ConfigService
from server.configuration.types import (
    ConfigActor,
    ConfigCommand,
    ConfigError,
    ConfigResult,
    SystemState,
)

router = APIRouter(tags=["configuration"])


def get_config_service(request: Request) -> ConfigService:
    service = getattr(request.app.state, "config_service", None)
    if not isinstance(service, ConfigService):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CONFIG_UNAVAILABLE",
                "message": "Configuration service is unavailable",
            },
        )
    return service


async def resolve_config_actor(
    request: Request,
    service: ConfigService = Depends(get_config_service),
) -> ConfigActor:
    if await service._system_state() == SystemState.SETUP:
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bootstrap" or not token or not await verify_bootstrap_token(service.repository, token):
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "BOOTSTRAP_REQUIRED",
                    "message": "A valid bootstrap token is required",
                },
            )
        return ConfigActor(
            scope="bootstrap",
            actor_type="bootstrap",
            credential_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        )

    from server.models import UserRole

    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    bearer_authenticated = scheme.lower() == "bearer"
    if not bearer_authenticated and request.headers.get("x-pas-csrf") != "1":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CSRF_REQUIRED",
                "message": "Browser configuration requires CSRF protection",
            },
        )
    token = token if bearer_authenticated else request.cookies.get("session_token", "")
    async with service.repository.session_factory() as session:
        user = await validate_access_token_user(token, session)
    if user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "Admin required"},
        )
    return ConfigActor(scope=f"admin:{user.id}", actor_type="admin")


@router.post("/config", response_model=ConfigResult)
async def config_command(
    body: ConfigCommand,
    request: Request,
    service: ConfigService = Depends(get_config_service),
    actor: ConfigActor = Depends(resolve_config_actor),
) -> JSONResponse:
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 1_048_576:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "CONFIG_TOO_LARGE",
                "message": "Configuration request exceeds 1 MiB",
            },
        )
    try:
        result = await service.execute(body, actor)
    except ConfigError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if result.system_state == SystemState.READY and hasattr(request.app.state, "runtime_access_policy"):
        from server.middleware.runtime_policy import (
            RuntimeAccessPolicy,
        )

        current = request.app.state.runtime_access_policy
        request.app.state.runtime_access_policy = RuntimeAccessPolicy(
            mode="READY",
            cors_allowed_origins=current.cors_allowed_origins,
            sso_active=current.sso_active,
        )
    return JSONResponse(
        result.model_dump(mode="json", exclude_none=True),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/config/user-sso/tests")
async def start_user_sso_test(
    service: ConfigService = Depends(get_config_service),
    actor: ConfigActor = Depends(resolve_config_actor),
) -> JSONResponse:
    try:
        result = await service.start_user_sso_test(actor)
    except ConfigError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/config/user-sso/tests/{test_id}")
async def get_user_sso_test(
    test_id: str,
    service: ConfigService = Depends(get_config_service),
    actor: ConfigActor = Depends(resolve_config_actor),
) -> JSONResponse:
    try:
        result = await service.describe_user_sso_test(test_id, actor)
    except ConfigError as exc:
        raise HTTPException(
            status_code=404 if exc.code == "SSO_TEST_NOT_FOUND" else 409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
