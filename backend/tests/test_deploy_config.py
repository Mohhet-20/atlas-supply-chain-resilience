"""Deployment: production/staging configuration fails fast; the validator script and smoke test behave as documented."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GOOD = {
    "environment": "production",
    "cors_allowed_origins": "https://atlas.example.test",
    "database_url": "sqlite:///x.db",
}


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def make(**over):
    return Settings(_env_file=None, **(GOOD | over))


@pytest.mark.parametrize("env", ["production", "staging"])
def test_production_like_environments_accept_a_safe_configuration(env):
    s = make(environment=env)
    assert s.production_like


@pytest.mark.parametrize(
    "over, text",
    [
        ({"cors_allowed_origins": "*"}, "Wildcard"),
        ({"cors_allowed_origins": ""}, "CORS"),
        ({"cors_allowed_origins": "http://atlas.example.test"}, "https://"),
        ({"security_headers_enabled": False}, "SECURITY_HEADERS"),
        ({"log_level": "DEBUG"}, "DEBUG"),
        ({"integrations_master_key": "too-short"}, "32 characters"),
    ],
)
def test_unsafe_production_settings_are_rejected_with_clear_messages(over, text):
    with pytest.raises(ValidationError) as e:
        make(**over)
    assert text in str(e.value)
    assert "too-short" not in str(e.value)  # the rejected secret is never echoed


def test_staging_is_not_treated_as_development():
    with pytest.raises(ValidationError):
        make(environment="staging", cors_allowed_origins="*")
    assert make(environment="staging").write_api_allowed is True  # staging uses synthetic data, so writes stay on
    assert make(environment="production").write_api_allowed is False  # production default stays closed


def test_development_defaults_unchanged():
    s = Settings(_env_file=None, environment="development")
    assert not s.production_like and s.demo_data_allowed


def test_validate_config_script_reports_errors_and_warnings_without_secrets():
    v = load("validate_config")
    errs, warns = v.check(
        {
            "ATLAS_ENVIRONMENT": "production",
            "ATLAS_CORS_ALLOWED_ORIGINS": "*",
            "ATLAS_INTEGRATIONS_MASTER_KEY": "secret-value-xyz",
        }
    )
    assert errs and not any("secret-value-xyz" in e for e in errs)
    errs, warns = v.check({"ATLAS_ENVIRONMENT": "production", "ATLAS_CORS_ALLOWED_ORIGINS": "https://a.example.test"})
    assert not errs and any("SQLite" in w for w in warns) and any("MASTER_KEY" in w for w in warns)
    errs, warns = v.check({"ATLAS_ENVIRONMENT": "development"})
    assert not errs and any("production rules not applied" in w for w in warns)


def test_smoke_test_fails_when_nothing_is_listening_and_never_counts_skips_as_passes():
    smoke = load("smoke_test")
    results = smoke.run("http://127.0.0.1:9", None, None)  # port 9 (discard) refuses connections
    assert [s for _, s, _ in results[:4]] == ["FAIL"] * 4
    assert all(s == "SKIP" for _, s, _ in results[4:])
    assert smoke.main(["--base-url", "http://127.0.0.1:9"]) == 1
