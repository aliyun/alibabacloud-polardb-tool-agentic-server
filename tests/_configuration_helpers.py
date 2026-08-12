from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from server.configuration.bootstrap import initialize_configuration
from server.configuration.repository import ConfigRepository
from server.configuration.service import ConfigService
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    EffectiveConfig,
    ModuleDocument,
    ModuleState,
)
from server.core.config_crypto import ConfigCrypto
from server.models import Base, SystemConfig

ROOT_KEY = b"01234567890123456789012345678901"


@dataclass(slots=True)
class ConfigTestContext:
    engine: AsyncEngine
    repository: ConfigRepository
    crypto: ConfigCrypto
    service: ConfigService

    async def close(self) -> None:
        await self.engine.dispose()


async def create_config_context() -> ConfigTestContext:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ConfigRepository(factory)
    crypto = ConfigCrypto(ROOT_KEY)
    await initialize_configuration(repository, crypto)
    return ConfigTestContext(
        engine=engine,
        repository=repository,
        crypto=crypto,
        service=ConfigService(repository, crypto),
    )


async def save_aliyun_draft(
    context: ConfigTestContext,
    config: dict[str, object],
    *,
    expected_revision: int = 0,
):
    return await context.service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="aliyun_access",
            expected_revision=expected_revision,
            config=config,
        ),
        ConfigActor(scope="admin:1", actor_type="admin"),
    )


def assume_input(
    *,
    transition: dict[str, object] | None = None,
    assume_role: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "credential_mode": "assume_role",
        "assume_role": assume_role
        or {
            "source_access_key_id": "TEST0987654321WXYZ",
            "source_access_key_secret": "source-secret",
            "role_arn": "acs:ram::123456789012:role/polardb",
            "role_session_name": "polardb-agentic",
        },
    }
    if transition is not None:
        value["transition"] = transition
    return value


async def save_draft(
    context: ConfigTestContext,
    expected_revision: int,
    config: dict[str, object],
):
    return await save_aliyun_draft(
        context,
        config,
        expected_revision=expected_revision,
    )


async def active_direct_config(context: ConfigTestContext):
    return await save_aliyun_draft(
        context,
        {
            "credential_mode": "direct_ak",
            "direct_ak": {
                "access_key_id": "TEST1234567890ABCD",
                "access_key_secret": "direct-secret",
            },
        },
    )


async def active_assume_with_retained_direct(context: ConfigTestContext):
    direct = await active_direct_config(context)
    return await save_draft(
        context,
        direct.module["revision"],
        assume_input(
            transition={
                "previous_mode_action": "retain",
                "selected_mode_action": "replace",
            }
        ),
    )


async def switch_to_assume_with_explicit_source_reuse(
    context: ConfigTestContext,
    expected_revision: int,
):
    return await save_draft(
        context,
        expected_revision,
        assume_input(
            transition={
                "previous_mode_action": "retain",
                "reuse_direct_ak_as_assume_source": True,
            },
            assume_role={
                "role_arn": "acs:ram::123456789012:role/polardb",
                "role_session_name": "polardb-agentic",
            },
        ),
    )


def _legacy_secret(
    context: ConfigTestContext, value: str, field_path: str
) -> dict[str, object]:
    return {
        "$secret": context.crypto.encrypt_field(
            value,
            module="aliyun_access",
            field_path=field_path,
            schema_version=1,
        ).model_dump(mode="json")
    }


async def _seed_v1_document(
    context: ConfigTestContext, config: dict[str, object]
) -> ModuleDocument:
    document = ModuleDocument(
        schema_version=1,
        workflow_state=ModuleState.ACTIVE,
        initial_state=ModuleState.SKIPPED,
        desired_state=ModuleState.ACTIVE,
        draft=config,
        effective=EffectiveConfig(
            revision=1,
            state=ModuleState.ACTIVE,
            config=config,
        ),
    )
    current = await context.repository.get_module("aliyun_access")
    assert current is not None
    await context.repository.compare_and_set_module(
        "aliyun_access",
        expected_revision=current.revision,
        document=document,
    )
    stored = await context.repository.get_module("aliyun_access")
    assert stored is not None
    return stored


async def seed_v1_direct_document(
    context: ConfigTestContext,
) -> ModuleDocument:
    return await _seed_v1_document(
        context,
        {
            "credential_mode": "direct_ak",
            "access_key_id": _legacy_secret(
                context, "TEST1234567890ABCD", "access_key_id"
            ),
            "access_key_secret": _legacy_secret(
                context, "legacy-direct-secret", "access_key_secret"
            ),
            "role_arn": "acs:ram::123456789012:role/unused",
            "role_session_name": "unused-session",
            "sts_duration_seconds": 3600,
            "region_id": "cn-hangzhou",
            "openapi_network": "public",
        },
    )


async def seed_v1_assume_document(
    context: ConfigTestContext,
    *,
    include_role_session_name: bool = True,
) -> ModuleDocument:
    config: dict[str, object] = {
        "credential_mode": "assume_role",
        "access_key_id": _legacy_secret(
            context, "TEST0987654321WXYZ", "access_key_id"
        ),
        "access_key_secret": _legacy_secret(
            context, "legacy-source-secret", "access_key_secret"
        ),
        "role_arn": "acs:ram::123456789012:role/polardb",
        "sts_duration_seconds": 1800,
        "region_id": "cn-beijing",
        "openapi_network": "vpc",
    }
    if include_role_session_name:
        config["role_session_name"] = "polardb-agentic"
    return await _seed_v1_document(
        context,
        config,
    )


async def corrupt_legacy_ciphertext(context: ConfigTestContext) -> None:
    async with context.repository.session_factory() as session:
        async with session.begin():
            row = await session.get(SystemConfig, "module.aliyun_access")
            assert row is not None
            value = json.loads(row.config_value)
            for config in (
                value.get("draft"),
                (value.get("effective") or {}).get("config"),
            ):
                if isinstance(config, dict):
                    secret = config.get("access_key_secret")
                    if isinstance(secret, dict) and isinstance(
                        secret.get("$secret"), dict
                    ):
                        secret["$secret"]["ciphertext"] = "corrupt"
            row.config_value = json.dumps(value, sort_keys=True, separators=(",", ":"))


async def save_region_change(context: ConfigTestContext):
    current = await context.repository.get_module("aliyun_access")
    assert current is not None
    return await context.service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="aliyun_access",
            expected_revision=current.revision,
            config={"region_id": "cn-shanghai"},
        ),
        ConfigActor(scope="admin:1", actor_type="admin"),
    )


async def seed_active_assume_with_retained_direct(
    context: ConfigTestContext,
):
    direct = await active_direct_config(context)
    validated_direct = await context.service.execute(
        ConfigCommand(
            action=ConfigAction.VALIDATE,
            module="aliyun_access",
            expected_revision=direct.module["revision"],
        ),
        ConfigActor(scope="admin:1", actor_type="admin"),
    )
    activated_direct = await context.service.execute(
        ConfigCommand(
            action=ConfigAction.ACTIVATE,
            module="aliyun_access",
            expected_revision=validated_direct.module["revision"],
            validation_id=validated_direct.validation["validation_id"],
            idempotency_key="activate-direct-for-retention",
        ),
        ConfigActor(scope="admin:1", actor_type="admin"),
    )
    assume = await save_draft(
        context,
        activated_direct.module["revision"],
        assume_input(
            transition={
                "previous_mode_action": "retain",
                "selected_mode_action": "replace",
            }
        ),
    )
    validated_assume = await context.service.execute(
        ConfigCommand(
            action=ConfigAction.VALIDATE,
            module="aliyun_access",
            expected_revision=assume.module["revision"],
        ),
        ConfigActor(scope="admin:1", actor_type="admin"),
    )
    return await context.service.execute(
        ConfigCommand(
            action=ConfigAction.ACTIVATE,
            module="aliyun_access",
            expected_revision=validated_assume.module["revision"],
            validation_id=validated_assume.validation["validation_id"],
            idempotency_key="activate-assume-with-retained-direct",
        ),
        ConfigActor(scope="admin:1", actor_type="admin"),
    )
