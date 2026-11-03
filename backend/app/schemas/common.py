from __future__ import annotations

import re
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int = Field(ge=0)
    limit: int
    offset: int


class ErrorDetail(BaseModel):
    loc: list[str]
    msg: str
    type: str


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def validate_code(value: str, max_length: int) -> str:
    value = value.strip()
    if len(value) > max_length or not CODE_PATTERN.match(value):
        raise ValueError(f"code must match {CODE_PATTERN.pattern} and be at most {max_length} characters")
    return value
