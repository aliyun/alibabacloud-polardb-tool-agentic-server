from __future__ import annotations

import pytest

from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ConfigError,
)
from server.core.config_crypto import SecretEnvelope
from tests._configuration_helpers import (
    active_direct_config,
    create_config_context,
    save_aliyun_draft,
    seed_v1_direct_document,
    switch_to_assume_with_explicit_source_reuse,
)

ADMIN = ConfigActor(scope="admin:1", actor_type="admin")


@pytest.fixture
async def context():
    value = await create_config_context()
    yield value
    await value.close()


async def test_omitted_secret_preserves_draft_value(context) -> None:
    first = await save_aliyun_draft(
        context,
        {
            "credential_mode": "direct_ak",
            "direct_ak": {
                "access_key_id": "ak",
                "access_key_secret": "original",
            },
        },
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
    assert second.module["draft"]["direct_ak"]["access_key_secret"][
        "configured"
    ]
    internal = await context.service.describe_internal("aliyun_access")
    envelope = SecretEnvelope.model_validate(
        internal.draft["direct_ak"]["access_key_secret"]["$secret"]
    )
    assert (
        context.crypto.decrypt_field(
            envelope,
            module="aliyun_access",
            field_path="direct_ak.access_key_secret",
            schema_version=2,
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


async def test_nested_configured_placeholder_is_rejected(context) -> None:
    with pytest.raises(ConfigError) as exc:
        await save_aliyun_draft(
            context,
            {
                "credential_mode": "direct_ak",
                "direct_ak": {
                    "access_key_id": {"configured": True},
                    "access_key_secret": "secret",
                },
            },
        )
    assert exc.value.code == "INVALID_SECRET_INPUT"


async def test_export_omits_secrets_and_lists_metadata(context) -> None:
    await save_aliyun_draft(
        context,
        {
            "credential_mode": "direct_ak",
            "direct_ak": {
                "access_key_id": "ak",
                "access_key_secret": "secret",
            },
        },
    )
    result = await context.service.execute(
        ConfigCommand(action=ConfigAction.EXPORT),
        ADMIN,
    )
    exported = result.export["modules"]["aliyun_access"]
    assert "direct_ak" not in exported["config"]
    assert exported["metadata"]["configured_secret_fields"] == [
        "direct_ak.access_key_id",
        "direct_ak.access_key_secret",
    ]


async def test_assume_role_export_omits_all_nested_secrets(context) -> None:
    source_secret = "source-secret"
    external_id = "external-identity"
    await save_aliyun_draft(
        context,
        {
            "credential_mode": "assume_role",
            "assume_role": {
                "source_access_key_id": "TEST1234567890ABCD",
                "source_access_key_secret": source_secret,
                "role_arn": "acs:ram::123456789012:role/polardb",
                "role_session_name": "polardb-agentic",
                "external_id": external_id,
            },
        },
    )

    result = await context.service.execute(
        ConfigCommand(action=ConfigAction.EXPORT), ADMIN
    )
    exported = result.export["modules"]["aliyun_access"]
    serialized = str(exported)
    assert source_secret not in serialized
    assert external_id not in serialized
    assert exported["config"]["assume_role"] == {
        "role_arn": "acs:ram::123456789012:role/polardb",
        "role_session_name": "polardb-agentic",
    }
    assert exported["metadata"]["configured_secret_fields"] == [
        "assume_role.source_access_key_id",
        "assume_role.source_access_key_secret",
        "assume_role.external_id",
    ]


async def test_v1_export_omits_flat_secret_envelopes(context) -> None:
    await seed_v1_direct_document(context)

    result = await context.service.execute(
        ConfigCommand(action=ConfigAction.EXPORT, module="aliyun_access"),
        ADMIN,
    )

    exported = result.export["modules"]["aliyun_access"]
    assert "access_key_id" not in exported["config"]
    assert "access_key_secret" not in exported["config"]
    assert "$secret" not in str(exported)
    assert exported["metadata"]["configured_secret_fields"] == [
        "access_key_id",
        "access_key_secret",
    ]


async def test_nested_access_key_is_encrypted_masked_and_preserved(context) -> None:
    saved = await save_aliyun_draft(
        context,
        {
            "credential_mode": "direct_ak",
            "direct_ak": {
                "access_key_id": "TEST1234567890ABCD",
                "access_key_secret": "fixed-secret",
            },
        },
    )

    public = saved.module["draft"]["direct_ak"]
    assert public["access_key_id"]["display_hint"] == "TEST****ABCD"
    assert public["access_key_secret"] == {
        "configured": True,
        "updated_at": public["access_key_secret"]["updated_at"],
    }
    internal = await context.service.describe_internal("aliyun_access")
    assert "$secret" in internal.draft["direct_ak"]["access_key_secret"]
    assert "fixed-secret" not in internal.model_dump_json()
    assert "TEST1234567890ABCD" not in internal.model_dump_json()


async def test_reused_direct_source_stays_encrypted_at_its_new_path(
    context,
) -> None:
    direct = await active_direct_config(context)

    await switch_to_assume_with_explicit_source_reuse(
        context, direct.module["revision"]
    )

    internal = await context.service.describe_internal("aliyun_access")
    stored = internal.model_dump_json()
    assert "direct-secret" not in stored
    assert "TEST1234567890ABCD" not in stored
    assert (
        internal.draft["direct_ak"]["access_key_secret"]["$secret"]
        != internal.draft["assume_role"]["source_access_key_secret"]["$secret"]
    )


async def test_inactive_retained_block_is_redacted_without_decryption(
    context, monkeypatch
) -> None:
    first = await save_aliyun_draft(
        context,
        {
            "credential_mode": "direct_ak",
            "direct_ak": {
                "access_key_id": "TEST1234567890ABCD",
                "access_key_secret": "fixed-secret",
            },
        },
    )
    await save_aliyun_draft(
        context,
        {
            "credential_mode": "assume_role",
            "transition": {"previous_mode_action": "retain"},
            "assume_role": {
                "source_access_key_id": "TEST0987654321WXYZ",
                "source_access_key_secret": "source-secret",
                "role_arn": "acs:ram::123456789012:role/polardb",
                "role_session_name": "polardb-agentic",
            },
        },
        expected_revision=first.module["revision"],
    )
    monkeypatch.setattr(
        context.crypto,
        "decrypt_field",
        lambda *args, **kwargs: pytest.fail("must not decrypt"),
    )

    result = context.service._public_module(
        "aliyun_access",
        await context.service.describe_internal("aliyun_access"),
    )

    assert result["draft"]["direct_ak"]["access_key_id"][
        "display_hint"
    ] == "TEST****ABCD"
    assert result["draft"]["assume_role"]["source_access_key_id"][
        "display_hint"
    ] == "TEST****WXYZ"
