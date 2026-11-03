"""Structured JSON logging with secret redaction and request-correlation support."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

_SENSITIVE = re.compile(r"(password|passwd|secret|token|authorization|api[_-]?key|credential|cookie)", re.I)
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}
_URL_CREDENTIALS = re.compile(r"(\w+://[^:/\s]+:)[^@\s]+(@)")


def redact(key: str, value: object) -> object:
    if _SENSITIVE.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return _URL_CREDENTIALS.sub(r"\1[REDACTED]\2", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": redact("event", record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = redact(key, value)
        if record.exc_info and record.exc_info[0] is not None:
            # Exception type only: messages may embed sensitive values.
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_atlas", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler._atlas = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
