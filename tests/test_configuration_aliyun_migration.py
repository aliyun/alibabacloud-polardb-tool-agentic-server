from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.configuration.module_migrations import migrate_document_for_write
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ConfigError,
    ModuleState,
    ValidationOperation,
    ValidationProof,
)
from server.core.config_crypto import SecretEnvelope
from tests._configuration_helpers import (
    corrupt_legacy_ciphertext,
    create_config_context,
    save_region_change,
    seed_v1_assume_document,
    seed_v1_direct_document,
)


@pytest.fixture
async def context():
    value = await create_config_context()
    yield value
    await value.close()


async def test_v1_direct_is_read_without_database_write(context) -> None:
    await seed_v1_direct_document(context)
    before = await context.repository.global_version()

    described = await context.service.describe_internal("aliyun_access")

    assert described.schema_version == 1
    assert await context.repository.global_version() == before


async def test_first_write_migrates_draft_and_effective_once(context) -> None:
    await seed_v1_assume_document(context)
    before = await context.repository.global_version()
    legacy_row = await context.repository.get_config_row("module.aliyun_access")
    assert legacy_row is not None
    migrated_at = legacy_row.updated_at or legacy_row.created_at
    if migrated_at.tzinfo is None:
        migrated_at = migrated_at.replace(tzinfo=timezone.utc)

    result = await save_region_change(context)
    internal = await context.service.describe_internal("aliyun_access")

    assert internal.schema_version == 2
    assert internal.draft is not None
    assert internal.effective is not None
    assert "assume_role" in internal.draft
    assert "assume_role" in internal.effective.config
    assert internal.last_validation is None
    assert result.config_version == before + 1
    source_secret = internal.draft["assume_role"]["source_access_key_secret"]
    assert context.crypto.decrypt_field(
        SecretEnvelope.model_validate(source_secret["$secret"]),
        module="aliyun_access",
        field_path="assume_role.source_access_key_secret",
        schema_version=2,
    ) == "legacy-source-secret"
    assert source_secret["updated_at"] == migrated_at.isoformat()


async def test_migration_drops_unused_direct_role_fields(context) -> None:
    document = await seed_v1_direct_document(context)
    row = await context.repository.get_config_row("module.aliyun_access")
    assert row is not None

    migrated = migrate_document_for_write(
        "aliyun_access",
        document,
        crypto=context.crypto,
        legacy_updated_at=(row.updated_at or row.created_at).replace(
            tzinfo=timezone.utc
        ),
    )

    assert migrated.draft == {
        "credential_mode": "direct_ak",
        "region_id": "cn-hangzhou",
        "openapi_network": "public",
        "direct_ak": migrated.draft["direct_ak"],
    }
    assert "role_arn" not in migrated.draft


async def test_failed_legacy_decrypt_leaves_row_unchanged(context) -> None:
    await seed_v1_direct_document(context)
    await corrupt_legacy_ciphertext(context)
    row = await context.repository.get_config_row("module.aliyun_access")
    assert row is not None
    before = row.config_value
    version = await context.repository.global_version()

    with pytest.raises(ConfigError, match="migration"):
        await save_region_change(context)

    after = await context.repository.get_config_row("module.aliyun_access")
    assert after is not None
    assert after.config_value == before
    assert await context.repository.global_version() == version


@pytest.mark.parametrize("state", (ModuleState.VALIDATING, ModuleState.VALIDATED))
async def test_expired_v1_describe_never_writes(context, state) -> None:
    document = await seed_v1_direct_document(context)
    now = datetime.now(timezone.utc)
    if state == ModuleState.VALIDATING:
        document = document.model_copy(
            update={
                "workflow_state": state,
                "validation_operation": ValidationOperation(
                    operation_id="expired-validation",
                    started_at=now - timedelta(minutes=3),
                    lease_expires_at=now - timedelta(minutes=1),
                ),
            }
        )
    else:
        document = document.model_copy(
            update={
                "workflow_state": state,
                "last_validation": ValidationProof(
                    status="PASSED",
                    checked_at=now - timedelta(minutes=12),
                    expires_at=now - timedelta(minutes=1),
                    validation_id_hash="expired",
                    validated_revision=document.revision,
                    config_digest="expired",
                    dependency_revisions={},
                ),
            }
        )
    await context.repository.compare_and_set_module(
        "aliyun_access",
        expected_revision=document.revision,
        document=document,
    )
    before = await context.repository.global_version()

    described = await context.service.execute(
        ConfigCommand(action=ConfigAction.DESCRIBE, module="aliyun_access"),
        ConfigActor(scope="admin:1", actor_type="admin"),
    )

    assert described.module["schema_version"] == 1
    assert await context.repository.global_version() == before


@pytest.mark.parametrize("state", (ModuleState.VALIDATING, ModuleState.VALIDATED))
async def test_first_write_recovers_expired_v1_in_one_cas(context, state) -> None:
    document = await seed_v1_direct_document(context)
    now = datetime.now(timezone.utc)
    updates: dict[str, object] = {"workflow_state": state}
    if state == ModuleState.VALIDATING:
        updates["validation_operation"] = ValidationOperation(
            operation_id="expired-validation",
            started_at=now - timedelta(minutes=3),
            lease_expires_at=now - timedelta(minutes=1),
        )
    else:
        updates["last_validation"] = ValidationProof(
            status="PASSED",
            checked_at=now - timedelta(minutes=12),
            expires_at=now - timedelta(minutes=1),
            validation_id_hash="expired",
            validated_revision=document.revision,
            config_digest="expired",
            dependency_revisions={},
        )
    document = document.model_copy(update=updates)
    await context.repository.compare_and_set_module(
        "aliyun_access",
        expected_revision=document.revision,
        document=document,
    )
    before = await context.repository.global_version()

    result = await save_region_change(context)
    stored = await context.service.describe_internal("aliyun_access")

    assert result.config_version == before + 1
    assert stored.schema_version == 2
    assert stored.workflow_state == ModuleState.DRAFT
    assert stored.last_validation is None
    assert stored.validation_operation is None


@pytest.mark.parametrize("schema_version", (0, 3))
async def test_unsupported_schema_is_rejected_for_read_and_write(
    context, schema_version
) -> None:
    document = await seed_v1_direct_document(context)
    document = document.model_copy(update={"schema_version": schema_version})
    await context.repository.compare_and_set_module(
        "aliyun_access",
        expected_revision=document.revision,
        document=document,
    )
    row = await context.repository.get_config_row("module.aliyun_access")
    assert row is not None
    before = row.config_value

    with pytest.raises(ConfigError, match="schema version"):
        await context.service.describe_internal("aliyun_access")
    with pytest.raises(ConfigError, match="schema version"):
        await context.service.execute(
            ConfigCommand(action=ConfigAction.DESCRIBE),
            ConfigActor(scope="admin:1", actor_type="admin"),
        )
    with pytest.raises(ConfigError, match="schema version"):
        await save_region_change(context)

    after = await context.repository.get_config_row("module.aliyun_access")
    assert after is not None
    assert after.config_value == before


async def test_legacy_assume_without_session_name_reads_and_migrates(context) -> None:
    document = await seed_v1_assume_document(
        context, include_role_session_name=False
    )
    row = await context.repository.get_config_row("module.aliyun_access")
    assert row is not None

    projected = context.service._decrypt_config(
        "aliyun_access", document, document.effective.config
    )
    migrated = migrate_document_for_write(
        "aliyun_access",
        document,
        crypto=context.crypto,
        legacy_updated_at=row.updated_at or row.created_at,
    )
    await save_region_change(context)
    stored = await context.service.describe_internal("aliyun_access")

    assert projected["assume_role"]["role_session_name"] == "polardb-agentic"
    assert migrated.draft["assume_role"]["role_session_name"] == "polardb-agentic"
    assert stored.draft["assume_role"]["role_session_name"] == "polardb-agentic"


async def test_migration_is_pure_idempotent_and_preserves_workflow(context) -> None:
    document = await seed_v1_direct_document(context)
    document = document.model_copy(
        update={
            "workflow_state": ModuleState.ERROR,
            "desired_state": ModuleState.ACTIVE,
            "last_error_code": "OLD_ERROR",
        }
    )
    row = await context.repository.get_config_row("module.aliyun_access")
    assert row is not None

    migrated = migrate_document_for_write(
        "aliyun_access",
        document,
        crypto=context.crypto,
        legacy_updated_at=row.updated_at or row.created_at,
    )
    repeated = migrate_document_for_write(
        "aliyun_access",
        migrated,
        crypto=context.crypto,
        legacy_updated_at=row.updated_at or row.created_at,
    )

    assert migrated.workflow_state == ModuleState.ERROR
    assert migrated.desired_state == ModuleState.ACTIVE
    assert migrated.last_error_code == "OLD_ERROR"
    assert migrated.effective == document.effective.model_copy(
        update={"config": migrated.effective.config}
    )
    assert repeated == migrated


async def test_stale_first_write_leaves_v1_row_unchanged(context) -> None:
    document = await seed_v1_direct_document(context)
    row = await context.repository.get_config_row("module.aliyun_access")
    assert row is not None
    before = row.config_value

    with pytest.raises(ConfigError) as exc:
        await context.service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="aliyun_access",
                expected_revision=document.revision - 1,
                config={"region_id": "cn-shanghai"},
            ),
            ConfigActor(scope="admin:1", actor_type="admin"),
        )

    assert exc.value.code == "REVISION_CONFLICT"
    after = await context.repository.get_config_row("module.aliyun_access")
    assert after is not None
    assert after.config_value == before
