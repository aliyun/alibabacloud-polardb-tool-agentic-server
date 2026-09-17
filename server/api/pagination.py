from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
