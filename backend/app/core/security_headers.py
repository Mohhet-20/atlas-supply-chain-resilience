"""Pure-ASGI middleware adding security headers to every API response, and a streaming request-size guard.

The size guard matters because ``Content-Length`` can be absent (chunked transfer) or wrong; the declared-length check in
RequestContextMiddleware alone does not bound a chunked body.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

DOC_PATHS = ("/docs", "/openapi.json", "/docs/oauth2-redirect")


class SecurityHeadersMiddleware:
    def __init__(
        self, app: ASGIApp, *, hsts: bool, max_bytes: int, upload_prefix: str = "", max_upload_bytes: int | None = None
    ) -> None:
        self.app, self.hsts, self.max_bytes = app, hsts, max_bytes
        self.upload_prefix, self.max_upload_bytes = upload_prefix, max_upload_bytes or max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        docs = path in DOC_PATHS
        limit = self.max_upload_bytes if self.upload_prefix and path.startswith(self.upload_prefix) else self.max_bytes
        seen = 0
        started = False
        exceeded = False
        too_large_body = json.dumps(
            {"error": {"code": "payload_too_large", "message": "Request body too large.", "request_id": None}}
        ).encode()

        async def counting_receive() -> Message:
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    exceeded = True
                    raise _TooLarge()
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if exceeded:  # FastAPI turns a body-read exception into a 400; the client must see 413 either way
                if message["type"] == "http.response.start":
                    started = True
                    message = {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(too_large_body)).encode()),
                        ],
                    }
                elif message["type"] == "http.response.body":
                    message = {"type": "http.response.body", "body": too_large_body}
            if message["type"] == "http.response.start":
                started = True
                h = message.setdefault("headers", [])
                present = {k.lower() for k, _ in h}
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"no-referrer",
                    b"cross-origin-resource-policy": b"same-origin",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
                }
                if not docs:  # the interactive docs need inline scripts; the API itself never serves HTML
                    extra[b"content-security-policy"] = b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
                    extra[b"cache-control"] = b"no-store"
                if self.hsts:
                    extra[b"strict-transport-security"] = b"max-age=31536000; includeSubDomains"
                for k, v in extra.items():
                    if k not in present:
                        h.append((k, v))
            await send(message)

        try:
            await self.app(scope, counting_receive, send_wrapper)
        except _TooLarge:
            if started:
                raise
            body = too_large_body
            await send_wrapper(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                }
            )
            await send_wrapper({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass
