from __future__ import annotations

from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ConfigError,
)


def authorize_managed_command(
    command: ConfigCommand,
    actor: ConfigActor,
) -> None:
    if actor.actor_type != "managed_initializer":
        return
    allowed = command.module == "core_admin" and command.action in {
        ConfigAction.DESCRIBE,
        ConfigAction.SAVE_DRAFT,
        ConfigAction.VALIDATE,
        ConfigAction.ACTIVATE,
        ConfigAction.SET_INITIAL_PASSWORD,
    }
    if command.action == ConfigAction.SAVE_DRAFT:
        allowed = allowed and set(command.config or {}) == {"username"}
    elif command.action == ConfigAction.SET_INITIAL_PASSWORD:
        allowed = allowed and set(command.config or {}) == {"password"}
    elif command.config:
        allowed = False
    if not allowed:
        raise ConfigError(
            "CONFIG_OPERATION_NOT_ALLOWED",
            "Managed configuration operation is not allowed",
        )
    if (
        actor.instance_id is None
        or actor.instance_generation is None
        or actor.lease_owner is None
    ):
        raise ConfigError(
            "INSTANCE_IDENTITY_MISMATCH",
            "Managed instance identity is required",
        )
