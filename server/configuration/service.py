from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from server.auth.oidc_login import (
    OIDC_LOGIN_PURPOSE_CONFIG_TEST,
    create_oidc_login,
)
from server.auth.credential_mutation import AdminPasswordAlreadyInitialized
from server.auth.password_policy import PasswordPolicyError, validate_password
from server.configuration.aliyun_access_mutation import (
    AliyunTransitionError,
    AliyunTransitionAudit,
    CREDENTIAL_BLOCK_KEYS,
    merge_aliyun_access_draft,
)
from server.configuration.bootstrap import (
    migrate_global_audit_documents,
    project_global_audit_documents,
)
from server.configuration.external_validation import (
    ExternalModuleValidator,
    ExternalValidationError,
    ExternalValidationResult,
    NoopExternalModuleValidator,
)
from server.configuration.idempotency import (
    ManagedCommandIdempotency,
    ManagedIdempotencyError,
    ManagedReceiptClaim,
)
from server.configuration.managed_policy import authorize_managed_command
from server.configuration.module_migrations import (
    ModuleMigrationError,
    decrypt_effective_config,
    migrate_document_for_write,
    secret_fields_for_document,
)
from server.configuration.registry import (
    MODULE_REGISTRY,
    ModuleDefinition,
    ModuleValidationResult,
    validate_module_config,
)
from server.configuration.secrets import (
    SecretInputError,
    configured_secret_fields,
    decrypt_secret_tree,
    export_secret_tree,
    merge_secret_tree,
    redact_secret_tree,
)
from server.configuration.repository import (
    ConfigConflict,
    ConfigRepository,
    ManagedIdentityConflict,
    SSOTestConflict,
)
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ConfigError,
    ConfigResult,
    EffectiveConfig,
    ModuleDocument,
    ModuleState,
    SystemState,
    ValidationOperation,
    ValidationProof,
    transition,
)
from server.core.config_crypto import ConfigCrypto
from server.core.crypto import decrypt
from server.logging import safe_request_id, trace_id_var
from server.config import OIDCConfig
from server.models import OIDCLoginState

VALIDATION_LEASE = timedelta(minutes=2)
VALIDATION_LIFETIME = timedelta(minutes=10)
RECEIPT_LIFETIME = timedelta(hours=24)
SIDE_EFFECTING_ACTIONS = {
    ConfigAction.ACTIVATE,
    ConfigAction.DISABLE,
    ConfigAction.SET_INITIAL_PASSWORD,
}
AUDIT_LOGGER = logging.getLogger("server.configuration.audit")
TRANSITION_LOGGER = logging.getLogger("server.configuration.transition")
GLOBAL_AUDIT_MODULES = frozenset({"sql_security", "observability"})


@dataclass(frozen=True, slots=True)
class _AliyunAuditContext:
    transition: AliyunTransitionAudit | None = None
    source_revision: int | None = None
    attempted_revision: int | None = None
    observed_revision: int | None = None
    result: str | None = None
    error_code: str | None = None
    request_id: str | None = None
    committed: bool = False


_aliyun_audit_context: ContextVar[_AliyunAuditContext | None] = ContextVar(
    "aliyun_audit_context", default=None
)


def _strip_whitespace(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return {field: _strip_whitespace(item) for field, item in value.items()}
    if isinstance(value, list):
        return [_strip_whitespace(item) for item in value]
    return value


class ConfigService:
    def __init__(
        self,
        repository: ConfigRepository,
        crypto: ConfigCrypto,
        external_validator: ExternalModuleValidator | None = None,
        *,
        allow_insecure_loopback_urls: bool = False,
        managed: bool = False,
    ) -> None:
        self.repository = repository
        self.managed = managed
        self.crypto = crypto
        self.external_validator = external_validator or NoopExternalModuleValidator()
        self.allow_insecure_loopback_urls = allow_insecure_loopback_urls
        self.managed_idempotency = ManagedCommandIdempotency(
            repository,
            crypto,
        )

    def _validate_module_config(
        self,
        module: str,
        config: dict[str, Any],
        *,
        effective_configs: dict[str, dict[str, Any]],
    ) -> ModuleValidationResult:
        return validate_module_config(
            module,
            config,
            effective_configs=effective_configs,
            allow_insecure_loopback_urls=self.allow_insecure_loopback_urls,
        )

    @staticmethod
    def _admin_user_id(actor: ConfigActor) -> str:
        if actor.actor_type != "admin" or not actor.scope.startswith("admin:"):
            raise ConfigError("ADMIN_REQUIRED", "Administrator access is required")
        user_id = actor.scope.removeprefix("admin:")
        if not user_id:
            raise ConfigError("ADMIN_REQUIRED", "Administrator access is required")
        return user_id

    async def _validated_user_sso_config(
        self,
        *,
        config_revision: int | None = None,
        config_digest: str | None = None,
    ) -> tuple[OIDCConfig, ModuleDocument, str]:
        document = await self.describe_internal("user_sso")
        proof = document.last_validation
        now = datetime.now(timezone.utc)
        if (
            document.workflow_state != ModuleState.VALIDATED
            or document.draft is None
            or proof is None
            or proof.expires_at <= now
            or proof.validated_revision != document.revision
            or (
                config_revision is not None
                and document.revision != config_revision
            )
            or (
                config_digest is not None
                and proof.config_digest != config_digest
            )
        ):
            raise ConfigError(
                "SSO_VALIDATION_REQUIRED",
                "Save and validate the current SSO configuration first",
            )
        plaintext = self._decrypt_config(
            "user_sso",
            document,
            document.draft,
        )
        checked = self._validate_module_config(
            "user_sso",
            plaintext,
            effective_configs=await self._effective_configs(),
        )
        if (
            not checked.valid
            or self.crypto.digest(checked.normalized_config)
            != proof.config_digest
        ):
            raise ConfigError(
                "SSO_VALIDATION_STALE",
                "The SSO configuration changed after validation",
            )
        runtime = await self._effective_configs()
        external_base_url = str(
            runtime.get("runtime_policy", {}).get("external_base_url", "")
        ).rstrip("/")
        callback_url = f"{external_base_url}/auth/oidc/callback"
        return (
            OIDCConfig.model_validate(
                {
                    **checked.normalized_config,
                    "redirect_uri": callback_url,
                }
            ),
            document,
            callback_url,
        )

    async def start_user_sso_test(
        self,
        actor: ConfigActor,
    ) -> dict[str, Any]:
        user_id = self._admin_user_id(actor)
        oidc_config, document, callback_url = (
            await self._validated_user_sso_config()
        )
        assert document.last_validation is not None
        async with self.repository.session_factory() as session:
            record, authorize_url = await create_oidc_login(
                session,
                oidc_config=oidc_config,
                callback_url=callback_url,
                purpose=OIDC_LOGIN_PURPOSE_CONFIG_TEST,
                initiator_user_id=user_id,
                config_revision=document.revision,
                config_digest=document.last_validation.config_digest,
            )
        return {
            "id": record.id,
            "status": record.status,
            "authorize_url": authorize_url,
            "expires_at": record.expires_at.isoformat(),
        }

    async def describe_user_sso_test(
        self,
        test_id: str,
        actor: ConfigActor,
    ) -> dict[str, Any]:
        user_id = self._admin_user_id(actor)
        async with self.repository.session_factory() as session:
            record = await session.get(OIDCLoginState, test_id)
        if (
            record is None
            or record.purpose != OIDC_LOGIN_PURPOSE_CONFIG_TEST
            or record.initiator_user_id != user_id
        ):
            raise ConfigError("SSO_TEST_NOT_FOUND", "SSO login test not found")
        return {
            "id": record.id,
            "status": record.status,
            "error_code": record.error_code,
            "expires_at": record.expires_at.isoformat(),
        }

    async def user_sso_test_callback_config(
        self,
        *,
        config_revision: int | None,
        config_digest: str | None,
    ) -> tuple[OIDCConfig, str]:
        oidc_config, _, callback_url = await self._validated_user_sso_config(
            config_revision=config_revision,
            config_digest=config_digest,
        )
        return oidc_config, callback_url

    async def execute(
        self,
        command: ConfigCommand,
        actor: ConfigActor,
    ) -> ConfigResult:
        started = time.monotonic()
        audit_token = _aliyun_audit_context.set(None)
        managed_atomic_command = (
            actor.actor_type == "managed_initializer"
            and command.action
            in {
                ConfigAction.ACTIVATE,
                ConfigAction.SET_INITIAL_PASSWORD,
            }
        )
        try:
            try:
                authorize_managed_command(command, actor)
                if (self.managed and command.module == "knowledge"
                        and command.action not in {ConfigAction.DESCRIBE, ConfigAction.EXPORT}
                        and not (actor.actor_type == "system" or (
                            actor.actor_type == "admin" and command.action in {
                                ConfigAction.PLAN, ConfigAction.SAVE_DRAFT,
                                ConfigAction.VALIDATE, ConfigAction.ACTIVATE,
                            }
                        ))):
                    raise ConfigError("KNOWLEDGE_MANAGED_BY_CONTROL_PLANE", "Use the knowledge setup or disable workflow with a PAS administrator")
                if (
                    command.action in SIDE_EFFECTING_ACTIONS
                    and not managed_atomic_command
                ):
                    replay = await self._idempotency_replay(command, actor)
                    if replay is not None:
                        self._audit(
                            command,
                            actor,
                            result=replay,
                            started=started,
                            replayed=True,
                            audit_context=_aliyun_audit_context.get(),
                        )
                        return replay

                handler = getattr(self, f"_execute_{command.action.value}")
                result: ConfigResult = await handler(command, actor)

                if (
                    command.action in SIDE_EFFECTING_ACTIONS
                    and not managed_atomic_command
                ):
                    await self._store_receipt(command, actor, result)
            except Exception as exc:
                self._audit(
                    command,
                    actor,
                    started=started,
                    error_code=(
                        exc.code if isinstance(exc, ConfigError) else "INTERNAL_ERROR"
                    ),
                    audit_context=_aliyun_audit_context.get(),
                )
                raise
            self._audit(
                command,
                actor,
                result=result,
                started=started,
                audit_context=_aliyun_audit_context.get(),
            )
            return result
        finally:
            _aliyun_audit_context.reset(audit_token)

    @staticmethod
    def _audit(
        command: ConfigCommand,
        actor: ConfigActor,
        *,
        started: float,
        result: ConfigResult | None = None,
        error_code: str | None = None,
        replayed: bool = False,
        audit_context: _AliyunAuditContext | None = None,
    ) -> None:
        module_result = result.module if result is not None else None
        if error_code is not None:
            result_status = "error"
            result_error_code = error_code
        else:
            result_status = (
                audit_context.result
                if audit_context is not None and audit_context.result is not None
                else "success"
            )
            result_error_code = (
                audit_context.error_code
                if audit_context is not None and audit_context.error_code is not None
                else None
            )
        audit_fields: dict[str, Any] = {
            "config_action": command.action.value,
            "config_module": command.module,
            "config_actor_type": actor.actor_type,
            "config_actor_scope": actor.scope,
            "config_result": result_status,
            "config_error_code": result_error_code,
            "config_changed_fields": tuple(
                sorted(
                    field
                    for field in (command.config or {})
                    if not (command.module == "core_admin" and field == "password")
                )
            ),
            "config_revision": (
                module_result.get("revision")
                if module_result is not None
                else (
                    audit_context.observed_revision
                    if audit_context is not None
                    and audit_context.observed_revision is not None
                    else (
                        audit_context.source_revision
                        if audit_context is not None
                        else None
                    )
                )
            ),
            "config_state": (
                module_result.get("workflow_state")
                if module_result is not None
                else None
            ),
            "config_replayed": replayed,
            "config_duration_ms": round((time.monotonic() - started) * 1000),
            "config_trace_id": safe_request_id(trace_id_var.get()),
            "config_request_id": (
                audit_context.request_id
                if audit_context is not None and audit_context.request_id is not None
                else ConfigService._result_request_id(result)
            ),
        }
        if audit_context is not None:
            audit_fields.update(
                {
                    "config_source_revision": audit_context.source_revision,
                    "config_attempted_revision": audit_context.attempted_revision,
                    "config_observed_revision": audit_context.observed_revision,
                }
            )
        transition_audit = (
            audit_context.transition if audit_context is not None else None
        )
        if transition_audit is not None:
            audit_fields.update(
                {
                    "aliyun_previous_mode": transition_audit.previous_mode,
                    "aliyun_selected_mode": transition_audit.selected_mode,
                    "aliyun_previous_mode_action": (
                        transition_audit.previous_mode_action
                    ),
                    "aliyun_selected_mode_action": (
                        transition_audit.selected_mode_action
                    ),
                    "aliyun_reused_direct_source": (
                        transition_audit.reused_direct_source
                    ),
                    "aliyun_credential_id_mask": (transition_audit.credential_id_mask),
                    "aliyun_delete_retained_modes": (
                        transition_audit.delete_retained_modes
                    ),
                    "aliyun_transition_committed": (audit_context.committed),
                }
            )
        AUDIT_LOGGER.info(
            "configuration command",
            extra=audit_fields,
        )

    @staticmethod
    def _result_request_id(result: ConfigResult | None) -> str | None:
        if result is None:
            return None
        for payload in (result.validation, result.plan):
            if isinstance(payload, dict):
                request_id = safe_request_id(payload.get("request_id"))
                if request_id is not None:
                    return request_id
        return None

    @staticmethod
    def _set_aliyun_audit_context(**changes: Any) -> None:
        current = _aliyun_audit_context.get() or _AliyunAuditContext()
        _aliyun_audit_context.set(replace(current, **changes))

    @staticmethod
    def _validation_request_id(
        validation: ExternalValidationResult | None,
    ) -> str | None:
        if validation is None:
            return None
        for check in validation.checks:
            request_id = safe_request_id(check.request_id)
            if request_id is not None:
                return request_id
        return None

    @staticmethod
    def _project_global_audit(
        documents: dict[str, ModuleDocument],
    ) -> dict[str, ModuleDocument]:
        try:
            return project_global_audit_documents(documents)
        except RuntimeError as exc:
            raise ConfigError(
                "MIGRATION_FAILED",
                "Global audit configuration migration failed",
            ) from exc

    async def describe_internal(self, module: str) -> ModuleDocument:
        self._definition(module)
        if module in GLOBAL_AUDIT_MODULES:
            stored_documents = await self.repository.list_modules()
            documents = self._project_global_audit(stored_documents)
            document = documents.get(module)
            stored_document = stored_documents.get(module)
        else:
            document = await self.repository.get_module(module)
            stored_document = document
        if document is None:
            raise ConfigError("UNKNOWN_MODULE", f"Unknown module: {module}")
        self._ensure_supported_schema(module, document)
        if module == "aliyun_access" and document.schema_version == 1:
            return document
        if (
            stored_document is not None
            and stored_document.schema_version != document.schema_version
        ):
            return document
        recovery = self._expired_recovery(document)
        if recovery is None:
            return document
        try:
            await self.repository.compare_and_set_module(
                module,
                expected_revision=document.revision,
                document=recovery,
            )
        except ConfigConflict:
            pass
        refreshed = await self.repository.get_module(module)
        if refreshed is not None:
            self._ensure_supported_schema(module, refreshed)
            return refreshed
        return document

    @staticmethod
    def _expired_recovery(document: ModuleDocument) -> ModuleDocument | None:
        now = datetime.now(timezone.utc)
        if (
            document.workflow_state == ModuleState.VALIDATING
            and document.validation_operation is not None
            and document.validation_operation.lease_expires_at <= now
        ):
            return transition(document, "recover_validation", now=now)
        if (
            document.workflow_state == ModuleState.VALIDATED
            and document.last_validation is not None
            and document.last_validation.expires_at <= now
        ):
            return transition(document, "save_draft", now=now)
        return None

    @staticmethod
    def _ensure_supported_schema(module: str, document: ModuleDocument) -> None:
        try:
            secret_fields_for_document(module, document)
        except ModuleMigrationError as exc:
            raise ConfigError("UNSUPPORTED_SCHEMA_VERSION", str(exc)) from exc

    def _definition(self, module: str | None) -> ModuleDefinition:
        if module is None or module not in MODULE_REGISTRY:
            raise ConfigError("UNKNOWN_MODULE", "A known module is required")
        return MODULE_REGISTRY[module]

    def _require_configurable(self, module: str | None) -> ModuleDefinition:
        definition = self._definition(module)
        if not definition.configurable:
            raise ConfigError(
                "MODULE_NOT_CONFIGURABLE",
                "This built-in module is always active and cannot be edited",
            )
        return definition

    @staticmethod
    def _require_revision(command: ConfigCommand) -> int:
        if command.expected_revision is None:
            raise ConfigError(
                "EXPECTED_REVISION_REQUIRED",
                "expected_revision is required",
            )
        return command.expected_revision

    async def _system_state(self) -> SystemState:
        core = await self.repository.get_module("core_admin")
        if (
            core is not None
            and core.effective is not None
            and core.effective.state == ModuleState.ACTIVE
        ):
            return SystemState.READY
        return SystemState.SETUP

    async def _result(
        self,
        *,
        module: str | None = None,
        document: ModuleDocument | None = None,
        validation: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
        export: dict[str, Any] | None = None,
    ) -> ConfigResult:
        return ConfigResult(
            config_version=await self.repository.global_version(),
            system_state=await self._system_state(),
            module=(
                self._public_module(module, document)
                if module is not None and document is not None
                else None
            ),
            validation=validation,
            plan=plan,
            export=export,
        )

    def _public_module(self, module: str, document: ModuleDocument) -> dict[str, Any]:
        definition = MODULE_REGISTRY[module]
        value = document.model_dump(mode="json")
        if value["draft"] is not None:
            value["draft"] = self._redact_document_config(
                module, document, definition, value["draft"]
            )
        if value["effective"] is not None:
            value["effective"]["config"] = self._redact_document_config(
                module,
                document,
                definition,
                value["effective"]["config"],
            )
        schema = definition.model.model_json_schema()
        if module == "core_admin":
            schema.setdefault("properties", {})["password"] = {
                "type": "string",
                "title": "Administrator password",
                "minLength": 12,
            }
            schema["required"] = [
                *schema.get("required", []),
                "password",
            ]
        value.update(
            {
                "name": module,
                "schema": schema,
                "ui_hints": {
                    **definition.ui_hints,
                    "secret_fields": [
                        *(spec.path for spec in definition.secret_fields),
                        *(("password",) if module == "core_admin" else ()),
                    ],
                },
                "dependencies": list(definition.dependencies),
                "dependents": self._dependents(module),
                "configurable": definition.configurable,
            }
        )
        if value["last_validation"] is not None:
            value["last_validation"].pop("validation_id_hash", None)
            value["last_validation"].pop("config_digest", None)
        return value

    @staticmethod
    def _dependents(module: str) -> list[str]:
        return [
            name
            for name, definition in MODULE_REGISTRY.items()
            if module in definition.dependencies
        ]

    @staticmethod
    def _redact_config(
        definition: ModuleDefinition, config: dict[str, Any]
    ) -> dict[str, Any]:
        return redact_secret_tree(config, secret_fields=definition.secret_fields)

    @staticmethod
    def _redact_document_config(
        module: str,
        document: ModuleDocument,
        definition: ModuleDefinition,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        del definition
        secret_fields = secret_fields_for_document(module, document)
        return redact_secret_tree(config, secret_fields=secret_fields)

    @staticmethod
    def _audit_aliyun_transition(audit: AliyunTransitionAudit) -> None:
        TRANSITION_LOGGER.info(
            "aliyun credential transition",
            extra={
                "credential_previous_mode": audit.previous_mode,
                "credential_selected_mode": audit.selected_mode,
                "credential_previous_mode_action": audit.previous_mode_action,
                "credential_selected_mode_action": audit.selected_mode_action,
                "credential_reused_direct_source": audit.reused_direct_source,
                "credential_id_mask": audit.credential_id_mask,
                "credential_deleted_retained_modes": (audit.delete_retained_modes),
            },
        )

    def _merge_draft(
        self,
        module: str,
        document: ModuleDocument,
        incoming: dict[str, Any],
    ) -> tuple[dict[str, Any], AliyunTransitionAudit | None]:
        definition = MODULE_REGISTRY[module]
        base = dict(
            document.draft
            or (document.effective.config if document.effective is not None else {})
        )
        if module == "aliyun_access" and (
            "credential_mode" in incoming
            or "transition" in incoming
            or CREDENTIAL_BLOCK_KEYS.intersection(incoming)
        ):
            try:
                return merge_aliyun_access_draft(
                    base,
                    _strip_whitespace(incoming),
                    crypto=self.crypto,
                    schema_version=document.schema_version,
                    now=datetime.now(timezone.utc),
                )
            except AliyunTransitionError as exc:
                raise ConfigError("INVALID_CREDENTIAL_TRANSITION", str(exc)) from exc
            except SecretInputError as exc:
                raise ConfigError("INVALID_SECRET_INPUT", str(exc)) from exc
        try:
            return (
                merge_secret_tree(
                    base,
                    {
                        field: _strip_whitespace(value)
                        for field, value in incoming.items()
                    },
                    secret_fields=definition.secret_fields,
                    crypto=self.crypto,
                    module=module,
                    schema_version=document.schema_version,
                    now=datetime.now(timezone.utc),
                ),
                None,
            )
        except SecretInputError as exc:
            raise ConfigError("INVALID_SECRET_INPUT", str(exc)) from exc

    def _decrypt_config(
        self,
        module: str,
        document: ModuleDocument,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        if module == "aliyun_access":
            effective = EffectiveConfig(
                revision=1,
                state=ModuleState.ACTIVE,
                config=config,
            )
            return decrypt_effective_config(
                module,
                document.model_copy(update={"effective": effective}),
                self.crypto,
            )
        return decrypt_secret_tree(
            config,
            secret_fields=MODULE_REGISTRY[module].secret_fields,
            crypto=self.crypto,
            module=module,
            schema_version=document.schema_version,
        )

    async def _effective_configs(self) -> dict[str, dict[str, Any]]:
        documents = self._project_global_audit(await self.repository.list_modules())
        return {
            name: decrypt_effective_config(name, document, self.crypto)
            for name, document in documents.items()
            if document.effective is not None
            and document.effective.state == ModuleState.ACTIVE
        }

    async def _prepare_document_for_write(
        self,
        module: str | None,
        expected_revision: int,
    ) -> tuple[ModuleDocument, int]:
        self._definition(module)
        assert module is not None
        document = await self.describe_internal(module)
        if module not in GLOBAL_AUDIT_MODULES:
            return (
                await self._migrate_document_for_write(module, document),
                expected_revision,
            )
        if document.revision != expected_revision:
            raise ConfigError(
                "REVISION_CONFLICT",
                f"expected revision {expected_revision}, current revision is {document.revision}",
            )

        stored = await self.repository.list_modules()
        sql_document = stored.get("sql_security")
        observability_document = stored.get("observability")
        if sql_document is None or observability_document is None:
            raise ConfigError(
                "MIGRATION_FAILED",
                "Global audit configuration modules are incomplete",
            )
        if (
            sql_document.schema_version,
            observability_document.schema_version,
        ) == (
            MODULE_REGISTRY["sql_security"].schema_version,
            MODULE_REGISTRY["observability"].schema_version,
        ):
            return document, expected_revision
        try:
            migrated_sql, migrated_observability = migrate_global_audit_documents(
                sql_document,
                observability_document,
            )
            await self.repository.migrate_global_audit_configuration(
                expected_sql_revision=sql_document.revision,
                sql_document=migrated_sql,
                expected_observability_revision=(observability_document.revision),
                observability_document=migrated_observability,
            )
        except ConfigConflict as exc:
            raise ConfigError("REVISION_CONFLICT", str(exc)) from exc
        except RuntimeError as exc:
            raise ConfigError(
                "MIGRATION_FAILED",
                "Global audit configuration migration failed",
            ) from exc
        migrated = await self.describe_internal(module)
        return migrated, migrated.revision

    async def _migrate_document_for_write(
        self, module: str, document: ModuleDocument
    ) -> ModuleDocument:
        if document.schema_version == MODULE_REGISTRY[module].schema_version:
            return document
        snapshot = await self.repository.get_module_snapshot(module)
        if snapshot is None or snapshot.document.revision != document.revision:
            raise ConfigError(
                "REVISION_CONFLICT",
                "Configuration changed before migration could begin",
            )
        try:
            document = self._expired_recovery(document) or document
            return migrate_document_for_write(
                module,
                document,
                crypto=self.crypto,
                legacy_updated_at=snapshot.updated_at or snapshot.created_at,
            )
        except ModuleMigrationError as exc:
            raise ConfigError(
                "MIGRATION_FAILED", "Configuration migration failed"
            ) from exc

    async def _dependency_revisions(self, module: str) -> dict[str, int]:
        revisions: dict[str, int] = {}
        for dependency in MODULE_REGISTRY[module].dependencies:
            document = await self.describe_internal(dependency)
            if (
                document.effective is None
                or document.effective.state != ModuleState.ACTIVE
            ):
                raise ConfigError(
                    "DEPENDENCY_NOT_ACTIVE",
                    f"Dependency {dependency} is not active",
                )
            revisions[dependency] = document.effective.revision
        return revisions

    async def _execute_describe(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        del actor
        if command.module is not None:
            document = await self.describe_internal(command.module)
            return await self._result(module=command.module, document=document)
        documents = await self.repository.list_modules()
        documents = self._project_global_audit(documents)
        result = await self._result()
        for name in MODULE_REGISTRY:
            self._ensure_supported_schema(name, documents[name])
        result.modules = [
            self._public_module(name, documents[name]) for name in MODULE_REGISTRY
        ]
        return result

    async def _execute_save_draft(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        module = command.module
        self._require_configurable(module)
        assert module is not None
        expected = self._require_revision(command)
        managed_claim = None
        if actor.actor_type == "managed_initializer":
            managed_claim = await self._claim_managed_command(command, actor)
            if managed_claim.status == "REPLAY":
                assert managed_claim.response is not None
                return ConfigResult.model_validate(managed_claim.response)
        try:
            document, expected = await self._prepare_document_for_write(
                module, expected
            )
        except Exception:
            if managed_claim is not None:
                await self._abandon_managed_claim(managed_claim, actor)
            raise
        draft, transition_audit = self._merge_draft(
            module, document, command.config or {}
        )
        if transition_audit is not None:
            self._set_aliyun_audit_context(
                transition=transition_audit,
                source_revision=document.revision,
                attempted_revision=expected,
            )
        updated = transition(document, "save_draft").model_copy(update={"draft": draft})
        if managed_claim is not None:
            assert actor.lease_owner is not None
            system_state = await self._system_state()

            async def save_draft(session):
                (
                    _,
                    stored_document,
                    config_version,
                ) = await self.repository.compare_and_set_module_in_session(
                    session,
                    module,
                    expected_revision=expected,
                    document=updated,
                )
                return ConfigResult(
                    config_version=config_version,
                    system_state=system_state,
                    module=self._public_module(module, stored_document),
                ).model_dump(mode="json", exclude_none=True)

            try:
                result = await self.managed_idempotency.complete(
                    managed_claim,
                    lease_owner=actor.lease_owner,
                    mutation=save_draft,
                )
            except ConfigConflict as exc:
                await self._abandon_managed_claim(managed_claim, actor)
                raise ConfigError("REVISION_CONFLICT", str(exc)) from exc
            except ManagedIdempotencyError as exc:
                raise ConfigError(exc.code, exc.code) from exc
            return ConfigResult.model_validate(result)
        try:
            await self.repository.compare_and_set_module(
                module,
                expected_revision=expected,
                document=updated,
            )
        except ConfigConflict as exc:
            observed = await self.repository.get_module(module)
            self._set_aliyun_audit_context(
                observed_revision=(observed.revision if observed is not None else None),
                result="error",
                error_code="REVISION_CONFLICT",
            )
            raise ConfigError("REVISION_CONFLICT", str(exc)) from exc
        if transition_audit is not None:
            self._audit_aliyun_transition(transition_audit)
            self._set_aliyun_audit_context(committed=True)
        stored = await self.describe_internal(module)
        return await self._result(module=module, document=stored)

    async def _execute_skip(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        del actor
        module = command.module
        definition = self._require_configurable(module)
        if not definition.optional:
            raise ConfigError(
                "MODULE_NOT_OPTIONAL", "Required modules cannot be skipped"
            )
        return await self._transition_and_store(command, "skip")

    async def _execute_reset(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        del actor
        self._require_configurable(command.module)
        return await self._transition_and_store(command, "reset")

    async def _transition_and_store(
        self, command: ConfigCommand, action: str
    ) -> ConfigResult:
        module = command.module
        self._definition(module)
        assert module is not None
        expected = self._require_revision(command)
        document, expected = await self._prepare_document_for_write(module, expected)
        try:
            updated = transition(document, action)
            await self.repository.compare_and_set_module(
                module,
                expected_revision=expected,
                document=updated,
            )
        except (ConfigConflict, ValueError) as exc:
            code = (
                "REVISION_CONFLICT"
                if isinstance(exc, ConfigConflict)
                else getattr(exc, "code", "INVALID_STATE_TRANSITION")
            )
            raise ConfigError(code, str(exc)) from exc
        stored = await self.describe_internal(module)
        return await self._result(module=module, document=stored)

    async def _execute_plan(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        del actor
        module = command.module
        definition = self._require_configurable(module)
        document = await self.describe_internal(module)
        document = await self._migrate_document_for_write(module, document)
        incoming = dict(command.config or {})
        password = incoming.pop("password", None) if module == "core_admin" else None
        candidate, transition_audit = self._merge_draft(module, document, incoming)
        if transition_audit is not None:
            self._set_aliyun_audit_context(
                transition=transition_audit,
                source_revision=document.revision,
            )
        plaintext = self._decrypt_config(module, document, candidate)
        checked = self._validate_module_config(
            module,
            plaintext,
            effective_configs=await self._effective_configs(),
        )
        if module == "core_admin" and (not isinstance(password, str)):
            checked = ModuleValidationResult(
                valid=False,
                normalized_config=checked.normalized_config,
                error_code="INVALID_ADMIN_PASSWORD",
                message=("Administrator password must be at least 12 characters"),
            )
        elif module == "core_admin":
            assert isinstance(password, str)
            try:
                validate_password(password)
            except PasswordPolicyError:
                checked = ModuleValidationResult(
                    valid=False,
                    normalized_config=checked.normalized_config,
                    error_code="INVALID_ADMIN_PASSWORD",
                    message=("Administrator password must be at least 12 characters"),
                )
        external_validation: ExternalValidationResult | None = None
        confirmation_allowed = False
        confirmation_error_code: str | None = None
        external_request_id: str | None = None
        if checked.valid:
            try:
                await self._dependency_revisions(module)
            except ConfigError as error:
                checked = ModuleValidationResult(
                    valid=False,
                    normalized_config=checked.normalized_config,
                    error_code=error.code,
                    message=error.message,
                )
        if checked.valid and module in {"aliyun_access", "user_sso"}:
            try:
                external_validation = await self.external_validator.validate(
                    module,
                    checked.normalized_config,
                )
            except ExternalValidationError as error:
                checked = ModuleValidationResult(
                    valid=False,
                    normalized_config=checked.normalized_config,
                    error_code=error.code,
                    message=error.message,
                )
                confirmation_allowed = error.code != "OPENAPI_ENDPOINT_UNSUPPORTED"
                confirmation_error_code = error.code
                external_request_id = error.request_id
        if module == "aliyun_access":
            self._set_aliyun_audit_context(
                result="success" if checked.valid else "error",
                error_code=checked.error_code,
                request_id=(
                    external_request_id
                    or self._validation_request_id(external_validation)
                ),
            )
        return await self._result(
            plan={
                "module": module,
                "valid": checked.valid,
                "error_code": checked.error_code,
                "message": checked.message,
                "config": self._redact_config(
                    definition,
                    checked.normalized_config if checked.valid else plaintext,
                ),
                "dependencies": list(definition.dependencies),
                "writes": False,
                "confirmation_allowed": confirmation_allowed,
                "confirmation_error_code": confirmation_error_code,
                **(
                    {"request_id": external_request_id}
                    if external_request_id is not None
                    else {}
                ),
                **(
                    {"external_validation": external_validation.as_dict()}
                    if external_validation is not None
                    else {}
                ),
            }
        )

    async def _execute_validate(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        if actor.actor_type == "managed_initializer":
            return await self._execute_managed_validate(command, actor)
        del actor
        module = command.module
        self._require_configurable(module)
        assert module is not None
        expected = self._require_revision(command)
        document, expected = await self._prepare_document_for_write(module, expected)
        if document.draft is None:
            raise ConfigError("DRAFT_REQUIRED", "No draft is available")
        now = datetime.now(timezone.utc)
        operation_id = secrets.token_urlsafe(24)
        validating = transition(document, "begin_validation", now=now).model_copy(
            update={
                "validation_operation": ValidationOperation(
                    operation_id=operation_id,
                    started_at=now,
                    lease_expires_at=now + VALIDATION_LEASE,
                )
            }
        )
        try:
            await self.repository.compare_and_set_module(
                module,
                expected_revision=expected,
                document=validating,
            )
        except ConfigConflict as exc:
            raise ConfigError("REVISION_CONFLICT", str(exc)) from exc
        validating = await self.describe_internal(module)
        plaintext = self._decrypt_config(module, validating, validating.draft or {})
        checked = self._validate_module_config(
            module,
            plaintext,
            effective_configs=await self._effective_configs(),
        )
        if not checked.valid:
            failed = transition(validating, "validation_failed").model_copy(
                update={"last_error_code": checked.error_code}
            )
            await self.repository.compare_and_set_module(
                module,
                expected_revision=validating.revision,
                document=failed,
            )
            raise ConfigError(
                checked.error_code or "VALIDATION_FAILED",
                checked.message or "Configuration validation failed",
            )

        external_validation: ExternalValidationResult | None = None
        warnings: tuple[str, ...] = ()
        external_request_id: str | None = None
        try:
            dependency_revisions = await self._dependency_revisions(module)
        except ConfigError as error:
            failed = transition(validating, "validation_failed").model_copy(
                update={"last_error_code": error.code}
            )
            await self.repository.compare_and_set_module(
                module,
                expected_revision=validating.revision,
                document=failed,
            )
            raise

        if module in {"aliyun_access", "user_sso"}:
            try:
                external_validation = await self.external_validator.validate(
                    module,
                    checked.normalized_config,
                )
            except ExternalValidationError as error:
                if (
                    module == "aliyun_access"
                    and
                    command.confirm_impact
                    and error.code != "OPENAPI_ENDPOINT_UNSUPPORTED"
                ):
                    warnings = (error.code,)
                    external_request_id = error.request_id
                    external_validation = ExternalValidationResult(status="WARNING")
                else:
                    failed = transition(validating, "validation_failed").model_copy(
                        update={"last_error_code": error.code}
                    )
                    await self.repository.compare_and_set_module(
                        module,
                        expected_revision=validating.revision,
                        document=failed,
                    )
                    if module == "aliyun_access":
                        self._set_aliyun_audit_context(
                            result="error",
                            error_code=error.code,
                            request_id=error.request_id,
                        )
                    raise ConfigError(error.code, error.message) from None

        validation_id = secrets.token_urlsafe(32)
        final_revision = validating.revision + 1
        proof = ValidationProof(
            status="PASSED",
            checked_at=now,
            expires_at=now + VALIDATION_LIFETIME,
            validation_id_hash=self._token_hash(validation_id),
            validated_revision=final_revision,
            config_digest=self.crypto.digest(checked.normalized_config),
            dependency_revisions=dependency_revisions,
            warnings=warnings,
            request_id=external_request_id,
        )
        passed = transition(validating, "validation_passed").model_copy(
            update={"last_validation": proof}
        )
        await self.repository.compare_and_set_module(
            module,
            expected_revision=validating.revision,
            document=passed,
        )
        stored = await self.describe_internal(module)
        if module == "aliyun_access":
            self._set_aliyun_audit_context(
                result="success",
                request_id=(
                    proof.request_id or self._validation_request_id(external_validation)
                ),
            )
        return await self._result(
            module=module,
            document=stored,
            validation={
                "status": "PASSED",
                "validation_id": validation_id,
                "expires_at": proof.expires_at.isoformat(),
                "warnings": list(proof.warnings),
                **(
                    {"request_id": proof.request_id}
                    if proof.request_id is not None
                    else {}
                ),
                **(
                    {"external_validation": external_validation.as_dict()}
                    if external_validation is not None
                    else {}
                ),
            },
        )

    async def _execute_managed_validate(
        self,
        command: ConfigCommand,
        actor: ConfigActor,
    ) -> ConfigResult:
        module = command.module
        self._definition(module)
        assert module is not None
        expected = self._require_revision(command)
        claim = await self._claim_managed_command(command, actor)
        if claim.status == "REPLAY":
            assert claim.response is not None
            return ConfigResult.model_validate(claim.response)
        try:
            document, expected = await self._prepare_document_for_write(
                module, expected
            )
        except Exception:
            await self._abandon_managed_claim(claim, actor)
            raise
        if document.draft is None:
            await self._abandon_managed_claim(claim, actor)
            raise ConfigError("DRAFT_REQUIRED", "No draft is available")
        now = datetime.now(timezone.utc)
        validating = transition(document, "begin_validation", now=now).model_copy(
            update={
                "validation_operation": ValidationOperation(
                    operation_id=secrets.token_urlsafe(24),
                    started_at=now,
                    lease_expires_at=now + VALIDATION_LEASE,
                )
            }
        )
        plaintext = self._decrypt_config(module, document, document.draft)
        checked = self._validate_module_config(
            module,
            plaintext,
            effective_configs=await self._effective_configs(),
        )
        if not checked.valid:
            await self._abandon_managed_claim(claim, actor)
            raise ConfigError(
                checked.error_code or "VALIDATION_FAILED",
                checked.message or "Configuration validation failed",
            )
        dependency_revisions = await self._dependency_revisions(module)
        validation_id = secrets.token_urlsafe(32)
        final_revision = validating.revision + 1
        proof = ValidationProof(
            status="PASSED",
            checked_at=now,
            expires_at=now + VALIDATION_LIFETIME,
            validation_id_hash=self._token_hash(validation_id),
            validated_revision=final_revision,
            config_digest=self.crypto.digest(checked.normalized_config),
            dependency_revisions=dependency_revisions,
        )
        passed = transition(validating, "validation_passed").model_copy(
            update={"last_validation": proof}
        )
        assert actor.lease_owner is not None
        system_state = await self._system_state()

        async def validate(session):
            (
                _,
                stored_validating,
                _,
            ) = await self.repository.compare_and_set_module_in_session(
                session,
                module,
                expected_revision=expected,
                document=validating,
            )
            (
                _,
                stored_document,
                config_version,
            ) = await self.repository.compare_and_set_module_in_session(
                session,
                module,
                expected_revision=stored_validating.revision,
                document=passed,
            )
            return ConfigResult(
                config_version=config_version,
                system_state=system_state,
                module=self._public_module(module, stored_document),
                validation={
                    "status": "PASSED",
                    "validation_id": validation_id,
                    "expires_at": proof.expires_at.isoformat(),
                },
            ).model_dump(mode="json", exclude_none=True)

        try:
            result = await self.managed_idempotency.complete(
                claim,
                lease_owner=actor.lease_owner,
                mutation=validate,
            )
        except ConfigConflict as exc:
            await self._abandon_managed_claim(claim, actor)
            raise ConfigError("REVISION_CONFLICT", str(exc)) from exc
        except ManagedIdempotencyError as exc:
            raise ConfigError(exc.code, exc.code) from exc
        return ConfigResult.model_validate(result)

    async def _execute_activate(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        module = command.module
        self._require_configurable(module)
        expected = self._require_revision(command)
        receipt_claim = None
        if actor.actor_type == "managed_initializer":
            receipt_claim = await self._claim_managed_command(command, actor)
            if receipt_claim.status == "REPLAY":
                assert receipt_claim.response is not None
                return ConfigResult.model_validate(receipt_claim.response)
        try:
            document, expected = await self._prepare_document_for_write(
                module, expected
            )
        except Exception:
            if receipt_claim is not None:
                await self._abandon_managed_claim(receipt_claim, actor)
            raise
        proof = document.last_validation
        validation_id = command.validation_id
        now = datetime.now(timezone.utc)
        stale = (
            document.workflow_state != ModuleState.VALIDATED
            or document.draft is None
            or proof is None
            or validation_id is None
            or proof.validation_id_hash != self._token_hash(validation_id)
            or proof.expires_at <= now
            or proof.validated_revision != document.revision
        )
        if not stale:
            plaintext = self._decrypt_config(module, document, document.draft)
            checked = self._validate_module_config(
                module,
                plaintext,
                effective_configs=await self._effective_configs(),
            )
            stale = (
                not checked.valid
                or proof.config_digest != self.crypto.digest(checked.normalized_config)
                or proof.dependency_revisions
                != await self._dependency_revisions(module)
            )
        if stale:
            if receipt_claim is not None:
                await self._abandon_managed_claim(receipt_claim, actor)
            raise ConfigError(
                "VALIDATION_STALE",
                "The validation proof is missing, expired, or stale",
            )
        next_revision = document.revision + 1
        activated = transition(document, "activate").model_copy(
            update={
                "desired_state": ModuleState.ACTIVE,
                "draft": None,
                "last_validation": None,
                "effective": EffectiveConfig(
                    revision=next_revision,
                    state=ModuleState.ACTIVE,
                    config=document.draft,
                ),
            }
        )
        try:
            if module == "core_admin":
                from server.auth.builtin import hash_password

                if actor.actor_type == "managed_initializer":
                    assert actor.lease_owner is not None
                    assert actor.instance_id is not None
                    assert actor.instance_generation is not None
                    assert receipt_claim is not None
                    random_password = secrets.token_urlsafe(48)
                    password_hash = hash_password(random_password)
                    del random_password

                    async def activate(session):
                        (
                            _,
                            stored_document,
                            config_version,
                        ) = await self.repository.activate_managed_core_admin_in_session(
                            session,
                            expected_revision=expected,
                            document=activated,
                            username=str(document.draft["username"]),
                            password_hash=password_hash,
                            instance_id=actor.instance_id,
                            instance_generation=(actor.instance_generation),
                        )
                        return ConfigResult(
                            config_version=config_version,
                            system_state=SystemState.READY,
                            module=self._public_module(module, stored_document),
                        ).model_dump(mode="json", exclude_none=True)

                    try:
                        result = await self.managed_idempotency.complete(
                            receipt_claim,
                            lease_owner=actor.lease_owner,
                            mutation=activate,
                        )
                    except ManagedIdempotencyError as exc:
                        raise ConfigError(exc.code, exc.code) from exc
                    return ConfigResult.model_validate(result)
                else:
                    password = (command.config or {}).get("password")
                    if not isinstance(password, str):
                        raise ConfigError(
                            "INVALID_ADMIN_PASSWORD",
                            "Administrator password must be at least 12 characters",
                        )
                    try:
                        validate_password(password)
                    except PasswordPolicyError as exc:
                        raise ConfigError(
                            "INVALID_ADMIN_PASSWORD",
                            "Administrator password must be at least 12 characters",
                        ) from exc
                    if actor.actor_type != "bootstrap" or actor.credential_hash is None:
                        raise ConfigError(
                            "BOOTSTRAP_REQUIRED",
                            "Bootstrap ownership is required",
                        )
                    await self.repository.activate_core_admin(
                        expected_revision=expected,
                        document=activated,
                        username=str(document.draft["username"]),
                        password_hash=hash_password(password),
                        bootstrap_token_hash=actor.credential_hash,
                    )
            elif module == "knowledge":
                from server.features.knowledge import activate_configuration
                await activate_configuration(self.repository, expected_revision=expected, document=activated)
            elif module == "user_sso":
                plaintext_draft = self._decrypt_config(
                    "user_sso",
                    document,
                    document.draft or {},
                )
                browser_login_enabled = plaintext_draft.get(
                    "browser_login_enabled", True
                ) is True
                if not browser_login_enabled:
                    await self.repository.compare_and_set_module(
                        module,
                        expected_revision=expected,
                        document=activated,
                    )
                elif not command.sso_test_id:
                    raise ConfigError(
                        "SSO_TEST_REQUIRED",
                        "A successful browser SSO test is required",
                    )
                else:
                    user_id = self._admin_user_id(actor)
                    async with self.repository.session_factory() as session:
                        test = await session.get(
                            OIDCLoginState,
                            command.sso_test_id,
                        )
                    if (
                        test is None
                        or not test.identity_snapshot_ciphertext
                    ):
                        raise ConfigError(
                            "SSO_TEST_REQUIRED",
                            "A successful browser SSO test is required",
                        )
                    try:
                        identity = json.loads(
                            decrypt(test.identity_snapshot_ciphertext)
                        )
                        provider_name = identity["provider_name"]
                        subject = identity["subject"]
                        if not isinstance(provider_name, str) or not isinstance(
                            subject,
                            str,
                        ):
                            raise ValueError
                    except (KeyError, TypeError, ValueError):
                        raise ConfigError(
                            "SSO_TEST_INVALID",
                            "The SSO login test proof is invalid",
                        ) from None
                    await self.repository.activate_with_session_epoch(
                        module,
                        expected_revision=expected,
                        document=activated,
                        sso_test_id=command.sso_test_id,
                        initiator_user_id=user_id,
                        identity_provider=provider_name,
                        external_subject=subject,
                        identity_snapshot_ciphertext=(
                            test.identity_snapshot_ciphertext
                        ),
                    )
            else:
                await self.repository.compare_and_set_module(
                    module,
                    expected_revision=expected,
                    document=activated,
                )
        except ConfigConflict as exc:
            if receipt_claim is not None:
                await self._abandon_managed_claim(receipt_claim, actor)
            raise ConfigError("REVISION_CONFLICT", str(exc)) from exc
        except SSOTestConflict as exc:
            if receipt_claim is not None:
                await self._abandon_managed_claim(receipt_claim, actor)
            raise ConfigError(exc.code, exc.code) from exc
        stored = await self.describe_internal(module)
        return await self._result(module=module, document=stored)

    async def _execute_set_initial_password(
        self,
        command: ConfigCommand,
        actor: ConfigActor,
    ) -> ConfigResult:
        if actor.actor_type != "managed_initializer":
            raise ConfigError(
                "CONFIG_OPERATION_NOT_ALLOWED",
                "Initial password setup is only available to managed initialization",
            )
        assert actor.instance_id is not None
        assert actor.instance_generation is not None
        assert actor.lease_owner is not None
        password = (command.config or {}).get("password")
        if not isinstance(password, str):
            raise ConfigError(
                "INVALID_ADMIN_PASSWORD",
                "Administrator password does not satisfy the password policy",
            )
        try:
            validate_password(password)
        except PasswordPolicyError as exc:
            raise ConfigError(
                "INVALID_ADMIN_PASSWORD",
                "Administrator password must be at least 12 characters",
            ) from exc
        claim = await self._claim_managed_command(command, actor)
        if claim.status == "REPLAY":
            assert claim.response is not None
            return ConfigResult.model_validate(claim.response)

        async def set_password(session):
            (
                config_version,
                credential_state,
            ) = await self.repository.set_initial_admin_password_in_session(
                session,
                new_password=password,
                instance_id=actor.instance_id,
                instance_generation=actor.instance_generation,
            )
            return ConfigResult(
                config_version=config_version,
                system_state=SystemState.READY,
                credential_state=credential_state,
            ).model_dump(mode="json", exclude_none=True)

        try:
            result = await self.managed_idempotency.complete(
                claim,
                lease_owner=actor.lease_owner,
                mutation=set_password,
            )
        except AdminPasswordAlreadyInitialized as exc:
            try:
                await self.managed_idempotency.abandon(
                    claim,
                    lease_owner=actor.lease_owner,
                )
            except ManagedIdempotencyError:
                pass
            raise ConfigError(
                "ADMIN_PASSWORD_ALREADY_INITIALIZED",
                "Administrator password is already initialized",
            ) from exc
        except ManagedIdentityConflict as exc:
            raise ConfigError(exc.code, exc.code) from exc
        except ManagedIdempotencyError as exc:
            raise ConfigError(exc.code, exc.code) from exc
        return ConfigResult.model_validate(result)

    async def _execute_disable(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        del actor
        module = command.module
        definition = self._require_configurable(module)
        if not definition.optional:
            raise ConfigError(
                "MODULE_NOT_OPTIONAL",
                "Required or system modules cannot be disabled",
            )
        active_dependents = []
        for dependent in self._dependents(module):
            document = await self.describe_internal(dependent)
            if (
                document.effective is not None
                and document.effective.state == ModuleState.ACTIVE
            ):
                active_dependents.append(dependent)
        if active_dependents:
            raise ConfigError(
                "ACTIVE_DEPENDENTS",
                "Disable dependents first: " + ", ".join(active_dependents),
            )
        return await self._transition_and_store(command, "disable")

    async def _execute_export(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        del actor
        documents = self._project_global_audit(await self.repository.list_modules())
        selected = [command.module] if command.module is not None else MODULE_REGISTRY
        modules: dict[str, Any] = {}
        for module in selected:
            self._definition(module)
            document = documents[module]
            self._ensure_supported_schema(module, document)
            source = document.draft or (
                document.effective.config if document.effective is not None else {}
            )
            secret_fields = secret_fields_for_document(module, document)
            configured = configured_secret_fields(source, secret_fields=secret_fields)
            modules[module] = {
                "desired_state": (
                    document.desired_state or document.workflow_state
                ).value,
                "config": export_secret_tree(source, secret_fields=secret_fields),
                "metadata": {"configured_secret_fields": configured},
            }
        return await self._result(export={"protocol_version": 1, "modules": modules})

    @staticmethod
    def _token_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    async def _claim_managed_command(
        self,
        command: ConfigCommand,
        actor: ConfigActor,
    ) -> ManagedReceiptClaim:
        assert actor.actor_type == "managed_initializer"
        assert actor.instance_id is not None
        assert actor.instance_generation is not None
        assert actor.lease_owner is not None
        if command.idempotency_key is None:
            raise ConfigError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "idempotency_key is required",
            )
        try:
            await self.repository.verify_managed_identity(
                instance_id=actor.instance_id,
                instance_generation=actor.instance_generation,
            )
            return await self.managed_idempotency.claim(
                actor_scope=actor.scope,
                idempotency_key=command.idempotency_key,
                request={
                    "command": command.model_dump(
                        mode="json",
                        exclude={"idempotency_key"},
                        exclude_none=True,
                    ),
                    "instance_id": actor.instance_id,
                    "instance_generation": actor.instance_generation,
                },
                action=command.action.value,
                module=command.module,
                instance_id=actor.instance_id,
                instance_generation=actor.instance_generation,
                lease_owner=actor.lease_owner,
            )
        except ManagedIdentityConflict as exc:
            raise ConfigError(exc.code, exc.code) from exc
        except ManagedIdempotencyError as exc:
            raise ConfigError(exc.code, exc.code) from exc

    async def _abandon_managed_claim(
        self,
        claim: ManagedReceiptClaim,
        actor: ConfigActor,
    ) -> None:
        assert actor.lease_owner is not None
        try:
            await self.managed_idempotency.abandon(
                claim,
                lease_owner=actor.lease_owner,
            )
        except ManagedIdempotencyError:
            pass

    def _request_digest(self, command: ConfigCommand) -> str:
        value = command.model_dump(
            mode="json",
            exclude={"idempotency_key"},
            exclude_none=True,
        )
        return self.crypto.digest(value)

    async def _idempotency_replay(
        self,
        command: ConfigCommand,
        actor: ConfigActor,
    ) -> ConfigResult | None:
        if not command.idempotency_key:
            raise ConfigError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "idempotency_key is required",
            )
        receipt = await self.repository.get_receipt(
            actor_scope=actor.scope,
            idempotency_key_hash=self._token_hash(command.idempotency_key),
        )
        if receipt is None:
            return None
        if receipt.request_digest != self._request_digest(command):
            raise ConfigError(
                "IDEMPOTENCY_CONFLICT",
                "Idempotency key was used for another request",
            )
        try:
            return ConfigResult.model_validate_json(receipt.response_json)
        except (ValidationError, ValueError) as exc:
            raise ConfigError(
                "IDEMPOTENCY_RECEIPT_INVALID",
                "Stored operation result is invalid",
            ) from exc

    async def _store_receipt(
        self,
        command: ConfigCommand,
        actor: ConfigActor,
        result: ConfigResult,
    ) -> None:
        assert command.idempotency_key is not None
        receipt = await self.repository.store_receipt(
            actor_scope=actor.scope,
            idempotency_key_hash=self._token_hash(command.idempotency_key),
            action=command.action.value,
            module=command.module,
            request_digest=self._request_digest(command),
            response_json=result.model_dump_json(exclude_none=True),
            expires_at=datetime.now(timezone.utc) + RECEIPT_LIFETIME,
        )
        if receipt.request_digest != self._request_digest(command):
            raise ConfigError(
                "IDEMPOTENCY_CONFLICT",
                "Idempotency key was used for another request",
            )
