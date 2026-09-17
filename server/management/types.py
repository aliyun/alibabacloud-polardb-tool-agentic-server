from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


class ManagedTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(gt=0)


class ManagedActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["managed_initializer"]
    subject_id: str | None = Field(default=None, max_length=255)


class ManagedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module: Literal["core_admin"]
    action: Literal[
        "describe",
        "save_draft",
        "validate",
        "activate",
        "set_initial_password",
    ]
    parameter: Literal["username", "password"] | None = None
    value: Any | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    validation_id: str | None = None
    idempotency_key: str | None = None

    @model_validator(mode="after")
    def validate_single_parameter(self) -> ManagedCommand:
        if isinstance(self.value, (dict, list, tuple, set)):
            raise PydanticCustomError(
                "multiple_parameters_not_supported",
                "MULTIPLE_PARAMETERS_NOT_SUPPORTED",
            )
        expected_parameter = {
            "save_draft": "username",
            "set_initial_password": "password",
        }.get(self.action)
        if expected_parameter is None:
            allowed = self.parameter is None and self.value is None
        else:
            allowed = (
                self.parameter == expected_parameter
                and isinstance(self.value, str)
                and bool(self.value)
            )
        if not allowed:
            raise PydanticCustomError(
                "config_operation_not_allowed",
                "CONFIG_OPERATION_NOT_ALLOWED",
            )
        return self


class ManagedEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1] = 1
    target: ManagedTarget
    actor: ManagedActor
    command: ManagedCommand


class ManagementStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1] = 1
    runtime_phase: str
    pas_state: str
    schema_status: str
    config_status: str
    desired_config_version: int | None
    loaded_config_version: int | None
    instance_id: str | None
    instance_generation: int | None
    credential_state: str | None
