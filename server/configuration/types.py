from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModuleState(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"
    DISABLED = "DISABLED"
    SKIPPED = "SKIPPED"


class SystemState(StrEnum):
    SETUP = "SETUP"
    READY = "READY"


class ConfigAction(StrEnum):
    DESCRIBE = "describe"
    SAVE_DRAFT = "save_draft"
    PLAN = "plan"
    VALIDATE = "validate"
    ACTIVATE = "activate"
    SKIP = "skip"
    DISABLE = "disable"
    RESET = "reset"
    EXPORT = "export"


class EffectiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    state: ModuleState
    config: dict[str, Any]


class ValidationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    started_at: datetime
    lease_expires_at: datetime


class ValidationProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["PASSED"]
    checked_at: datetime
    expires_at: datetime
    validation_id_hash: str
    validated_revision: int
    config_digest: str
    dependency_revisions: dict[str, int]
    validator_set_version: int = 1
    message: str = "Configuration is valid"


class ModuleDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    revision: int = Field(default=0, ge=0)
    workflow_state: ModuleState
    initial_state: ModuleState
    desired_state: ModuleState | None = None
    draft: dict[str, Any] | None = None
    effective: EffectiveConfig | None = None
    last_validation: ValidationProof | None = None
    validation_operation: ValidationOperation | None = None
    last_error_code: str | None = None


class ConfigActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str
    actor_type: Literal["bootstrap", "admin", "system"]
    credential_hash: str | None = None


class ConfigCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1] = 1
    action: ConfigAction
    module: str | None = None
    expected_revision: int | None = None
    idempotency_key: str | None = None
    validation_id: str | None = None
    confirm_impact: bool = False
    config: dict[str, Any] | None = None


class ConfigResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_version: int
    system_state: SystemState
    module: dict[str, Any] | None = None
    modules: list[dict[str, Any]] | None = None
    validation: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    export: dict[str, Any] | None = None


class ConfigError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConfigStateError(ConfigError):
    def __init__(self, message: str) -> None:
        super().__init__("INVALID_STATE_TRANSITION", message)


def _next(
    document: ModuleDocument,
    state: ModuleState,
    **updates: Any,
) -> ModuleDocument:
    return document.model_copy(
        update={
            "revision": document.revision + 1,
            "workflow_state": state,
            **updates,
        }
    )


def transition(
    document: ModuleDocument,
    action: str,
    *,
    now: datetime | None = None,
) -> ModuleDocument:
    state = document.workflow_state
    current_time = now or datetime.now(timezone.utc)

    if action in {"save_draft", "edit"} and state in {
        ModuleState.NOT_CONFIGURED,
        ModuleState.SKIPPED,
        ModuleState.DRAFT,
        ModuleState.VALIDATED,
        ModuleState.ACTIVE,
        ModuleState.DISABLED,
        ModuleState.ERROR,
    }:
        return _next(
            document,
            ModuleState.DRAFT,
            last_validation=None,
            validation_operation=None,
            last_error_code=None,
        )
    if action == "begin_validation" and state in {
        ModuleState.DRAFT,
        ModuleState.ERROR,
    }:
        return _next(document, ModuleState.VALIDATING)
    if action == "validation_passed" and state == ModuleState.VALIDATING:
        return _next(
            document,
            ModuleState.VALIDATED,
            validation_operation=None,
            last_error_code=None,
        )
    if action == "validation_failed" and state == ModuleState.VALIDATING:
        return _next(
            document,
            ModuleState.ERROR,
            validation_operation=None,
        )
    if action == "activate" and state == ModuleState.VALIDATED:
        return _next(document, ModuleState.ACTIVE)
    if action == "disable" and state == ModuleState.ACTIVE:
        effective = document.effective
        if effective is not None:
            effective = effective.model_copy(
                update={"state": ModuleState.DISABLED}
            )
        return _next(
            document,
            ModuleState.DISABLED,
            effective=effective,
            draft=None,
            last_validation=None,
        )
    if action == "skip":
        if document.effective is not None:
            raise ConfigStateError(
                "cannot skip a module with an effective revision"
            )
        if state in {
            ModuleState.NOT_CONFIGURED,
            ModuleState.DRAFT,
            ModuleState.VALIDATED,
            ModuleState.ERROR,
            ModuleState.SKIPPED,
        }:
            return _next(
                document,
                ModuleState.SKIPPED,
                draft=None,
                last_validation=None,
                validation_operation=None,
            )
    if action == "reset":
        if state == ModuleState.VALIDATING:
            raise ConfigStateError(
                "cannot reset while validation is running"
            )
        if state in {
            ModuleState.DRAFT,
            ModuleState.VALIDATED,
            ModuleState.ERROR,
        }:
            target = (
                document.effective.state
                if document.effective is not None
                else document.initial_state
            )
            return _next(
                document,
                target,
                draft=None,
                last_validation=None,
                validation_operation=None,
                last_error_code=None,
            )
    if action == "recover_validation" and state == ModuleState.VALIDATING:
        operation = document.validation_operation
        if operation is None or operation.lease_expires_at > current_time:
            raise ConfigStateError(
                "validation lease has not expired"
            )
        return _next(
            document,
            ModuleState.DRAFT,
            validation_operation=None,
            last_error_code="VALIDATION_INTERRUPTED",
        )
    raise ConfigStateError(
        f"action '{action}' is not allowed from state '{state.value}'"
    )
