from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    app: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, str]
