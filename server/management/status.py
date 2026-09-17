from __future__ import annotations

from typing import Any

from server.configuration.service import ConfigService
from server.management.accounts import ManagedAccountService
from server.management.identity import ManagedIdentityBinder
from server.management.settings import ListenerSettings
from server.management.types import ManagedTarget, ManagementStatus


def _phase_value(runtime: Any) -> str:
    phase = getattr(runtime, "phase", "STARTING")
    return str(getattr(phase, "value", phase))


def _schema_status(phase: str) -> str:
    return {
        "SCHEMA_OUTDATED": "OUTDATED",
        "SCHEMA_TOO_NEW": "TOO_NEW",
        "FAILED": "UNAVAILABLE",
        "STARTING": "CHECKING",
    }.get(phase, "CURRENT")


async def build_management_status(
    runtime: Any,
    settings: ListenerSettings,
) -> ManagementStatus:
    phase = _phase_value(runtime)
    state = getattr(runtime, "application_state", None)
    access_policy = getattr(state, "runtime_access_policy", None)
    pas_state = (
        str(getattr(access_policy, "mode", phase))
        if phase in {"SETUP", "READY"}
        else phase
    )
    store = getattr(state, "runtime_config_store", None)

    desired_version: int | None = None
    loaded_version: int | None = None
    config_status = "UNAVAILABLE"
    if store is not None:
        loaded_version = getattr(store, "config_version", None)
        try:
            desired_version = await store.repository.global_version()
        except Exception:
            config_status = "UNAVAILABLE"
        else:
            if getattr(store, "last_error_code", None) is not None:
                config_status = "ERROR"
            elif desired_version != loaded_version:
                config_status = "STALE"
            elif getattr(store, "local_errors", None):
                config_status = "DEGRADED"
            else:
                config_status = "CURRENT"

    identity = settings.managed_identity
    credential_state = getattr(state, "credential_state", None)
    service = getattr(state, "config_service", None)
    if (
        credential_state is None
        and isinstance(service, ConfigService)
        and identity is not None
    ):
        try:
            account = await ManagedAccountService(
                service,
                ManagedIdentityBinder(service.repository, identity),
            ).describe_admin(
                ManagedTarget(
                    instance_id=identity.instance_id,
                    generation=identity.generation,
                )
            )
        except Exception:
            credential_state = None
        else:
            credential_state = account.password_status.value
    if credential_state is None:
        repository = getattr(service, "repository", None)
        read_password_state = getattr(
            repository, "get_admin_password_state", None
        )
        if read_password_state is not None:
            try:
                credential_state = await read_password_state()
            except Exception:
                credential_state = None
    return ManagementStatus(
        runtime_phase=phase,
        pas_state=pas_state,
        schema_status=_schema_status(phase),
        config_status=config_status,
        desired_config_version=desired_version,
        loaded_config_version=loaded_version,
        instance_id=(identity.instance_id if identity is not None else None),
        instance_generation=(
            identity.generation if identity is not None else None
        ),
        credential_state=credential_state,
    )
