import json
import logging

from app.core.logging import JsonFormatter, redact, setup_logging


def format_record(**extra):
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "evt", (), None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return json.loads(JsonFormatter().format(rec))


def test_json_format_has_required_fields():
    out = format_record(request_id="abc")
    assert {"timestamp", "level", "event", "request_id"} <= set(out) and out["event"] == "evt"
    assert out["timestamp"].endswith("+00:00")


def test_sensitive_keys_and_url_credentials_are_redacted():
    out = format_record(
        password="p", api_key="k", Authorization="Bearer x", dsn="postgresql://u:hunter2@h/db", safe="v"
    )
    assert out["password"] == out["api_key"] == out["Authorization"] == "[REDACTED]"
    assert "hunter2" not in out["dsn"] and out["safe"] == "v"
    assert redact("note", "plain") == "plain"


def test_setup_logging_is_idempotent():
    setup_logging("WARNING")
    setup_logging("WARNING")
    assert sum(1 for h in logging.getLogger().handlers if getattr(h, "_atlas", False)) == 1
