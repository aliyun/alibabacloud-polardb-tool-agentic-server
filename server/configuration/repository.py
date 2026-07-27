from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from server.configuration.types import ModuleDocument
from server.models import (
    AuthProvider,
    ConfigBootstrapClaim,
    ConfigOperationReceipt,
    SystemConfig,
    User,
    UserRole,
    UserStatus,
)
from server.models.system_config import MAX_CONFIG_DOCUMENT_BYTES


class ConfigConflict(ValueError):
    """Raised when an optimistic configuration revision is stale."""


class ConfigRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.session_factory = session_factory

    async def ensure_setup_status(self) -> SystemConfig:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(
                    SystemConfig, "setup.status", with_for_update=True
                )
                if row is None:
                    row = SystemConfig(
                        config_key="setup.status",
                        config_value=json.dumps(
                            {
                                "schema_version": 1,
                                "system_state": "SETUP",
                                "initialized_at": datetime.now(
                                    timezone.utc
                                ).isoformat(),
                                "ready_at": None,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        config_version=1,
                    )
                    session.add(row)
            return row

    async def global_version(self) -> int:
        async with self.session_factory() as session:
            row = await session.get(SystemConfig, "setup.status")
            return row.config_version if row is not None else 0

    async def get_module(
        self, module: str
    ) -> ModuleDocument | None:
        async with self.session_factory() as session:
            row = await session.get(SystemConfig, f"module.{module}")
            if row is None:
                return None
            return ModuleDocument.model_validate_json(row.config_value)

    async def get_config_row(self, key: str) -> SystemConfig | None:
        async with self.session_factory() as session:
            return await session.get(SystemConfig, key)

    async def list_modules(self) -> dict[str, ModuleDocument]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(SystemConfig).where(
                        SystemConfig.config_key.like("module.%")
                    )
                )
            ).scalars()
            return {
                row.config_key.removeprefix("module."):
                ModuleDocument.model_validate_json(row.config_value)
                for row in rows
            }

    @staticmethod
    def _serialize(document: ModuleDocument) -> str:
        value = document.model_dump_json(
            exclude_none=True,
            by_alias=True,
        )
        if len(value.encode("utf-8")) > MAX_CONFIG_DOCUMENT_BYTES:
            raise ValueError(
                "Configuration document exceeds the 1 MiB limit"
            )
        return value

    async def compare_and_set_module(
        self,
        module: str,
        *,
        expected_revision: int,
        document: ModuleDocument,
    ) -> SystemConfig:
        key = f"module.{module}"
        async with self.session_factory() as session:
            async with session.begin():
                setup = await session.get(
                    SystemConfig, "setup.status", with_for_update=True
                )
                if setup is None:
                    raise RuntimeError(
                        "setup.status must exist before configuration writes"
                    )
                row = await session.get(
                    SystemConfig, key, with_for_update=True
                )
                current_revision = (
                    ModuleDocument.model_validate_json(
                        row.config_value
                    ).revision
                    if row is not None
                    else 0
                )
                if current_revision != expected_revision:
                    raise ConfigConflict(
                        f"expected revision {expected_revision}, "
                        f"current revision is {current_revision}"
                    )
                next_revision = current_revision + 1
                updated_document = document.model_copy(
                    update={"revision": next_revision}
                )
                serialized = self._serialize(updated_document)
                next_version = setup.config_version + 1
                setup.config_version = next_version
                if row is None:
                    row = SystemConfig(
                        config_key=key,
                        config_value=serialized,
                        config_version=next_version,
                    )
                    session.add(row)
                else:
                    row.config_value = serialized
                    row.config_version = next_version
            return row

    async def activate_core_admin(
        self,
        *,
        expected_revision: int,
        document: ModuleDocument,
        username: str,
        password_hash: str,
        bootstrap_token_hash: str,
    ) -> SystemConfig:
        """Create the break-glass admin and activate its module atomically."""
        async with self.session_factory() as session:
            async with session.begin():
                setup = await session.get(
                    SystemConfig, "setup.status", with_for_update=True
                )
                row = await session.get(
                    SystemConfig,
                    "module.core_admin",
                    with_for_update=True,
                )
                claim = await session.get(
                    ConfigBootstrapClaim,
                    "bootstrap",
                    with_for_update=True,
                )
                if setup is None or row is None:
                    raise RuntimeError(
                        "configuration defaults are not initialized"
                    )
                if (
                    claim is None
                    or claim.consumed_at is not None
                    or claim.token_hash != bootstrap_token_hash
                ):
                    raise ConfigConflict(
                        "bootstrap claim is unavailable"
                    )
                current = ModuleDocument.model_validate_json(
                    row.config_value
                )
                if current.revision != expected_revision:
                    raise ConfigConflict(
                        f"expected revision {expected_revision}, "
                        f"current revision is {current.revision}"
                    )
                existing = (
                    await session.execute(
                        select(User)
                        .where(
                            User.external_id == username,
                            User.auth_provider
                            == AuthProvider.BUILTIN,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    raise ConfigConflict(
                        "administrator username already exists"
                    )
                session.add(
                    User(
                        external_id=username,
                        display_name="Administrator",
                        auth_provider=AuthProvider.BUILTIN,
                        password_hash=password_hash,
                        role=UserRole.ADMIN,
                        status=UserStatus.ACTIVE,
                    )
                )
                next_version = setup.config_version + 1
                next_document = document.model_copy(
                    update={"revision": current.revision + 1}
                )
                row.config_value = self._serialize(next_document)
                row.config_version = next_version
                status = json.loads(setup.config_value)
                status["system_state"] = "READY"
                status["ready_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                setup.config_value = json.dumps(
                    status, sort_keys=True, separators=(",", ":")
                )
                setup.config_version = next_version
                claim.consumed_at = datetime.now(timezone.utc)
                claim.row_version += 1
            return row

    async def activate_with_session_epoch(
        self,
        module: str,
        *,
        expected_revision: int,
        document: ModuleDocument,
    ) -> SystemConfig:
        """Activate SSO and invalidate human sessions in one transaction."""
        key = f"module.{module}"
        async with self.session_factory() as session:
            async with session.begin():
                setup = await session.get(
                    SystemConfig, "setup.status", with_for_update=True
                )
                row = await session.get(
                    SystemConfig, key, with_for_update=True
                )
                token_row = await session.get(
                    SystemConfig,
                    "module.token_security",
                    with_for_update=True,
                )
                if setup is None or row is None or token_row is None:
                    raise RuntimeError(
                        "configuration defaults are not initialized"
                    )
                current = ModuleDocument.model_validate_json(
                    row.config_value
                )
                if current.revision != expected_revision:
                    raise ConfigConflict(
                        f"expected revision {expected_revision}, "
                        f"current revision is {current.revision}"
                    )
                token_document = ModuleDocument.model_validate_json(
                    token_row.config_value
                )
                if token_document.effective is None:
                    raise RuntimeError(
                        "token_security is not active"
                    )
                token_config = dict(
                    token_document.effective.config
                )
                token_config["session_epoch"] = (
                    int(token_config.get("session_epoch", 1)) + 1
                )
                token_effective = (
                    token_document.effective.model_copy(
                        update={
                            "revision":
                            token_document.effective.revision + 1,
                            "config": token_config,
                        }
                    )
                )
                token_document = token_document.model_copy(
                    update={
                        "revision": token_document.revision + 1,
                        "effective": token_effective,
                    }
                )
                next_version = setup.config_version + 1
                setup.config_version = next_version
                row.config_value = self._serialize(
                    document.model_copy(
                        update={"revision": current.revision + 1}
                    )
                )
                row.config_version = next_version
                token_row.config_value = self._serialize(
                    token_document
                )
                token_row.config_version = next_version
            return row

    async def initialize_rows(
        self,
        documents: Mapping[str, ModuleDocument],
        *,
        setup_value: str,
        token_hash: str,
        token_expires_at: datetime,
    ) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                setup_values = {
                    "config_key": "setup.status",
                    "config_value": setup_value,
                    "config_version": 1,
                }
                dialect = session.get_bind().dialect.name
                if dialect == "postgresql":
                    statement = postgresql_insert(SystemConfig).values(
                        **setup_values
                    ).on_conflict_do_nothing(
                        index_elements=["config_key"]
                    )
                elif dialect == "sqlite":
                    statement = sqlite_insert(SystemConfig).values(
                        **setup_values
                    ).on_conflict_do_nothing(
                        index_elements=["config_key"]
                    )
                elif dialect == "mysql":
                    statement = mysql_insert(SystemConfig).values(
                        **setup_values
                    ).prefix_with("IGNORE")
                else:
                    raise RuntimeError(
                        f"Unsupported database dialect: {dialect}"
                    )
                winner = await session.execute(statement)
                if winner.rowcount != 1:
                    return False

                setup = await session.get(
                    SystemConfig, "setup.status"
                )
                if setup is None:
                    raise RuntimeError("setup status insert was lost")
                for module, document in documents.items():
                    session.add(
                        SystemConfig(
                            config_key=f"module.{module}",
                            config_value=self._serialize(document),
                            config_version=1,
                        )
                    )
                session.add(
                    ConfigBootstrapClaim(
                        singleton_key="bootstrap",
                        token_hash=token_hash,
                        expires_at=token_expires_at,
                    )
                )
                return True

    async def get_bootstrap_claim(
        self, *, for_update: bool = False
    ) -> ConfigBootstrapClaim | None:
        async with self.session_factory() as session:
            if for_update:
                result = await session.execute(
                    select(ConfigBootstrapClaim)
                    .where(
                        ConfigBootstrapClaim.singleton_key
                        == "bootstrap"
                    )
                    .with_for_update()
                )
                return result.scalar_one_or_none()
            return await session.get(
                ConfigBootstrapClaim, "bootstrap"
            )

    async def record_bootstrap_failure(self) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                claim = await session.get(
                    ConfigBootstrapClaim,
                    "bootstrap",
                    with_for_update=True,
                )
                if claim is not None:
                    claim.failed_attempts += 1
                    claim.row_version += 1

    async def consume_bootstrap_claim(self) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                claim = await session.get(
                    ConfigBootstrapClaim,
                    "bootstrap",
                    with_for_update=True,
                )
                if claim is None or claim.consumed_at is not None:
                    raise ConfigConflict(
                        "bootstrap claim is unavailable"
                    )
                claim.consumed_at = datetime.now(timezone.utc)
                claim.row_version += 1

    async def replace_bootstrap_claim(
        self,
        *,
        token_hash: str,
        expires_at: datetime,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                claim = await session.get(
                    ConfigBootstrapClaim,
                    "bootstrap",
                    with_for_update=True,
                )
                if claim is None:
                    claim = ConfigBootstrapClaim(
                        singleton_key="bootstrap",
                        token_hash=token_hash,
                        expires_at=expires_at,
                    )
                    session.add(claim)
                else:
                    claim.token_hash = token_hash
                    claim.expires_at = expires_at
                    claim.failed_attempts = 0
                    claim.consumed_at = None
                    claim.row_version += 1

    async def delete_expired_receipts(
        self, *, now: datetime, limit: int = 500
    ) -> int:
        async with self.session_factory() as session:
            ids = (
                await session.execute(
                    select(ConfigOperationReceipt.id)
                    .where(ConfigOperationReceipt.expires_at <= now)
                    .limit(limit)
                )
            ).scalars().all()
            if not ids:
                return 0
            await session.execute(
                delete(ConfigOperationReceipt).where(
                    ConfigOperationReceipt.id.in_(ids)
                )
            )
            await session.commit()
            return len(ids)

    async def get_receipt(
        self,
        *,
        actor_scope: str,
        idempotency_key_hash: str,
    ) -> ConfigOperationReceipt | None:
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(ConfigOperationReceipt).where(
                        ConfigOperationReceipt.actor_scope
                        == actor_scope,
                        ConfigOperationReceipt.idempotency_key_hash
                        == idempotency_key_hash,
                    )
                )
            ).scalar_one_or_none()

    async def store_receipt(
        self,
        *,
        actor_scope: str,
        idempotency_key_hash: str,
        action: str,
        module: str | None,
        request_digest: str,
        response_json: str,
        expires_at: datetime,
    ) -> ConfigOperationReceipt:
        receipt = ConfigOperationReceipt(
            actor_scope=actor_scope,
            idempotency_key_hash=idempotency_key_hash,
            action=action,
            module=module,
            request_digest=request_digest,
            status="SUCCEEDED",
            response_json=response_json,
            expires_at=expires_at,
        )
        async with self.session_factory() as session:
            session.add(receipt)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = (
                    await session.execute(
                        select(ConfigOperationReceipt).where(
                            ConfigOperationReceipt.actor_scope
                            == actor_scope,
                            ConfigOperationReceipt.idempotency_key_hash
                            == idempotency_key_hash,
                        )
                    )
                ).scalar_one()
                return existing
        return receipt
