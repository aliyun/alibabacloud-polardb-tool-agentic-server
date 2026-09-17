from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from server.management.types import ManagedTarget
from server.models import PasswordState


class ManagedAccountOperation(StrEnum):
    MODIFY = "MODIFY"
    RESET = "RESET"


class ManagedAccountActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["control_user"]
    subject_id: str = Field(min_length=1, max_length=255)


class ManagedAccountPasswordEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    protocol_version: Literal[1]
    target: ManagedTarget
    actor: ManagedAccountActor
    operation: ManagedAccountOperation
    old_password: str | None = Field(default=None, repr=False)
    new_password: str = Field(min_length=1, repr=False)
    idempotency_key: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_password_parameters(self) -> ManagedAccountPasswordEnvelope:
        valid = (self.operation is ManagedAccountOperation.RESET and self.old_password is None) or (
            self.operation is ManagedAccountOperation.MODIFY
            and isinstance(self.old_password, str)
            and bool(self.old_password)
        )
        if not valid:
            raise PydanticCustomError(
                "invalid_account_password_parameters",
                "INVALID_ACCOUNT_PASSWORD",
            )
        return self


class ManagedAccount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account_name: Literal["admin"] = "admin"
    account_status: Literal["ACTIVE"] = "ACTIVE"
    password_status: PasswordState


class ManagedAccountMutationResult(ManagedAccount):
    pass
