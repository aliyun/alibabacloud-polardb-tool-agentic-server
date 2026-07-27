from __future__ import annotations

import pytest

from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ConfigError,
)
from tests._configuration_helpers import create_config_context

ADMIN = ConfigActor(scope="admin:1", actor_type="admin")


@pytest.fixture
async def context():
    value = await create_config_context()
    yield value
    await value.close()


async def _validated_command(context) -> ConfigCommand:
    saved = await context.service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="agent_token_auth",
            expected_revision=0,
            config={"enabled": True},
        ),
        ADMIN,
    )
    validated = await context.service.execute(
        ConfigCommand(
            action=ConfigAction.VALIDATE,
            module="agent_token_auth",
            expected_revision=saved.module["revision"],
        ),
        ADMIN,
    )
    return ConfigCommand(
        action=ConfigAction.ACTIVATE,
        module="agent_token_auth",
        expected_revision=validated.module["revision"],
        validation_id=validated.validation["validation_id"],
        idempotency_key="same",
    )


async def test_repeated_activate_returns_stored_result(context) -> None:
    command = await _validated_command(context)
    first = await context.service.execute(command, ADMIN)
    second = await context.service.execute(command, ADMIN)
    assert second == first


async def test_idempotency_key_reuse_with_other_body_conflicts(
    context,
) -> None:
    command = await _validated_command(context)
    await context.service.execute(command, ADMIN)
    with pytest.raises(ConfigError) as exc:
        await context.service.execute(
            ConfigCommand(
                action=ConfigAction.DISABLE,
                module="agent_token_auth",
                expected_revision=command.expected_revision + 1,
                idempotency_key="same",
            ),
            ADMIN,
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
