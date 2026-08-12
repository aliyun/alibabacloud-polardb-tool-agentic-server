from __future__ import annotations

import logging

import pytest

from server.configuration.runtime import project_app_config
from server.configuration.types import ConfigAction, ConfigActor, ConfigCommand
from server.configuration.types import ConfigError
from server.core.config_crypto import SecretEnvelope
from tests._configuration_helpers import (
    active_assume_with_retained_direct,
    active_direct_config,
    assume_input,
    save_draft,
    switch_to_assume_with_explicit_source_reuse,
)


@pytest.fixture
async def context():
    from tests._configuration_helpers import create_config_context

    value = await create_config_context()
    yield value
    await value.close()


async def test_switch_defaults_to_clearing_previous_block(context) -> None:
    direct = await active_direct_config(context)

    await save_draft(context, direct.module["revision"], assume_input())

    internal = await context.service.describe_internal("aliyun_access")
    assert internal.draft["credential_mode"] == "assume_role"
    assert "direct_ak" not in internal.draft


async def test_switch_can_retain_but_does_not_enable_previous_block(
    context,
) -> None:
    direct = await active_direct_config(context)

    await save_draft(
        context,
        direct.module["revision"],
        assume_input(
            transition={
                "previous_mode_action": "retain",
                "selected_mode_action": "replace",
            }
        ),
    )

    internal = await context.service.describe_internal("aliyun_access")
    assert "direct_ak" in internal.draft
    assert internal.draft["credential_mode"] == "assume_role"


async def test_reuse_direct_as_source_reencrypts_both_paths(context) -> None:
    direct = await active_direct_config(context)

    await switch_to_assume_with_explicit_source_reuse(
        context, direct.module["revision"]
    )

    internal = await context.service.describe_internal("aliyun_access")
    assert (
        internal.draft["direct_ak"]["access_key_id"]["$secret"]
        != internal.draft["assume_role"]["source_access_key_id"]["$secret"]
    )


async def test_retained_mode_can_be_deleted_without_switching(context) -> None:
    current = await active_assume_with_retained_direct(context)

    await save_draft(
        context,
        current.module["revision"],
        {
            "credential_mode": "assume_role",
            "transition": {"delete_retained_modes": ["direct_ak"]},
        },
    )

    internal = await context.service.describe_internal("aliyun_access")
    assert "direct_ak" not in internal.draft
    assert internal.draft["credential_mode"] == "assume_role"
    assert "transition" not in internal.draft


async def test_switching_back_requires_explicit_retained_reuse(context) -> None:
    current = await active_assume_with_retained_direct(context)

    with pytest.raises(ConfigError) as error:
        await save_draft(
            context,
            current.module["revision"],
            {"credential_mode": "direct_ak"},
        )

    assert error.value.code == "INVALID_CREDENTIAL_TRANSITION"


async def test_switching_back_reuses_retained_only_when_requested(
    context,
) -> None:
    current = await active_assume_with_retained_direct(context)

    await save_draft(
        context,
        current.module["revision"],
        {
            "credential_mode": "direct_ak",
            "transition": {"selected_mode_action": "reuse_retained"},
        },
    )

    internal = await context.service.describe_internal("aliyun_access")
    assert internal.draft["credential_mode"] == "direct_ak"
    assert "assume_role" not in internal.draft


async def test_runtime_uses_only_the_selected_credential_block(context) -> None:
    saved = await active_assume_with_retained_direct(context)
    actor = ConfigActor(scope="admin:1", actor_type="admin")
    validated = await context.service.execute(
        ConfigCommand(
            action=ConfigAction.VALIDATE,
            module="aliyun_access",
            expected_revision=saved.module["revision"],
        ),
        actor,
    )
    await context.service.execute(
        ConfigCommand(
            action=ConfigAction.ACTIVATE,
            module="aliyun_access",
            expected_revision=validated.module["revision"],
            validation_id=validated.validation["validation_id"],
            idempotency_key="activate-retained-direct",
        ),
        actor,
    )

    internal = await context.service.describe_internal("aliyun_access")
    runtime = project_app_config(
        {"aliyun_access": internal}, context.crypto
    ).aliyun
    assert "direct_ak" in internal.effective.config
    assert runtime.access_key_id == "TEST0987654321WXYZ"
    assert runtime.access_key_secret == "source-secret"


async def test_selected_mode_cannot_be_deleted(context) -> None:
    direct = await active_direct_config(context)

    with pytest.raises(ConfigError) as error:
        await save_draft(
            context,
            direct.module["revision"],
            {
                "credential_mode": "direct_ak",
                "transition": {"delete_retained_modes": ["direct_ak"]},
            },
        )

    assert error.value.code == "INVALID_CREDENTIAL_TRANSITION"


async def test_new_assume_role_generates_a_stable_session_name(context) -> None:
    direct = await active_direct_config(context)

    await save_draft(
        context,
        direct.module["revision"],
        assume_input(
            assume_role={
                "source_access_key_id": "TEST0987654321WXYZ",
                "source_access_key_secret": "source-secret",
                "role_arn": "acs:ram::123456789012:role/polardb",
            }
        ),
    )
    first = await context.service.describe_internal("aliyun_access")
    session_name = first.draft["assume_role"]["role_session_name"]

    await save_draft(
        context,
        first.revision,
        {"region_id": "cn-shanghai"},
    )
    refreshed = await context.service.describe_internal("aliyun_access")
    assert refreshed.draft["assume_role"]["role_session_name"] == session_name


async def test_block_only_patch_cannot_create_an_inactive_retained_block(
    context,
) -> None:
    direct = await active_direct_config(context)

    with pytest.raises(ConfigError) as error:
        await save_draft(
            context,
            direct.module["revision"],
            {
                "assume_role": {
                    "source_access_key_id": "TEST0987654321WXYZ",
                    "source_access_key_secret": "source-secret",
                    "role_arn": "acs:ram::123456789012:role/polardb",
                    "role_session_name": "polardb-agentic",
                }
            },
        )

    assert error.value.code == "INVALID_CREDENTIAL_TRANSITION"


async def test_block_only_patch_cannot_mutate_an_inactive_retained_block(
    context,
) -> None:
    current = await active_assume_with_retained_direct(context)

    with pytest.raises(ConfigError) as error:
        await save_draft(
            context,
            current.module["revision"],
            {
                "direct_ak": {
                    "access_key_id": "TEST1111111111WXYZ",
                    "access_key_secret": "replacement-secret",
                }
            },
        )

    assert error.value.code == "INVALID_CREDENTIAL_TRANSITION"


async def test_block_only_patch_updates_the_selected_block(context) -> None:
    direct = await active_direct_config(context)

    await save_draft(
        context,
        direct.module["revision"],
        {"direct_ak": {"access_key_secret": "replacement-secret"}},
    )

    internal = await context.service.describe_internal("aliyun_access")
    stored = SecretEnvelope.model_validate(
        internal.draft["direct_ak"]["access_key_secret"]["$secret"]
    )
    assert context.crypto.decrypt_field(
        stored,
        module="aliyun_access",
        field_path="direct_ak.access_key_secret",
        schema_version=2,
    ) == "replacement-secret"


async def test_partial_selected_block_update_allows_explicit_current_mode(
    context,
) -> None:
    direct = await active_direct_config(context)

    await save_draft(
        context,
        direct.module["revision"],
        {
            "credential_mode": "direct_ak",
            "direct_ak": {"access_key_secret": "replacement-secret"},
        },
    )

    internal = await context.service.describe_internal("aliyun_access")
    stored = SecretEnvelope.model_validate(
        internal.draft["direct_ak"]["access_key_secret"]["$secret"]
    )
    assert context.crypto.decrypt_field(
        stored,
        module="aliyun_access",
        field_path="direct_ak.access_key_secret",
        schema_version=2,
    ) == "replacement-secret"


async def test_partial_assume_role_update_preserves_omitted_fields_and_secrets(
    context,
) -> None:
    direct = await active_direct_config(context)
    assumed = await save_draft(
        context,
        direct.module["revision"],
        assume_input(
            assume_role={
                "source_access_key_id": "TEST0987654321WXYZ",
                "source_access_key_secret": "source-secret",
                "role_arn": "acs:ram::123456789012:role/polardb",
                "external_id": "external-identity",
            }
        ),
    )
    before = await context.service.describe_internal("aliyun_access")
    before_assume = before.draft["assume_role"]

    await save_draft(
        context,
        assumed.module["revision"],
        {"assume_role": {"duration_seconds": 1800}},
    )

    internal = await context.service.describe_internal("aliyun_access")
    updated = internal.draft["assume_role"]
    assert updated["duration_seconds"] == 1800
    assert updated["role_arn"] == "acs:ram::123456789012:role/polardb"
    assert updated["role_session_name"] == before_assume["role_session_name"]
    assert updated["external_id"] == before_assume["external_id"]
    assert updated["source_access_key_id"] == before_assume["source_access_key_id"]
    assert updated["source_access_key_secret"] == before_assume[
        "source_access_key_secret"
    ]


async def test_partial_assume_role_external_id_clear_is_accepted(context) -> None:
    direct = await active_direct_config(context)
    assumed = await save_draft(
        context,
        direct.module["revision"],
        assume_input(
            assume_role={
                "source_access_key_id": "TEST0987654321WXYZ",
                "source_access_key_secret": "source-secret",
                "role_arn": "acs:ram::123456789012:role/polardb",
                "external_id": "external-identity",
            }
        ),
    )

    await save_draft(
        context,
        assumed.module["revision"],
        {"assume_role": {"external_id": {"$secret_action": "clear"}}},
    )

    internal = await context.service.describe_internal("aliyun_access")
    assert "external_id" not in internal.draft["assume_role"]


async def test_full_assume_role_replacement_omits_blank_external_id(context) -> None:
    direct = await active_direct_config(context)

    await save_draft(
        context,
        direct.module["revision"],
        assume_input(
            assume_role={
                "source_access_key_id": "TEST0987654321WXYZ",
                "source_access_key_secret": "source-secret",
                "role_arn": "acs:ram::123456789012:role/polardb",
            }
        ),
    )

    internal = await context.service.describe_internal("aliyun_access")
    assert "external_id" not in internal.draft["assume_role"]


async def test_partial_assume_role_rejects_external_id_marker_input(context) -> None:
    direct = await active_direct_config(context)
    assumed = await save_draft(context, direct.module["revision"], assume_input())

    with pytest.raises(ConfigError) as error:
        await save_draft(
            context,
            assumed.module["revision"],
            {"assume_role": {"external_id": {"configured": True}}},
        )

    assert error.value.code == "INVALID_SECRET_INPUT"


async def test_partial_ecs_ram_role_update_preserves_omitted_fields(
    context,
) -> None:
    direct = await active_direct_config(context)
    ecs = await save_draft(
        context,
        direct.module["revision"],
        {
            "credential_mode": "ecs_ram_role",
            "ecs_ram_role": {
                "role_name": "pas-runtime",
                "metadata_policy": "v2_only",
            },
        },
    )

    await save_draft(
        context,
        ecs.module["revision"],
        {"ecs_ram_role": {"role_name": "pas-updated"}},
    )

    internal = await context.service.describe_internal("aliyun_access")
    assert internal.draft["ecs_ram_role"] == {
        "role_name": "pas-updated",
        "metadata_policy": "v2_only",
    }


async def test_plan_does_not_emit_a_committed_transition_audit(
    context, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="server.configuration.transition")
    actor = ConfigActor(scope="admin:1", actor_type="admin")

    await context.service.execute(
        ConfigCommand(
            action=ConfigAction.PLAN,
            module="aliyun_access",
            config={
                "credential_mode": "direct_ak",
                "direct_ak": {
                    "access_key_id": "TEST1234567890ABCD",
                    "access_key_secret": "direct-secret",
                },
            },
        ),
        actor,
    )

    assert not [
        record
        for record in caplog.records
        if record.name == "server.configuration.transition"
    ]


async def test_stale_draft_save_does_not_emit_a_committed_transition_audit(
    context, caplog
) -> None:
    direct = await active_direct_config(context)
    caplog.set_level(logging.INFO, logger="server.configuration.transition")
    caplog.clear()

    with pytest.raises(ConfigError) as error:
        await save_draft(context, direct.module["revision"] - 1, assume_input())

    assert error.value.code == "REVISION_CONFLICT"
    assert not [
        record
        for record in caplog.records
        if record.name == "server.configuration.transition"
    ]


async def test_successful_draft_save_emits_one_committed_transition_audit(
    context, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="server.configuration.transition")

    await active_direct_config(context)

    records = [
        record
        for record in caplog.records
        if record.name == "server.configuration.transition"
    ]
    assert len(records) == 1
    assert records[0].credential_selected_mode == "direct_ak"


async def test_plan_audits_transition_attempt_without_persistence(
    context, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="server.configuration.audit")
    actor = ConfigActor(scope="admin:1", actor_type="admin")

    await context.service.execute(
        ConfigCommand(
            action=ConfigAction.PLAN,
            module="aliyun_access",
            config={
                "credential_mode": "direct_ak",
                "direct_ak": {
                    "access_key_id": "TEST1234567890ABCD",
                    "access_key_secret": "direct-secret",
                },
            },
        ),
        actor,
    )

    record = caplog.records[-1]
    assert record.aliyun_selected_mode == "direct_ak"
    assert record.aliyun_transition_committed is False
    assert record.config_source_revision == 0
    assert record.config_revision == 0
    assert not [
        item
        for item in caplog.records
        if item.name == "server.configuration.transition"
    ]


async def test_stale_save_audits_attempted_and_observed_revisions(
    context, caplog
) -> None:
    direct = await active_direct_config(context)
    caplog.set_level(logging.INFO, logger="server.configuration.audit")
    caplog.clear()

    with pytest.raises(ConfigError):
        await save_draft(
            context,
            direct.module["revision"] - 1,
            assume_input(),
        )

    record = caplog.records[-1]
    assert record.config_result == "error"
    assert record.config_error_code == "REVISION_CONFLICT"
    assert record.config_source_revision == direct.module["revision"]
    assert record.config_attempted_revision == direct.module["revision"] - 1
    assert record.config_observed_revision == direct.module["revision"]
    assert record.aliyun_transition_committed is False


async def test_reuse_and_partial_update_audits_stored_identity_mask(
    context, caplog
) -> None:
    direct = await active_direct_config(context)
    caplog.set_level(logging.INFO, logger="server.configuration.audit")
    caplog.clear()

    partial = await save_draft(
        context,
        direct.module["revision"],
        {"direct_ak": {"access_key_secret": "updated-secret"}},
    )
    partial_record = caplog.records[-1]
    assert partial_record.aliyun_credential_id_mask == "TEST****ABCD"

    await save_draft(
        context,
        partial.module["revision"],
        {"credential_mode": "direct_ak"},
    )
    reuse_record = caplog.records[-1]
    assert reuse_record.aliyun_selected_mode_action == "reuse_retained"
    assert reuse_record.aliyun_credential_id_mask == "TEST****ABCD"


async def test_retained_delete_modes_are_in_transition_audit(context, caplog) -> None:
    current = await active_assume_with_retained_direct(context)
    caplog.set_level(logging.INFO, logger="server.configuration.audit")
    caplog.clear()

    await save_draft(
        context,
        current.module["revision"],
        {
            "credential_mode": "assume_role",
            "transition": {"delete_retained_modes": ["direct_ak"]},
        },
    )

    record = caplog.records[-1]
    assert record.aliyun_delete_retained_modes == ("direct_ak",)


async def test_redacted_public_secret_placeholder_is_rejected_as_secret_input(
    context,
) -> None:
    direct = await active_direct_config(context)
    public_key_id = direct.module["draft"]["direct_ak"]["access_key_id"]

    with pytest.raises(ConfigError) as error:
        await save_draft(
            context,
            direct.module["revision"],
            assume_input(
                assume_role={
                    "source_access_key_id": public_key_id,
                    "source_access_key_secret": "source-secret",
                    "role_arn": "acs:ram::123456789012:role/polardb",
                    "role_session_name": "polardb-agentic",
                }
            ),
        )

    assert error.value.code == "INVALID_SECRET_INPUT"
