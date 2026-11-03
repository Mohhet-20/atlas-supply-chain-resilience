"""Domain exceptions and consistent API error responses."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("atlas.errors")


class AtlasError(Exception):
    """Base class for expected, user-presentable errors."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AtlasError):
    status_code = 404
    code = "not_found"


class ConflictError(AtlasError):
    status_code = 409
    code = "conflict"


class StalePreviewError(ConflictError):
    code = "stale_preview"


class PayloadTooLargeError(AtlasError):
    status_code = 413
    code = "payload_too_large"


class WriteDisabledError(AtlasError):
    status_code = 403
    code = "write_api_disabled"


class UnsupportedMediaError(AtlasError):
    status_code = 415
    code = "unsupported_media_type"


class DomainValidationError(AtlasError):
    status_code = 422
    code = "domain_validation_error"


class UnauthenticatedError(AtlasError):
    status_code = 401
    code = "unauthenticated"


class ForbiddenError(AtlasError):
    status_code = 403
    code = "forbidden"


class TooManyRequestsError(AtlasError):
    status_code = 429
    code = "rate_limited"
    retry_after = 60

    def __init__(self, message: str, retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class AuthNotConfiguredError(AtlasError):
    status_code = 503
    code = "auth_not_configured"


class VersionConflictError(ConflictError):
    code = "version_conflict"


class EntityMergedError(ConflictError):
    code = "entity_merged"


class MergeBlockedError(AtlasError):
    status_code = 422
    code = "merge_blocked"


class DuplicateRelationshipError(ConflictError):
    code = "duplicate_relationship"


class InvalidTransitionError(ConflictError):
    code = "invalid_status_transition"


class ReadOnlyRelationshipError(AtlasError):
    status_code = 422
    code = "read_only_relationship"


class CompensationUnsafeError(ConflictError):
    code = "compensation_unsafe"


class SchemaNotMigratedError(AtlasError):
    """The database schema is missing or not at the expected Alembic revision."""

    status_code = 503
    code = "schema_not_migrated"


def _request_id(request: Request) -> str | None:
    state = request.scope.get("state") or {}
    return state.get("request_id")


def error_response(
    request: Request, status: int, code: str, message: str, details: list[dict] | None = None
) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message, "request_id": _request_id(request)}}
    if details:
        body["error"]["details"] = details
    headers = {"X-Request-ID": rid} if (rid := _request_id(request)) else None
    return JSONResponse(status_code=status, content=body, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AtlasError)
    async def _atlas_error(request: Request, exc: AtlasError) -> JSONResponse:
        response = error_response(request, exc.status_code, exc.code, exc.message)
        if exc.status_code == 401:
            response.headers["WWW-Authenticate"] = "Bearer"
        if isinstance(exc, TooManyRequestsError):
            response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(StaleDataError)
    async def _stale(request: Request, exc: StaleDataError) -> JSONResponse:
        return error_response(
            request, 409, "version_conflict", "The record was changed concurrently. Reload and try again."
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError) -> JSONResponse:
        logger.warning("integrity_error", extra={"path": request.url.path})  # no SQL or values are logged
        return error_response(request, 409, "conflict", "The change conflicts with existing data.")

    @app.exception_handler(OperationalError)
    async def _operational(request: Request, exc: OperationalError) -> JSONResponse:
        # SQLite allows one writer; a writer that waited the full busy timeout is told to retry rather than shown a 500.
        if "database is locked" in str(exc.orig):
            logger.warning("database_busy", extra={"path": request.url.path})
            response = error_response(request, 503, "database_busy", "The database is busy. Retry shortly.")
            response.headers["Retry-After"] = "1"
            return response
        logger.error("database_error", extra={"error_type": type(exc).__name__, "path": request.url.path}, exc_info=exc)
        return error_response(request, 500, "internal_error", "An internal error occurred.")

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo submitted input values back; only location, message and error type.
        details = [
            {"loc": [str(p) for p in err.get("loc", ())], "msg": err.get("msg", ""), "type": err.get("type", "")}
            for err in exc.errors()
        ]
        return error_response(request, 422, "validation_error", "Request validation failed.", details)

    @app.exception_handler(PydanticValidationError)
    async def _model_validation_error(request: Request, exc: PydanticValidationError) -> JSONResponse:
        # Models validated inside handlers (entity-type specific bodies) get the same sanitized 422 envelope.
        details = [
            {"loc": [str(p) for p in err.get("loc", ())], "msg": err.get("msg", ""), "type": err.get("type", "")}
            for err in exc.errors(include_input=False, include_url=False)
        ]
        return error_response(request, 422, "validation_error", "Request validation failed.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        message = "Resource not found." if exc.status_code == 404 else str(exc.detail)
        return error_response(request, exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_exception",
            extra={"error_type": type(exc).__name__, "path": request.url.path},
            exc_info=exc,
        )
        return error_response(request, 500, "internal_error", "An internal error occurred.")
