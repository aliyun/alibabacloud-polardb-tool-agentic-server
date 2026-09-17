from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

from server.bootstrap import read_restricted_secret_file


DEFAULT_BUSINESS_PORT = 18760
SUPPORTED_AUTH_MODES = frozenset({"trusted-network", "bearer-token"})

ManagementAuthMode = Literal["trusted-network", "bearer-token"]


class ListenerConfigError(ValueError):
    """Raised when listener or managed-identity settings are unsafe."""


@dataclass(frozen=True, slots=True)
class ManagedIdentitySettings:
    instance_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class ListenerSettings:
    business_port: int
    management_port: int | None
    auth_mode: ManagementAuthMode | None
    token: bytes | None = field(repr=False)
    managed_identity: ManagedIdentitySettings | None
    listen_host: str = "0.0.0.0"
    local_sso_dev_mode: bool = False


def _parse_port(source: Mapping[str, str], name: str, *, default: int | None = None) -> int | None:
    if name not in source:
        return default
    raw_value = source[name].strip()
    try:
        port = int(raw_value)
    except ValueError as error:
        raise ListenerConfigError(f"{name} must be an integer in 1..65535") from error
    if not 1 <= port <= 65535:
        raise ListenerConfigError(f"{name} must be an integer in 1..65535")
    return port


def _managed_identity(source: Mapping[str, str], *, required: bool) -> ManagedIdentitySettings | None:
    instance_id = source.get("PAS_MANAGED_INSTANCE_ID", "").strip()
    generation_value = source.get("PAS_MANAGED_INSTANCE_GENERATION", "").strip()
    if not required and not instance_id and not generation_value:
        return None
    if not instance_id or not generation_value:
        raise ListenerConfigError(
            "PAS_MANAGED_INSTANCE_ID and PAS_MANAGED_INSTANCE_GENERATION are required together"
        )
    try:
        generation = int(generation_value)
    except ValueError as error:
        raise ListenerConfigError("PAS_MANAGED_INSTANCE_GENERATION must be a positive integer") from error
    if generation <= 0:
        raise ListenerConfigError("PAS_MANAGED_INSTANCE_GENERATION must be a positive integer")
    return ManagedIdentitySettings(instance_id=instance_id, generation=generation)


def _read_bearer_token(source: Mapping[str, str]) -> bytes:
    token_file = source.get("PAS_MANAGEMENT_TOKEN_FILE", "").strip()
    if not token_file:
        raise ListenerConfigError("PAS_MANAGEMENT_TOKEN_FILE is required for bearer-token mode")
    try:
        token = read_restricted_secret_file(
            token_file,
            setting_name="PAS_MANAGEMENT_TOKEN_FILE",
        ).encode("utf-8")
    except ValueError as error:
        raise ListenerConfigError(str(error)) from error
    if not token:
        raise ListenerConfigError("PAS_MANAGEMENT_TOKEN_FILE is empty")
    return token


def load_listener_settings(
    env: Mapping[str, str] | None = None,
    *,
    local_sso_dev_mode: bool = False,
) -> ListenerSettings:
    source = os.environ if env is None else env
    listen_host = "127.0.0.1" if local_sso_dev_mode else "0.0.0.0"
    if "PAS_MANAGEMENT_TOKEN" in source:
        raise ListenerConfigError(
            "PAS_MANAGEMENT_TOKEN is not supported; use PAS_MANAGEMENT_TOKEN_FILE"
        )

    business_port = _parse_port(source, "PAS_SERVER_PORT", default=DEFAULT_BUSINESS_PORT)
    assert business_port is not None
    management_port = _parse_port(source, "PAS_MANAGEMENT_PORT")
    raw_auth_mode = source.get("PAS_MANAGEMENT_AUTH_MODE", "").strip()

    if management_port is None:
        if raw_auth_mode:
            raise ListenerConfigError("PAS_MANAGEMENT_PORT is required when PAS_MANAGEMENT_AUTH_MODE is set")
        return ListenerSettings(
            business_port=business_port,
            management_port=None,
            auth_mode=None,
            token=None,
            managed_identity=None,
            listen_host=listen_host,
            local_sso_dev_mode=local_sso_dev_mode,
        )

    if management_port == business_port:
        raise ListenerConfigError("PAS_SERVER_PORT and PAS_MANAGEMENT_PORT must be distinct")
    if raw_auth_mode not in SUPPORTED_AUTH_MODES:
        raise ListenerConfigError(
            "PAS_MANAGEMENT_AUTH_MODE must be trusted-network or bearer-token"
        )
    auth_mode = cast(ManagementAuthMode, raw_auth_mode)

    if auth_mode == "trusted-network":
        if source.get("PAS_MANAGEMENT_TOKEN_FILE", "").strip():
            raise ListenerConfigError(
                "PAS_MANAGEMENT_TOKEN_FILE is not allowed in trusted-network mode"
            )
        identity = _managed_identity(source, required=True)
        token = None
    else:
        identity = _managed_identity(source, required=False)
        token = _read_bearer_token(source)

    return ListenerSettings(
        business_port=business_port,
        management_port=management_port,
        auth_mode=auth_mode,
        token=token,
        managed_identity=identity,
        listen_host=listen_host,
        local_sso_dev_mode=local_sso_dev_mode,
    )
