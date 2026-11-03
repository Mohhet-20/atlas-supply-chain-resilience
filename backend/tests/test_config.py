import pytest
from pydantic import ValidationError

from app.core.config import Settings


def make(**kw):
    return Settings(_env_file=None, **kw)


def test_defaults_are_safe_and_local():
    s = make()
    assert s.environment == "development"
    assert s.api_prefix == "/api/v1"
    assert "*" not in s.cors_allowed_origins
    assert s.resolved_database_url.startswith("sqlite:///")
    assert s.demo_data_allowed is True


def test_cors_origins_parse_from_comma_separated_env(monkeypatch):
    monkeypatch.setenv("ATLAS_CORS_ALLOWED_ORIGINS", "http://a.example, http://b.example ,")
    assert make().cors_allowed_origins == ["http://a.example", "http://b.example"]


def test_production_rejects_wildcard_cors():
    with pytest.raises(ValidationError):
        make(environment="production", cors_allowed_origins=["*"])


def test_production_requires_explicit_origins():
    with pytest.raises(ValidationError):
        make(environment="production", cors_allowed_origins=[])


def test_demo_data_disabled_by_default_in_production_but_overridable():
    assert make(environment="production", cors_allowed_origins=["https://x.example"]).demo_data_allowed is False
    assert make(
        environment="production", cors_allowed_origins=["https://x.example"], demo_data_enabled=True
    ).demo_data_allowed


@pytest.mark.parametrize("prefix", ["api", "/api/", "/a b"])
def test_invalid_api_prefix_rejected(prefix):
    with pytest.raises(ValidationError):
        make(api_prefix=prefix)


def test_request_size_bounds():
    with pytest.raises(ValidationError):
        make(max_request_bytes=10)


def test_safe_database_url_masks_password():
    s = make(database_url="postgresql://user:hunter2@db.example/atlas")
    assert "hunter2" not in s.safe_database_url
    assert "hunter2" not in repr(s.safe_database_url)


def test_invalid_log_level_rejected():
    with pytest.raises(ValidationError):
        make(log_level="LOUD")
