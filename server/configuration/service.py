from __future__ import annotations

import hashlib
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from server.configuration.external_validation import (
    ExternalModuleValidator,
    ExternalValidationError,
    ExternalValidationResult,
    NoopExternalModuleValidator,
)
from server.configuration.registry import (
    MODULE_REGISTRY,
    ModuleDefinition,
    ModuleValidationResult,
    validate_module_config,
)
from server.configuration.repository import (
    ConfigConflict,
    ConfigRepository,
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
from server.core.config_crypto import ConfigCrypto, SecretEnvelope

VALIDATION_LEASE = timedelta(minutes=2)
VALIDATION_LIFETIME = timedelta(minutes=10)
RECEIPT_LIFETIME = timedelta(hours=24)
SIDE_EFFECTING_ACTIONS = {
    ConfigAction.ACTIVATE,
    ConfigAction.DISABLE,
}
AUDIT_LOGGER = logging.getLogger("server.configuration.audit")


def _strip_whitespace(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [_strip_whitespace(item) for item in value]
    return value


class ConfigService:
    def __init__(
        self,
        repository: ConfigRepository,
        crypto: ConfigCrypto,
        external_validator: ExternalModuleValidator | None = None,
    ) -> None:
        self.repository = repository
        self.crypto = crypto
        self.external_validator = (
            external_validator or NoopExternalModuleValidator()
        )

    async def execute(
        self,
        command: ConfigCommand,
        actor: ConfigActor,
    ) -> ConfigResult:
        started = time.monotonic()
        try:
            if command.action in SIDE_EFFECTING_ACTIONS:
                replay = await self._idempotency_replay(command, actor)
                if replay is not None:
                    self._audit(
                        command,
                        actor,
                        result=replay,
                        started=started,
                        replayed=True,
                    )
                    return replay

            handler = getattr(
                self, f"_execute_{command.action.value}"
            )
            result: ConfigResult = await handler(command, actor)

            if command.action in SIDE_EFFECTING_ACTIONS:
                await self._store_receipt(command, actor, result)
        except Exception as exc:
            self._audit(
                command,
                actor,
                started=started,
                error_code=(
                    exc.code
                    if isinstance(exc, ConfigError)
                    else "INTERNAL_ERROR"
                ),
            )
            raise
        self._audit(
            command, actor, result=result, started=started
        )
        return result

    @staticmethod
    def _audit(
        command: ConfigCommand,
        actor: ConfigActor,
        *,
        started: float,
        result: ConfigResult | None = None,
        error_code: str | None = None,
        replayed: bool = False,
    ) -> None:
        module_result = result.module if result is not None else None
        AUDIT_LOGGER.info(
            "configuration command",
            extra={
                "config_action": command.action.value,
                "config_module": command.module,
                "config_actor_type": actor.actor_type,
                "config_actor_scope": actor.scope,
                "config_result": (
                    "error" if error_code is not None else "success"
                ),
                "config_error_code": error_code,
                "config_changed_fields": tuple(
                    sorted(
                        field
                        for field in (command.config or {})
                        if not (
                            command.module == "core_admin"
                            and field == "password"
                        )
                    )
                ),
                "config_revision": (
                    module_result.get("revision")
                    if module_result is not None
                    else None
                ),
                "config_state": (
                    module_result.get("workflow_state")
                    if module_result is not None
                    else None
                ),
                "config_replayed": replayed,
                "config_duration_ms": round(
                    (time.monotonic() - started) * 1000
                ),
            },
        )

    async def describe_internal(self, module: str) -> ModuleDocument:
        self._definition(module)
        document = await self.repository.get_module(module)
        if document is None:
            raise ConfigError("UNKNOWN_MODULE", f"Unknown module: {module}")
        now = datetime.now(timezone.utc)
        recovery: ModuleDocument | None = None
        if (
            document.workflow_state == ModuleState.VALIDATING
            and document.validation_operation is not None
            and document.validation_operation.lease_expires_at <= now
        ):
            recovery = transition(
                document, "recover_validation", now=now
            )
        elif (
            document.workflow_state == ModuleState.VALIDATED
            and document.last_validation is not None
            and document.last_validation.expires_at <= now
        ):
            recovery = transition(document, "save_draft", now=now)
        if recovery is not None:
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
                return refreshed
        return document

    def _definition(self, module: str | None) -> ModuleDefinition:
        if module is None or module not in MODULE_REGISTRY:
            raise ConfigError("UNKNOWN_MODULE", "A known module is required")
        return MODULE_REGISTRY[module]

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

    def _public_module(
        self, module: str, document: ModuleDocument
    ) -> dict[str, Any]:
        definition = MODULE_REGISTRY[module]
        value = document.model_dump(mode="json")
        if value["draft"] is not None:
            value["draft"] = self._redact_config(
                definition, value["draft"]
            )
        if value["effective"] is not None:
            value["effective"]["config"] = self._redact_config(
                definition, value["effective"]["config"]
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
                        *definition.secret_fields,
                        *(
                            ("password",)
                            if module == "core_admin"
                            else ()
                        ),
                    ],
                },
                "dependencies": list(definition.dependencies),
                "dependents": self._dependents(module),
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
        result = dict(config)
        for field in definition.secret_fields:
            if field in result:
                result[field] = {"configured": True}
        return result

    def _encrypt_secret(
        self,
        plaintext: str,
        *,
        module: str,
        field: str,
        schema_version: int,
    ) -> dict[str, Any]:
        return {
            "$secret": self.crypto.encrypt_field(
                plaintext,
                module=module,
                field_path=field,
                schema_version=schema_version,
            ).model_dump(mode="json")
        }

    def _merge_draft(
        self,
        module: str,
        document: ModuleDocument,
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        definition = MODULE_REGISTRY[module]
        base = dict(
            document.draft
            or (
                document.effective.config
                if document.effective is not None
                else {}
            )
        )
        for field, value in incoming.items():
            value = _strip_whitespace(value)
            if field not in definition.secret_fields:
                base[field] = value
                continue
            if value == {"configured": True}:
                raise ConfigError(
                    "INVALID_SECRET_INPUT",
                    f"{field} placeholder cannot be used as input",
                )
            if value == {"$secret_action": "clear"}:
                base.pop(field, None)
                continue
            if not isinstance(value, str):
                raise ConfigError(
                    "INVALID_SECRET_INPUT",
                    f"{field} must be supplied as plaintext",
                )
            base[field] = self._encrypt_secret(
                value,
                module=module,
                field=field,
                schema_version=document.schema_version,
            )
        return base

    def _decrypt_config(
        self,
        module: str,
        document: ModuleDocument,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        result = dict(config)
        for field in MODULE_REGISTRY[module].secret_fields:
            value = result.get(field)
            if isinstance(value, dict) and "$secret" in value:
                result[field] = self.crypto.decrypt_field(
                    SecretEnvelope.model_validate(value["$secret"]),
                    module=module,
                    field_path=field,
                    schema_version=document.schema_version,
                )
        return result

    async def _effective_configs(self) -> dict[str, dict[str, Any]]:
        documents = await self.repository.list_modules()
        return {
            name: self._decrypt_config(
                name, document, document.effective.config
            )
            for name, document in documents.items()
            if document.effective is not None
            and document.effective.state == ModuleState.ACTIVE
        }

    async def _dependency_revisions(
        self, module: str
    ) -> dict[str, int]:
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
            return await self._result(
                module=command.module, document=document
            )
        documents = await self.repository.list_modules()
        result = await self._result()
        result.modules = [
            self._public_module(name, documents[name])
            for name in MODULE_REGISTRY
        ]
        return result

    async def _execute_save_draft(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        del actor
        module = command.module
        self._definition(module)
        expected = self._require_revision(command)
        document = await self.describe_internal(module)
        draft = self._merge_draft(
            module, document, command.config or {}
        )
        updated = transition(document, "save_draft").model_copy(
            update={"draft": draft}
        )
        try:
            await self.repository.compare_and_set_module(
                module,
                expected_revision=expected,
                document=updated,
            )
        except ConfigConflict as exc:
            raise ConfigError("REVISION_CONFLICT", str(exc)) from exc
        stored = await self.describe_internal(module)
        return await self._result(module=module, document=stored)

    async def _execute_skip(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        del actor
        module = command.module
        definition = self._definition(module)
        if not definition.optional:
            raise ConfigError(
                "MODULE_NOT_OPTIONAL", "Required modules cannot be skipped"
            )
        return await self._transition_and_store(command, "skip")

    async def _execute_reset(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        del actor
        return await self._transition_and_store(command, "reset")

    async def _transition_and_store(
        self, command: ConfigCommand, action: str
    ) -> ConfigResult:
        module = command.module
        self._definition(module)
        expected = self._require_revision(command)
        document = await self.describe_internal(module)
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
        definition = self._definition(module)
        document = await self.describe_internal(module)
        incoming = dict(command.config or {})
        password = (
            incoming.pop("password", None)
            if module == "core_admin"
            else None
        )
        candidate = self._merge_draft(
            module, document, incoming
        )
        plaintext = self._decrypt_config(module, document, candidate)
        checked = validate_module_config(
            module,
            plaintext,
            effective_configs=await self._effective_configs(),
        )
        if module == "core_admin" and (
            not isinstance(password, str) or len(password) < 12
        ):
            checked = ModuleValidationResult(
                valid=False,
                normalized_config=checked.normalized_config,
                error_code="INVALID_ADMIN_PASSWORD",
                message=(
                    "Administrator password must be at least "
                    "12 characters"
                ),
            )
        external_validation: ExternalValidationResult | None = None
        if checked.valid and module == "aliyun_access":
            try:
                external_validation = (
                    await self.external_validator.validate(
                        module,
                        checked.normalized_config,
                    )
                )
            except ExternalValidationError as error:
                checked = ModuleValidationResult(
                    valid=False,
                    normalized_config=checked.normalized_config,
                    error_code=error.code,
                    message=error.message,
                )
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
        return await self._result(
            plan={
                "module": module,
                "valid": checked.valid,
                "error_code": checked.error_code,
                "message": checked.message,
                "config": self._redact_config(
                    definition,
                    checked.normalized_config
                    if checked.valid
                    else plaintext,
                ),
                "dependencies": list(definition.dependencies),
                "writes": False,
                **(
                    {
                        "external_validation":
                            external_validation.as_dict()
                    }
                    if external_validation is not None
                    else {}
                ),
            }
        )

    async def _execute_validate(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        del actor
        module = command.module
        self._definition(module)
        expected = self._require_revision(command)
        document = await self.describe_internal(module)
        if document.draft is None:
            raise ConfigError("DRAFT_REQUIRED", "No draft is available")
        now = datetime.now(timezone.utc)
        operation_id = secrets.token_urlsafe(24)
        validating = transition(
            document, "begin_validation", now=now
        ).model_copy(
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
        plaintext = self._decrypt_config(
            module, validating, validating.draft or {}
        )
        checked = validate_module_config(
            module,
            plaintext,
            effective_configs=await self._effective_configs(),
        )
        if not checked.valid:
            failed = transition(
                validating, "validation_failed"
            ).model_copy(
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
        if module == "aliyun_access":
            try:
                external_validation = (
                    await self.external_validator.validate(
                        module,
                        checked.normalized_config,
                    )
                )
            except ExternalValidationError as error:
                failed = transition(
                    validating, "validation_failed"
                ).model_copy(
                    update={"last_error_code": error.code}
                )
                await self.repository.compare_and_set_module(
                    module,
                    expected_revision=validating.revision,
                    document=failed,
                )
                raise ConfigError(error.code, error.message) from None

        try:
            dependency_revisions = await self._dependency_revisions(module)
        except ConfigError as error:
            failed = transition(
                validating, "validation_failed"
            ).model_copy(
                update={"last_error_code": error.code}
            )
            await self.repository.compare_and_set_module(
                module,
                expected_revision=validating.revision,
                document=failed,
            )
            raise
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
        passed = transition(
            validating, "validation_passed"
        ).model_copy(update={"last_validation": proof})
        await self.repository.compare_and_set_module(
            module,
            expected_revision=validating.revision,
            document=passed,
        )
        stored = await self.describe_internal(module)
        return await self._result(
            module=module,
            document=stored,
            validation={
                "status": "PASSED",
                "validation_id": validation_id,
                "expires_at": proof.expires_at.isoformat(),
                **(
                    {
                        "external_validation":
                            external_validation.as_dict()
                    }
                    if external_validation is not None
                    else {}
                ),
            },
        )

    async def _execute_activate(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        module = command.module
        self._definition(module)
        expected = self._require_revision(command)
        document = await self.describe_internal(module)
        proof = document.last_validation
        validation_id = command.validation_id
        now = datetime.now(timezone.utc)
        stale = (
            document.workflow_state != ModuleState.VALIDATED
            or document.draft is None
            or proof is None
            or validation_id is None
            or proof.validation_id_hash
            != self._token_hash(validation_id)
            or proof.expires_at <= now
            or proof.validated_revision != document.revision
        )
        if not stale:
            plaintext = self._decrypt_config(
                module, document, document.draft
            )
            checked = validate_module_config(
                module,
                plaintext,
                effective_configs=await self._effective_configs(),
            )
            stale = (
                not checked.valid
                or proof.config_digest
                != self.crypto.digest(checked.normalized_config)
                or proof.dependency_revisions
                != await self._dependency_revisions(module)
            )
        if stale:
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
                password = (command.config or {}).get("password")
                if not isinstance(password, str) or len(password) < 12:
                    raise ConfigError(
                        "INVALID_ADMIN_PASSWORD",
                        "Administrator password must be at least 12 characters",
                    )
                if (
                    actor.actor_type != "bootstrap"
                    or actor.credential_hash is None
                ):
                    raise ConfigError(
                        "BOOTSTRAP_REQUIRED",
                        "Bootstrap ownership is required",
                    )
                from server.auth.builtin import hash_password

                await self.repository.activate_core_admin(
                    expected_revision=expected,
                    document=activated,
                    username=str(document.draft["username"]),
                    password_hash=hash_password(password),
                    bootstrap_token_hash=actor.credential_hash,
                )
            elif module == "user_sso":
                await self.repository.activate_with_session_epoch(
                    module,
                    expected_revision=expected,
                    document=activated,
                )
            else:
                await self.repository.compare_and_set_module(
                    module,
                    expected_revision=expected,
                    document=activated,
                )
        except ConfigConflict as exc:
            raise ConfigError("REVISION_CONFLICT", str(exc)) from exc
        stored = await self.describe_internal(module)
        return await self._result(module=module, document=stored)

    async def _execute_disable(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        del actor
        module = command.module
        definition = self._definition(module)
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
                "Disable dependents first: "
                + ", ".join(active_dependents),
            )
        return await self._transition_and_store(command, "disable")

    async def _execute_export(
        self, command: ConfigCommand, actor: ConfigActor
    ) -> ConfigResult:
        del actor
        documents = await self.repository.list_modules()
        selected = (
            [command.module] if command.module is not None else MODULE_REGISTRY
        )
        modules: dict[str, Any] = {}
        for module in selected:
            definition = self._definition(module)
            document = documents[module]
            source = (
                document.draft
                or (
                    document.effective.config
                    if document.effective is not None
                    else {}
                )
            )
            configured = [
                field
                for field in definition.secret_fields
                if field in source
            ]
            modules[module] = {
                "desired_state": (
                    document.desired_state or document.workflow_state
                ).value,
                "config": {
                    key: value
                    for key, value in source.items()
                    if key not in definition.secret_fields
                },
                "metadata": {
                    "configured_secret_fields": configured
                },
            }
        return await self._result(
            export={"protocol_version": 1, "modules": modules}
        )

    @staticmethod
    def _token_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

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
            idempotency_key_hash=self._token_hash(
                command.idempotency_key
            ),
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
            idempotency_key_hash=self._token_hash(
                command.idempotency_key
            ),
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
