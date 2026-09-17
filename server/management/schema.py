"""Authenticated, read-only rollout verification on the current replica."""

from __future__ import annotations

import httpx
from sqlalchemy import select

from server.db.schema import DatabaseSchemaError, check_database_compatibility, inspect_database


async def inspect_runtime_schema(runtime):
    result = await inspect_database()
    result["runtime_phase"] = str(getattr(runtime, "phase", "STARTING"))
    result["configuration_compatible"] = False
    result["business_smoke"] = "PENDING"
    if not result["compatible"]:
        return result
    try:
        await check_database_compatibility()
        result["configuration_compatible"] = True
    except DatabaseSchemaError as error:
        result["error_code"] = error.code
        return result
    state = getattr(runtime, "application_state", None)
    app = getattr(runtime, "application", None)
    service = getattr(state, "config_service", None)
    if result["runtime_phase"] != "READY" or service is None or app is None:
        return result
    from server.auth.jwt_manager import create_access_token
    from server.models import AuthProvider, User, UserRole, UserStatus

    document = await service.repository.get_module("core_admin")
    if document is None or document.effective is None:
        return result
    async with service.repository.session_factory() as session:
        user = await session.scalar(
            select(User).where(
                User.external_id == document.effective.config["username"],
                User.auth_provider == AuthProvider.BUILTIN,
                User.role == UserRole.ADMIN,
                User.status == UserStatus.ACTIVE,
            )
        )
    if user is None:
        result["business_smoke"] = "FAILED"
        return result
    # The probe token never leaves this process or enters diagnostics.
    token = create_access_token({"sub": user.id, "role": "admin", "credential_epoch": user.credential_epoch})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://pas-health") as client:
        response = await client.get("/api/agents", params={"limit": 1}, headers={"Authorization": "Bearer " + token})
    result["business_smoke"] = (
        "PASSED" if response.status_code == 200 and isinstance(response.json().get("items"), list) else "FAILED"
    )
    return result
