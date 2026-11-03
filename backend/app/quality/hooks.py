"""Best-effort re-evaluation after edits and imports. A failure here never fails the user's write."""

from __future__ import annotations

import logging

from fastapi import Request

from app.db.tenancy import request_session_factory
from app.models.enums import EntityKind, EvalScope
from app.quality import engine
from app.registry.config import quality_config

logger = logging.getLogger("atlas.quality")


def _enabled(request: Request) -> bool:
    return bool(request.app.state.settings.quality_auto_reevaluate)


def reevaluate_entity(request: Request, kind: EntityKind, entity_id: int, actor: str) -> None:
    if not _enabled(request):
        return
    s = request.app.state.settings
    try:
        engine.evaluate_now(
            request_session_factory(request),
            EvalScope.ENTITY,
            {"entity_type": kind.value, "entity_id": entity_id},
            f"auto:{actor}"[:64],
            quality_config(s.quality_config_path),
            trigger="AUTO",
            limits=engine.limits_from(s),
        )
    except Exception:  # noqa: BLE001 - includes "already running"; the next scheduled evaluation will catch up
        logger.warning("quality re-evaluation skipped", extra={"kind": kind.value, "id": entity_id}, exc_info=False)


def reevaluate_batch(request: Request, batch_id: int, actor: str) -> None:
    if not _enabled(request):
        return
    s = request.app.state.settings
    try:
        engine.evaluate_now(
            request_session_factory(request),
            EvalScope.BATCH,
            {"batch_id": batch_id},
            f"auto:{actor}"[:64],
            quality_config(s.quality_config_path),
            trigger="IMPORT",
            limits=engine.limits_from(s),
        )
    except Exception:  # noqa: BLE001
        logger.warning("quality batch evaluation skipped", extra={"batch": batch_id}, exc_info=False)


def reevaluate_edge(request: Request, ref: str, actor: str) -> None:
    if not _enabled(request):
        return
    s = request.app.state.settings
    try:
        engine.evaluate_now(
            request_session_factory(request),
            EvalScope.RELATIONSHIPS,
            {"refs": [ref]},
            f"auto:{actor}"[:64],
            quality_config(s.quality_config_path),
            trigger="AUTO",
            limits=engine.limits_from(s),
        )
    except Exception:  # noqa: BLE001
        logger.warning("quality relationship evaluation skipped", extra={"ref": ref}, exc_info=False)
