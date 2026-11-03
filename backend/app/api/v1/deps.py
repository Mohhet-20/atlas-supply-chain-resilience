from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import Role, require
from app.db.session import get_db

DbSession = Annotated[Session, Depends(get_db)]
Limit = Annotated[int, Query(ge=1, le=100, description="Page size (1-100).")]
Offset = Annotated[int, Query(ge=0, le=1_000_000, description="Rows to skip.")]
SearchText = Annotated[str | None, Query(max_length=100, description="Case-insensitive name/code search.")]


# Route-level role gates for routers that predate the central auth dependency.
VIEW = [Depends(require(Role.VIEWER))]
EDIT = [Depends(require(Role.EDITOR))]
REVIEW = [Depends(require(Role.REVIEWER))]
