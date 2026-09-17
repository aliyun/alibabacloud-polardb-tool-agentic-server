from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from server.auth.credential_mutation import (
    CredentialMutationMode,
    mutate_builtin_password_in_session,
)
from server.configuration.types import ModuleDocument
from server.models import (
    AuthProvider,
    ConfigBootstrapClaim,
    ConfigOperationReceipt,
    ConfigReceiptStatus,
    ManagedInstanceBinding,
    OIDCLoginState,
    SystemConfig,
    User,
    UserExternalIdentity,
    UserRole,
    UserStatus,
)
from server.models.base import utc_now
from server.models.system_config import MAX_CONFIG_DOCUMENT_BYTES


class ConfigConflict(ValueError):
    """Raised when an optimistic configuration revision is stale."""


class SSOTestConflict(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ModuleDocumentSnapshot:
    document: ModuleDocument
    created_at: datetime
    updated_at: datetime | None


class ReceiptConflict(ValueError):
    """Raised when a receipt key is reused for another request."""


class ReceiptInProgress(ValueError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("managed operation is already in progress")
        self.retry_after_seconds = retry_after_seconds


class ReceiptLeaseLost(ValueError):
    """Raised when a worker no longer owns an operation lease."""


class ManagedIdentityConflict(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


ManagedMutation = Callable[[AsyncSession], Awaitable[dict[str, Any]]]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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

    async def get_managed_identity_binding(
        self,
    ) -> ManagedInstanceBinding | None:
        async with self.session_factory() as session:
            rows = (
                (await session.execute(select(ManagedInstanceBinding).limit(2)))
                .scalars()
                .all()
            )
            if len(rows) > 1:
                raise RuntimeError(
                    "managed PAS database has multiple identity bindings"
                )
            return rows[0] if rows else None

    async def verify_managed_identity(
        self,
        *,
        instance_id: str,
        instance_generation: int,
    ) -> None:
        binding = await self.get_managed_identity_binding()
        if binding is None or binding.instance_id != instance_id:
            raise ManagedIdentityConflict("INSTANCE_IDENTITY_MISMATCH")
        if binding.instance_generation != instance_generation:
            raise ManagedIdentityConflict("INSTANCE_GENERATION_MISMATCH")

    async def get_admin_password_state(self) -> str | None:
        async with self.session_factory() as session:
            module_row = await session.get(SystemConfig, "module.core_admin")
            if module_row is None:
                return None
            document = ModuleDocument.model_validate_json(module_row.config_value)
            if document.effective is None:
                return None
            username = document.effective.config.get("username")
            if not isinstance(username, str) or not username:
                return None
            user = await session.scalar(
                select(User).where(
                    User.external_id == username,
                    User.auth_provider == AuthProvider.BUILTIN,
                )
            )
            return user.effective_password_state.value if user is not None else None

    async def bind_managed_identity(
        self,
        *,
        instance_id: str,
        generation: int,
    ) -> ManagedInstanceBinding:
        async with self.session_factory() as session:
            async with session.begin():
                setup = await session.get(
                    SystemConfig, "setup.status", with_for_update=True
                )
                if setup is None:
                    raise RuntimeError(
                        "setup.status must exist before identity binding"
                    )
                rows = (
                    (
                        await session.execute(
                            select(ManagedInstanceBinding).limit(2).with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(rows) > 1:
                    raise RuntimeError(
                        "managed PAS database has multiple identity bindings"
                    )
                if rows:
                    return rows[0]
                binding = ManagedInstanceBinding(
                    instance_id=instance_id,
                    instance_generation=generation,
                    bound_at=utc_now(),
                )
                session.add(binding)
                await session.flush()
                return binding

    async def get_module(self, module: str) -> ModuleDocument | None:
        async with self.session_factory() as session:
            row = await session.get(SystemConfig, f"module.{module}")
            if row is None:
                return None
            return ModuleDocument.model_validate_json(row.config_value)

    async def get_module_snapshot(self, module: str) -> ModuleDocumentSnapshot | None:
        """Read the document and row timestamp needed by lazy migrations."""
        async with self.session_factory() as session:
            row = await session.get(SystemConfig, f"module.{module}")
            if row is None:
                return None
            return ModuleDocumentSnapshot(
                document=ModuleDocument.model_validate_json(row.config_value),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    async def get_config_row(self, key: str) -> SystemConfig | None:
        async with self.session_factory() as session:
            return await session.get(SystemConfig, key)

    async def list_modules(self) -> dict[str, ModuleDocument]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(SystemConfig).where(SystemConfig.config_key.like("module.%"))
                )
            ).scalars()
            return {
                row.config_key.removeprefix(
                    "module."
                ): ModuleDocument.model_validate_json(row.config_value)
                for row in rows
            }

    @staticmethod
    def _serialize(document: ModuleDocument) -> str:
        value = document.model_dump_json(
            exclude_none=True,
            by_alias=True,
        )
        if len(value.encode("utf-8")) > MAX_CONFIG_DOCUMENT_BYTES:
            raise ValueError("Configuration document exceeds the 1 MiB limit")
        return value

    async def compare_and_set_module(
        self,
        module: str,
        *,
        expected_revision: int,
        document: ModuleDocument,
    ) -> SystemConfig:
        async with self.session_factory() as session:
            async with session.begin():
                row, _, _ = await self.compare_and_set_module_in_session(
                    session,
                    module,
                    expected_revision=expected_revision,
                    document=document,
                )
            return row

    async def compare_and_set_module_in_session(
        self,
        session: AsyncSession,
        module: str,
        *,
        expected_revision: int,
        document: ModuleDocument,
    ) -> tuple[SystemConfig, ModuleDocument, int]:
        key = f"module.{module}"
        setup = await session.get(SystemConfig, "setup.status", with_for_update=True)
        if setup is None:
            raise RuntimeError("setup.status must exist before configuration writes")
        row = await session.get(SystemConfig, key, with_for_update=True)
        current_revision = (
            ModuleDocument.model_validate_json(row.config_value).revision
            if row is not None
            else 0
        )
        if current_revision != expected_revision:
            raise ConfigConflict(
                f"expected revision {expected_revision}, current revision is {current_revision}"
            )
        next_revision = current_revision + 1
        updated_document = document.model_copy(update={"revision": next_revision})
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
        return row, updated_document, next_version

    async def ensure_module(
        self,
        module: str,
        document: ModuleDocument,
    ) -> bool:
        """Insert one missing module and advance the global version once."""
        key = f"module.{module}"
        async with self.session_factory() as session:
            async with session.begin():
                setup = await session.get(
                    SystemConfig,
                    "setup.status",
                    with_for_update=True,
                )
                if setup is None:
                    raise RuntimeError(
                        "setup.status must exist before module reconciliation"
                    )
                next_version = setup.config_version + 1
                values = {
                    "config_key": key,
                    "config_value": self._serialize(document),
                    "config_version": next_version,
                }
                dialect = session.get_bind().dialect.name
                statement: Any
                if dialect == "postgresql":
                    statement = (
                        postgresql_insert(SystemConfig)
                        .values(**values)
                        .on_conflict_do_nothing(index_elements=["config_key"])
                    )
                elif dialect == "sqlite":
                    statement = (
                        sqlite_insert(SystemConfig)
                        .values(**values)
                        .on_conflict_do_nothing(index_elements=["config_key"])
                    )
                elif dialect == "mysql":
                    statement = (
                        mysql_insert(SystemConfig)
                        .values(**values)
                        .prefix_with("IGNORE")
                    )
                else:
                    raise RuntimeError(f"Unsupported database dialect: {dialect}")
                inserted = await session.execute(statement)
                if cast(Any, inserted).rowcount != 1:
                    return False
                setup.config_version = next_version
                return True

    async def migrate_global_audit_configuration(
        self,
        *,
        expected_sql_revision: int,
        sql_document: ModuleDocument,
        expected_observability_revision: int,
        observability_document: ModuleDocument,
    ) -> bool:
        """Atomically move global audit settings between configuration modules."""
        keys = {
            "sql_security": "module.sql_security",
            "observability": "module.observability",
        }
        async with self.session_factory() as session:
            async with session.begin():
                setup = await session.get(
                    SystemConfig,
                    "setup.status",
                    with_for_update=True,
                )
                if setup is None:
                    raise RuntimeError(
                        "setup.status must exist before configuration migration"
                    )
                rows = {
                    name: await session.get(
                        SystemConfig,
                        key,
                        with_for_update=True,
                    )
                    for name, key in keys.items()
                }
                if any(row is None for row in rows.values()):
                    raise RuntimeError(
                        "audit configuration modules must exist before migration"
                    )
                current_sql = ModuleDocument.model_validate_json(
                    cast(SystemConfig, rows["sql_security"]).config_value
                )
                current_observability = ModuleDocument.model_validate_json(
                    cast(SystemConfig, rows["observability"]).config_value
                )
                if (
                    current_sql.schema_version == sql_document.schema_version
                    and current_observability.schema_version
                    == observability_document.schema_version
                ):
                    return False
                if (
                    current_sql.revision != expected_sql_revision
                    or current_observability.revision != expected_observability_revision
                ):
                    raise ConfigConflict(
                        "configuration changed during global audit migration"
                    )

                next_version = setup.config_version + 1
                sql_row = cast(SystemConfig, rows["sql_security"])
                observability_row = cast(
                    SystemConfig,
                    rows["observability"],
                )
                sql_row.config_value = self._serialize(sql_document)
                sql_row.config_version = next_version
                observability_row.config_value = self._serialize(observability_document)
                observability_row.config_version = next_version
                setup.config_version = next_version
                return True

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
                    raise RuntimeError("configuration defaults are not initialized")
                if (
                    claim is None
                    or claim.consumed_at is not None
                    or claim.token_hash != bootstrap_token_hash
                ):
                    raise ConfigConflict("bootstrap claim is unavailable")
                current = ModuleDocument.model_validate_json(row.config_value)
                if current.revision != expected_revision:
                    raise ConfigConflict(
                        f"expected revision {expected_revision}, current revision is {current.revision}"
                    )
                existing = (
                    await session.execute(
                        select(User)
                        .where(
                            User.external_id == username,
                            User.auth_provider == AuthProvider.BUILTIN,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    raise ConfigConflict("administrator username already exists")
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
                status["ready_at"] = datetime.now(timezone.utc).isoformat()
                setup.config_value = json.dumps(
                    status, sort_keys=True, separators=(",", ":")
                )
                setup.config_version = next_version
                claim.consumed_at = datetime.now(timezone.utc)
                claim.row_version += 1
            return row

    async def activate_managed_core_admin(
        self,
        *,
        expected_revision: int,
        document: ModuleDocument,
        username: str,
        password_hash: str,
        instance_id: str,
        instance_generation: int,
    ) -> SystemConfig:
        async with self.session_factory() as session:
            async with session.begin():
                row, _, _ = await self.activate_managed_core_admin_in_session(
                    session,
                    expected_revision=expected_revision,
                    document=document,
                    username=username,
                    password_hash=password_hash,
                    instance_id=instance_id,
                    instance_generation=instance_generation,
                )
            return row

    async def activate_managed_core_admin_in_session(
        self,
        session: AsyncSession,
        *,
        expected_revision: int,
        document: ModuleDocument,
        username: str,
        password_hash: str,
        instance_id: str,
        instance_generation: int,
    ) -> tuple[SystemConfig, ModuleDocument, int]:
        setup = await session.get(SystemConfig, "setup.status", with_for_update=True)
        row = await session.get(SystemConfig, "module.core_admin", with_for_update=True)
        claim = await session.get(
            ConfigBootstrapClaim, "bootstrap", with_for_update=True
        )
        identity = await session.get(
            ManagedInstanceBinding, instance_id, with_for_update=True
        )
        if setup is None or row is None or claim is None:
            raise RuntimeError("managed configuration defaults are not initialized")
        if identity is None or identity.instance_generation != instance_generation:
            raise ConfigConflict("managed identity does not match")
        if claim.consumed_at is not None:
            raise ConfigConflict("bootstrap claim is unavailable")
        current = ModuleDocument.model_validate_json(row.config_value)
        if current.revision != expected_revision:
            raise ConfigConflict(
                f"expected revision {expected_revision}, current revision is {current.revision}"
            )
        existing = await session.scalar(
            select(User)
            .where(
                User.external_id == username,
                User.auth_provider == AuthProvider.BUILTIN,
            )
            .with_for_update()
        )
        if existing is not None:
            raise ConfigConflict("administrator username already exists")
        from server.models import PasswordState

        session.add(
            User(
                external_id=username,
                display_name="Administrator",
                auth_provider=AuthProvider.BUILTIN,
                password_hash=password_hash,
                password_state=PasswordState.RESET_REQUIRED,
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
            )
        )
        next_version = setup.config_version + 1
        next_document = document.model_copy(update={"revision": current.revision + 1})
        row.config_value = self._serialize(next_document)
        row.config_version = next_version
        status = json.loads(setup.config_value)
        status["system_state"] = "READY"
        status["ready_at"] = utc_now().isoformat()
        setup.config_value = json.dumps(status, sort_keys=True, separators=(",", ":"))
        setup.config_version = next_version
        claim.consumed_at = utc_now()
        claim.row_version += 1
        return row, next_document, next_version

    async def set_initial_admin_password_in_session(
        self,
        session: AsyncSession,
        *,
        new_password: str,
        instance_id: str,
        instance_generation: int,
    ) -> tuple[int, str]:
        identity = await session.get(
            ManagedInstanceBinding, instance_id, with_for_update=True
        )
        if identity is None:
            raise ManagedIdentityConflict("INSTANCE_IDENTITY_MISMATCH")
        if identity.instance_generation != instance_generation:
            raise ManagedIdentityConflict("INSTANCE_GENERATION_MISMATCH")
        setup = await session.get(SystemConfig, "setup.status")
        module_row = await session.get(
            SystemConfig,
            "module.core_admin",
            with_for_update=True,
        )
        if setup is None or module_row is None:
            raise RuntimeError("managed administrator is not initialized")
        document = ModuleDocument.model_validate_json(module_row.config_value)
        if document.effective is None:
            raise RuntimeError("managed administrator is not active")
        username = document.effective.config.get("username")
        if not isinstance(username, str) or not username:
            raise RuntimeError("managed administrator identity is unavailable")
        user = await session.scalar(
            select(User)
            .where(
                User.external_id == username,
                User.auth_provider == AuthProvider.BUILTIN,
            )
            .with_for_update()
        )
        if user is None:
            raise RuntimeError("managed administrator is unavailable")
        credential_state = await mutate_builtin_password_in_session(
            session,
            user=user,
            mode=CredentialMutationMode.INITIALIZE,
            new_password=new_password,
        )
        return setup.config_version, credential_state.value

    async def activate_with_session_epoch(
        self,
        module: str,
        *,
        expected_revision: int,
        document: ModuleDocument,
        sso_test_id: str,
        initiator_user_id: str,
        identity_provider: str,
        external_subject: str,
        identity_snapshot_ciphertext: str,
    ) -> SystemConfig:
        """Activate SSO and invalidate human sessions in one transaction."""
        key = f"module.{module}"
        async with self.session_factory() as session:
            async with session.begin():
                setup = await session.get(
                    SystemConfig, "setup.status", with_for_update=True
                )
                row = await session.get(SystemConfig, key, with_for_update=True)
                token_row = await session.get(
                    SystemConfig,
                    "module.token_security",
                    with_for_update=True,
                )
                if setup is None or row is None or token_row is None:
                    raise RuntimeError("configuration defaults are not initialized")
                test = await session.get(
                    OIDCLoginState,
                    sso_test_id,
                    with_for_update=True,
                )
                if (
                    test is None
                    or test.purpose != "config_test"
                    or test.initiator_user_id != initiator_user_id
                    or test.status != "passed"
                    or test.identity_snapshot_ciphertext
                    != identity_snapshot_ciphertext
                    or _aware_utc(test.expires_at)
                    <= datetime.now(timezone.utc)
                ):
                    raise SSOTestConflict("SSO_TEST_REQUIRED")
                current = ModuleDocument.model_validate_json(row.config_value)
                if current.revision != expected_revision:
                    raise ConfigConflict(
                        f"expected revision {expected_revision}, current revision is {current.revision}"
                    )
                if (
                    test.config_revision != current.revision
                    or current.last_validation is None
                    or test.config_digest
                    != current.last_validation.config_digest
                ):
                    raise SSOTestConflict("SSO_TEST_STALE")
                user = await session.get(
                    User,
                    initiator_user_id,
                    with_for_update=True,
                )
                if user is None or user.role != UserRole.ADMIN:
                    raise SSOTestConflict("SSO_TEST_ADMIN_UNAVAILABLE")
                mapping = await session.scalar(
                    select(UserExternalIdentity)
                    .where(
                        UserExternalIdentity.identity_provider
                        == identity_provider,
                        UserExternalIdentity.external_subject
                        == external_subject,
                    )
                    .with_for_update()
                )
                if mapping is not None and mapping.user_id != initiator_user_id:
                    raise SSOTestConflict("SSO_IDENTITY_IN_USE")
                if mapping is None:
                    session.add(
                        UserExternalIdentity(
                            user_id=initiator_user_id,
                            identity_provider=identity_provider,
                            external_subject=external_subject,
                        )
                    )
                token_document = ModuleDocument.model_validate_json(
                    token_row.config_value
                )
                if token_document.effective is None:
                    raise RuntimeError("token_security is not active")
                token_config = dict(token_document.effective.config)
                token_config["session_epoch"] = (
                    int(token_config.get("session_epoch", 1)) + 1
                )
                token_effective = token_document.effective.model_copy(
                    update={
                        "revision": token_document.effective.revision + 1,
                        "config": token_config,
                    }
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
                    document.model_copy(update={"revision": current.revision + 1})
                )
                row.config_version = next_version
                token_row.config_value = self._serialize(token_document)
                token_row.config_version = next_version
                test.status = "consumed"
                test.consumed_at = datetime.now(timezone.utc)
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
                    statement = (
                        postgresql_insert(SystemConfig)
                        .values(**setup_values)
                        .on_conflict_do_nothing(index_elements=["config_key"])
                    )
                elif dialect == "sqlite":
                    statement = (
                        sqlite_insert(SystemConfig)
                        .values(**setup_values)
                        .on_conflict_do_nothing(index_elements=["config_key"])
                    )
                elif dialect == "mysql":
                    statement = (
                        mysql_insert(SystemConfig)
                        .values(**setup_values)
                        .prefix_with("IGNORE")
                    )
                else:
                    raise RuntimeError(f"Unsupported database dialect: {dialect}")
                winner = await session.execute(statement)
                if winner.rowcount != 1:
                    return False

                setup = await session.get(SystemConfig, "setup.status")
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
                    .where(ConfigBootstrapClaim.singleton_key == "bootstrap")
                    .with_for_update()
                )
                return result.scalar_one_or_none()
            return await session.get(ConfigBootstrapClaim, "bootstrap")

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
                    raise ConfigConflict("bootstrap claim is unavailable")
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

    async def delete_expired_receipts(self, *, now: datetime, limit: int = 500) -> int:
        async with self.session_factory() as session:
            ids = (
                (
                    await session.execute(
                        select(ConfigOperationReceipt.id)
                        .where(ConfigOperationReceipt.expires_at <= now)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                return 0
            await session.execute(
                delete(ConfigOperationReceipt).where(ConfigOperationReceipt.id.in_(ids))
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
                        ConfigOperationReceipt.actor_scope == actor_scope,
                        ConfigOperationReceipt.idempotency_key_hash
                        == idempotency_key_hash,
                    )
                )
            ).scalar_one_or_none()

    async def claim_managed_receipt(
        self,
        *,
        actor_scope: str,
        idempotency_key_hash: str,
        action: str,
        module: str | None,
        request_digest: str,
        expires_at: datetime,
        lease_owner: str,
        lease_expires_at: datetime,
        instance_id: str,
        instance_generation: int,
        now: datetime,
    ) -> ConfigOperationReceipt:
        for attempt in range(2):
            try:
                async with self.session_factory() as session:
                    async with session.begin():
                        setup = await session.get(
                            SystemConfig,
                            "setup.status",
                            with_for_update=True,
                        )
                        if setup is None:
                            raise RuntimeError(
                                "setup.status must exist before receipt claim"
                            )
                        receipt = (
                            await session.execute(
                                select(ConfigOperationReceipt)
                                .where(
                                    ConfigOperationReceipt.actor_scope == actor_scope,
                                    ConfigOperationReceipt.idempotency_key_hash
                                    == idempotency_key_hash,
                                )
                                .with_for_update()
                            )
                        ).scalar_one_or_none()
                        if receipt is None:
                            receipt = ConfigOperationReceipt(
                                actor_scope=actor_scope,
                                idempotency_key_hash=idempotency_key_hash,
                                action=action,
                                module=module,
                                request_digest=request_digest,
                                status=ConfigReceiptStatus.IN_PROGRESS,
                                response_json="{}",
                                expires_at=expires_at,
                                lease_owner=lease_owner,
                                lease_expires_at=lease_expires_at,
                                instance_id=instance_id,
                                instance_generation=instance_generation,
                            )
                            session.add(receipt)
                            await session.flush()
                            return receipt
                        if receipt.request_digest != request_digest:
                            raise ReceiptConflict(
                                "receipt key belongs to another request"
                            )
                        if receipt.status == ConfigReceiptStatus.SUCCEEDED:
                            return receipt
                        if receipt.status != ConfigReceiptStatus.IN_PROGRESS:
                            raise ReceiptConflict("receipt has an unsupported state")
                        current_lease = receipt.lease_expires_at
                        if (
                            current_lease is not None
                            and _aware_utc(current_lease) > now
                        ):
                            retry_after = max(
                                1,
                                int((_aware_utc(current_lease) - now).total_seconds()),
                            )
                            raise ReceiptInProgress(retry_after)
                        receipt.lease_owner = lease_owner
                        receipt.lease_expires_at = lease_expires_at
                        receipt.expires_at = expires_at
                        return receipt
            except IntegrityError:
                if attempt == 1:
                    raise
        raise RuntimeError("receipt claim retry exhausted")

    async def complete_managed_receipt(
        self,
        *,
        receipt_id: str,
        request_digest: str,
        lease_owner: str,
        mutation: ManagedMutation,
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            async with session.begin():
                if session.get_bind().dialect.name == "sqlite":
                    await session.execute(text("BEGIN IMMEDIATE"))
                receipt = await session.get(
                    ConfigOperationReceipt,
                    receipt_id,
                    with_for_update=True,
                )
                if receipt is None:
                    raise ReceiptLeaseLost("receipt no longer exists")
                if receipt.request_digest != request_digest:
                    raise ReceiptConflict("receipt key belongs to another request")
                if (
                    receipt.status != ConfigReceiptStatus.IN_PROGRESS
                    or receipt.lease_owner != lease_owner
                ):
                    raise ReceiptLeaseLost("operation lease is not owned")
                result = await mutation(session)
                receipt.status = ConfigReceiptStatus.SUCCEEDED
                receipt.response_json = json.dumps(
                    result,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                receipt.lease_owner = None
                receipt.lease_expires_at = None
                return result

    async def abandon_managed_receipt(
        self,
        *,
        receipt_id: str,
        request_digest: str,
        lease_owner: str,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                receipt = await session.get(
                    ConfigOperationReceipt,
                    receipt_id,
                    with_for_update=True,
                )
                if receipt is None:
                    return
                if (
                    receipt.request_digest != request_digest
                    or receipt.status != ConfigReceiptStatus.IN_PROGRESS
                    or receipt.lease_owner != lease_owner
                ):
                    raise ReceiptLeaseLost("operation lease is not owned")
                await session.delete(receipt)

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
                            ConfigOperationReceipt.actor_scope == actor_scope,
                            ConfigOperationReceipt.idempotency_key_hash
                            == idempotency_key_hash,
                        )
                    )
                ).scalar_one()
                return existing
        return receipt
