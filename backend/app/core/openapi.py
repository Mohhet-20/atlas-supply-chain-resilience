"""OpenAPI generation that documents authentication and the access each operation requires.

The access requirement is read from the route's real dependency tree (``require`` / ``require_permission``
closures), so the published schema cannot drift from what the endpoint actually enforces.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

# No bearer credential: health probes, login, one-time-token password reset. Inbound webhooks are
# authenticated by an HMAC signature instead (documented in the operation description).
PUBLIC_PATHS = ("/health", "/readiness", "/auth/login", "/auth/password-reset/request", "/auth/password-reset/confirm")
SIGNED_PREFIX = "/webhooks/inbound/"


def _requirement(call: Any) -> str | None:
    """Return 'role:<name>' or 'permission:<value>' for a dependency produced by the auth factories."""
    qual = getattr(call, "__qualname__", "")
    if not qual.endswith("._dependency"):
        return None
    cells = {n: c.cell_contents for n, c in zip(call.__code__.co_freevars, call.__closure__ or (), strict=False)}
    if qual.startswith("require_permission."):
        perm = cells.get("permission")
        return f"permission:{getattr(perm, 'value', perm)}"
    if qual.startswith("require."):
        role = cells.get("role")
        return f"role:{getattr(role, 'name', str(role)).lower()}"
    return None


def _collect(dependant: Any, found: list[str]) -> None:
    req = _requirement(dependant.call)
    if req:
        found.append(req)
    for sub in dependant.dependencies:
        _collect(sub, found)


def _flatten(routes: Any, prefix: str = ""):
    """Yield (full_path, APIRoute, extra_dependency_calls), descending into lazily included routers."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield prefix + r.path, r, []
        elif hasattr(r, "include_context") and hasattr(r, "original_router"):
            ctx = r.include_context
            extra = [d.dependency for d in (ctx.dependencies or []) if getattr(d, "dependency", None)]
            for path, route, more in _flatten(r.original_router.routes, prefix + (ctx.prefix or "")):
                yield path, route, more + extra
        elif hasattr(r, "routes"):
            yield from _flatten(r.routes, prefix + getattr(r, "path", ""))


def install_openapi(app: FastAPI) -> None:
    def build() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        prefix = app.state.settings.api_prefix
        schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
        schema.setdefault("components", {})["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "Opaque credential: a session token (atl_s_...), an API key (atl_k_...) "
                "or a configured static token. Send as 'Authorization: Bearer <credential>'.",
            }
        }
        required: dict[tuple[str, str], list[str]] = {}
        for path, route, extra in _flatten(app.routes):
            found: list[str] = [q for q in (_requirement(c) for c in extra) if q]
            _collect(route.dependant, found)
            for method in route.methods or ():
                required[(path, method.lower())] = found
        for path, ops in schema["paths"].items():
            public = any(path == f"{prefix}{p}" for p in PUBLIC_PATHS)
            for method, op in ops.items():
                if not isinstance(op, dict):
                    continue
                if public:
                    op["security"] = []
                    continue
                if path.startswith(f"{prefix}{SIGNED_PREFIX}"):
                    op["security"] = []
                    op["description"] = (op.get("description") or "").rstrip() + (
                        "\n\n**Authentication:** HMAC signature header (no bearer credential)."
                    )
                    continue
                op["security"] = [{"bearerAuth": []}]
                reqs = sorted(set(required.get((path, method), [])))
                if reqs:
                    op["x-required-access"] = reqs
                    op["description"] = (op.get("description") or "").rstrip() + (
                        "\n\n**Requires:** " + ", ".join(f"`{r}`" for r in reqs)
                    )
        app.openapi_schema = schema
        return schema

    app.openapi = build  # type: ignore[method-assign]
