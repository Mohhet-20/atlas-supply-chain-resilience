"""SYNTHETIC helpers for alerting tests: a scripted fake notification provider and small builders. No real recipient or network is ever contacted."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.alerting import notify as N
from app.alerting import service as S
from app.alerting.config import PolicyBody, RuleBody

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
DIRECTORY = {"viewer-user": 1, "editor-user": 2, "reviewer-user": 3, "admin-user": 4}


class FakeProvider:
    """Records every send. ``script`` is a list of outcomes consumed in order; when empty every send is accepted."""

    def __init__(self, script: list[str] | None = None) -> None:
        self.script = list(script or [])
        self.sent: list[tuple[str, bytes, str]] = []

    def send(self, url: str, body: bytes, timeout: float, idempotency_key: str) -> N.SendResult:
        self.sent.append((url, body, idempotency_key))
        kind = self.script.pop(0) if self.script else "accepted"
        return N.SendResult(kind, f"scripted {kind}", "ref-1" if kind == "accepted" else None)


def ctx(provider: FakeProvider | None = None, **kw: Any) -> S.Ctx:
    base: dict[str, Any] = {
        "directory": dict(DIRECTORY),
        "provider": provider or FakeProvider(),
        "max_attempts": 3,
        "retry_base": 10,
    }
    return S.Ctx(**(base | kw))


def rule_body(**kw: Any) -> RuleBody:
    d: dict[str, Any] = {
        "name": "High metric", "detector": "event_threshold", "severity": "HIGH",
        "params": {"metric": "temp", "operator": "gte", "threshold": 50, "unit": "C"},
        "notify": {"channels": ["in_app"], "recipients": ["editor-user"], "min_severity": "LOW"},
    }  # fmt: skip
    return RuleBody.model_validate(d | kw)


def policy_body(**kw: Any) -> PolicyBody:
    d: dict[str, Any] = {
        "name": "Standard",
        "ack_minutes": 10,
        "resolve_minutes": 120,
        "escalate_after_minutes": 10,
        "targets": ["reviewer-user", "admin-user"],
    }
    return PolicyBody.model_validate(d | kw)


def event(key: str = "k1", value: float = 60, at: datetime = T0, **kw: Any) -> S.EventBody:
    d: dict[str, Any] = {
        "event_type": "metric.observation", "source": "sensor-feed", "idempotency_key": key, "occurred_at": at,
        "payload": {"metric": "temp", "value": value, "unit": "C"}, "entities": [{"entity_type": "facility", "entity_id": "F-1"}],
    }  # fmt: skip
    return S.EventBody.model_validate(d | kw)
