from __future__ import annotations

import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ALIYUN_ACCESS_SCHEMA_VERSION = 2
CredentialMode = Literal["direct_ak", "assume_role", "ecs_ram_role"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DirectAKConfig(_StrictModel):
    access_key_id: str = Field(min_length=1)
    access_key_secret: str = Field(min_length=1)


class AssumeRoleConfig(_StrictModel):
    source_access_key_id: str = Field(min_length=1)
    source_access_key_secret: str = Field(min_length=1)
    role_arn: str = Field(
        pattern=(
            r"^acs:ram::[0-9]{1,32}:role/"
            r"[A-Za-z0-9.@_/-]{1,128}$"
        )
    )
    role_session_name: str = Field(pattern=r"^[A-Za-z0-9+=,.@_-]{2,64}$")
    duration_seconds: int = Field(default=3600, ge=900, le=43200)
    external_id: str | None = Field(default=None, min_length=2, max_length=1224)


class ECSRamRoleConfig(_StrictModel):
    role_name: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9.@_-]{1,64}$"
    )
    metadata_policy: Literal["v2_only"] = "v2_only"


class AliyunAccessConfig(_StrictModel):
    credential_mode: CredentialMode
    region_id: str = Field(default="cn-hangzhou", min_length=1)
    openapi_network: Literal["public", "vpc"] = "public"
    direct_ak: DirectAKConfig | None = None
    assume_role: AssumeRoleConfig | None = None
    ecs_ram_role: ECSRamRoleConfig | None = None

    @model_validator(mode="after")
    def selected_mode_has_configuration(self) -> "AliyunAccessConfig":
        if getattr(self, self.credential_mode) is None:
            raise ValueError(
                "The selected credential mode requires its configuration"
            )
        if self.credential_mode == "ecs_ram_role" and (
            self.direct_ak is not None or self.assume_role is not None
        ):
            raise ValueError(
                "ECS RAM role mode cannot include access key credentials"
            )
        return self


class CredentialTransition(_StrictModel):
    previous_mode_action: Literal["clear", "retain"] = "clear"
    selected_mode_action: Literal["replace", "reuse_retained"] = "replace"
    reuse_direct_ak_as_assume_source: bool = False
    delete_retained_modes: tuple[CredentialMode, ...] = ()


class AliyunAccessMutation(_StrictModel):
    credential_mode: CredentialMode
    region_id: str | None = Field(default=None, min_length=1)
    openapi_network: Literal["public", "vpc"] | None = None
    direct_ak: DirectAKConfig | None = None
    assume_role: AssumeRoleConfig | None = None
    ecs_ram_role: ECSRamRoleConfig | None = None
    transition: CredentialTransition | None = None

    @model_validator(mode="after")
    def selected_mode_matches_mutation(self) -> "AliyunAccessMutation":
        reusing_retained = (
            self.transition is not None
            and self.transition.selected_mode_action == "reuse_retained"
        )
        if not reusing_retained and getattr(self, self.credential_mode) is None:
            raise ValueError(
                "The selected credential mode requires its configuration"
            )
        if self.credential_mode == "ecs_ram_role" and (
            self.direct_ak is not None or self.assume_role is not None
        ):
            raise ValueError(
                "ECS RAM role mode cannot include access key credentials"
            )
        return self


def generate_role_session_name() -> str:
    return f"polardb-agentic-{secrets.token_hex(4)}"
