from __future__ import annotations

import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


class InstanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    scheme: Literal["http", "https"]
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535, strict=True)
    username: str = Field(min_length=1, max_length=1024)
    password: str = Field(min_length=1, max_length=4096)
    tls_verify: bool = True
    ca_bundle: str | None = Field(default=None, max_length=262144)

    @field_validator("name", "username")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("value must not be blank")
        return candidate

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        candidate = value.strip()
        if not _HOST_RE.fullmatch(candidate) or "/" in candidate or "@" in candidate:
            raise ValueError("host must not contain a URL or credentials")
        return candidate
