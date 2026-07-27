from __future__ import annotations

import pytest

from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ConfigError,
)
from server.core.config_crypto import SecretEnvelope
from tests._configuration_helpers import create_config_context

ADMIN = ConfigActor(scope="admin:1", actor_type="admin")


@pytest.fixture
async def context():
    value = await create_config_context()
    yield value
    await value.close()


async def test_omitted_secret_preserves_draft_value(context) -> None:
    first = await context.service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="aliyun_access",
            expected_revision=0,
            config={
                "credential_mode": "direct_ak",
                "access_key_id": "ak",
                "access_key_secret": "original",
            },
        ),
        ADMIN,
    )
    second = await context.service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="aliyun_access",
            expected_revision=first.module["revision"],
            config={"region_id": "cn-shanghai"},
        ),
        ADMIN,
    )
    assert second.module["draft"]["access_key_secret"] == {
        "configured": True
    }
    internal = await context.service.describe_internal("aliyun_access")
    envelope = SecretEnvelope.model_validate(
        internal.draft["access_key_secret"]["$secret"]
    )
    assert (
        context.crypto.decrypt_field(
            envelope,
            module="aliyun_access",
            field_path="access_key_secret",
            schema_version=1,
        )
        == "original"
    )


async def test_configured_placeholder_is_rejected(context) -> None:
    with pytest.raises(ConfigError) as exc:
        await context.service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="user_sso",
                expected_revision=0,
                config={
                    "client_id": "client",
                    "client_secret": {"configured": True},
                },
            ),
            ADMIN,
        )
    assert exc.value.code == "INVALID_SECRET_INPUT"


async def test_export_omits_secrets_and_lists_metadata(context) -> None:
    await context.service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="aliyun_access",
            expected_revision=0,
            config={
                "credential_mode": "direct_ak",
                "access_key_id": "ak",
                "access_key_secret": "secret",
            },
        ),
        ADMIN,
    )
    result = await context.service.execute(
        ConfigCommand(action=ConfigAction.EXPORT),
        ADMIN,
    )
    exported = result.export["modules"]["aliyun_access"]
    assert "access_key_id" not in exported["config"]
    assert "access_key_secret" not in exported["config"]
    assert exported["metadata"]["configured_secret_fields"] == [
        "access_key_id",
        "access_key_secret",
    ]
