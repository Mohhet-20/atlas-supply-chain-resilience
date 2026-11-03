"""Pure-ASGI middleware: request IDs, payload-size guard, and access logging."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("atlas.access")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    def __init__(
        self, app: ASGIApp, max_request_bytes: int, upload_prefix: str = "", max_upload_bytes: int | None = None
    ) -> None:
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.upload_prefix = upload_prefix
        self.max_upload_bytes = max_upload_bytes or max_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        incoming = headers.get("x-request-id", "")
        request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        start = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        declared = headers.get("content-length", "")
        limit = (
            self.max_upload_bytes
            if self.upload_prefix and scope["path"].startswith(self.upload_prefix)
            else self.max_request_bytes
        )
        if declared.isdigit() and int(declared) > limit:
            body = json.dumps(
                {"error": {"code": "payload_too_large", "message": "Request body too large.", "request_id": request_id}}
            ).encode()
            await send_wrapper(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                }
            )
            await send_wrapper({"type": "http.response.body", "body": body})
        else:
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                logger.info(
                    "request_completed",
                    extra={
                        "request_id": request_id,
                        "method": scope["method"],
                        "path": scope["path"],
                        "status": status_holder["status"],
                        "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                    },
                )
