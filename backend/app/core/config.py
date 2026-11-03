"""Typed application settings (environment variables prefixed ``ATLAS_`` or a ``.env`` file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

Environment = Literal["development", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """All runtime configuration lives here; nothing else reads the environment."""

    model_config = SettingsConfigDict(
        env_prefix="ATLAS_",
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,  # a failed validation must never print configured secret values
    )

    app_name: str = Field(default="ATLAS", min_length=1, max_length=100)
    environment: Environment = "development"
    api_prefix: str = "/api/v1"
    database_url: str | None = None
    data_dir: Path = BACKEND_DIR / "var"
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )
    log_level: LogLevel = "INFO"
    max_request_bytes: int = Field(default=1_048_576, ge=1_024, le=104_857_600)
    demo_data_enabled: bool | None = None
    write_api_enabled: bool | None = None
    max_upload_bytes: int = Field(default=5_242_880, ge=1_024, le=52_428_800)
    max_ingest_rows: int = Field(default=5_000, ge=1, le=50_000)
    demo_seed_path: Path = REPO_ROOT / "data" / "demo" / "demo_seed.json"
    # "token:actor:role,token:actor:role"; roles: viewer < editor < reviewer < admin. Tokens >= 16 chars.
    api_tokens: SecretStr | None = None
    resolution_config_path: Path | None = None
    quality_config_path: Path | None = None
    # Evaluation bounds (a run over more subjects, or longer than the budget, finishes PARTIAL and says so)
    quality_eval_max_subjects: int = Field(default=5_000, ge=1, le=100_000)
    quality_eval_time_budget_seconds: float = Field(default=20.0, gt=0, le=600)
    quality_auto_reevaluate: bool = True  # re-evaluate one record after it is edited or imported (bounded, best effort)
    # Supplier risk
    risk_config_path: Path | None = None  # optional methodology override; must carry a new version label
    risk_run_max_suppliers: int = Field(default=2_000, ge=1, le=50_000)
    risk_run_time_budget_seconds: float = Field(default=20.0, gt=0, le=600)
    risk_auto_reassess: bool = True  # targeted reassessment of the suppliers a relationship or registry edit affects
    # Disruption scenarios (server-enforced; a request can never raise these)
    scenario_max_events: int = Field(default=25, ge=1, le=100)
    scenario_max_targets_per_event: int = Field(default=50, ge=1, le=500)
    scenario_max_depth: int = Field(default=4, ge=1, le=6)
    scenario_max_nodes: int = Field(default=500, ge=10, le=2_000)
    scenario_max_edges: int = Field(default=1_000, ge=10, le=5_000)
    scenario_time_budget_seconds: float = Field(default=20.0, gt=0, le=600)
    scenario_max_active_runs: int = Field(default=3, ge=1, le=20)
    scenario_max_scenarios: int = Field(default=1_000, ge=1, le=100_000)
    # Cascading impact analysis (server-enforced; a request can only choose values inside these bounds)
    impact_max_active_analyses: int = Field(default=3, ge=1, le=20)
    impact_time_budget_seconds: float = Field(default=30.0, gt=0, le=600)
    impact_max_path_steps: int = Field(default=50_000, ge=1_000, le=1_000_000)
    impact_max_findings: int = Field(default=2_000, ge=10, le=20_000)
    inventory_max_import_rows: int = Field(default=10_000, ge=10, le=100_000)
    inventory_max_pairs: int = Field(default=1_000, ge=10, le=20_000)
    inventory_max_horizon_days: int = Field(default=365, ge=7, le=365)
    inventory_time_budget_seconds: float = Field(default=30.0, gt=0, le=600)
    inventory_max_active_analyses: int = Field(default=3, ge=1, le=20)
    # Demand forecasting (server-enforced bounds)
    forecast_max_horizon_daily: int = Field(default=90, ge=7, le=365)
    forecast_max_horizon_weekly: int = Field(default=26, ge=4, le=104)
    forecast_max_history_periods: int = Field(default=730, ge=30, le=3_650)
    forecast_max_folds: int = Field(default=12, ge=3, le=30)
    forecast_max_candidates: int = Field(default=10, ge=1, le=10)
    forecast_max_active_runs: int = Field(default=4, ge=1, le=20)
    forecast_time_budget_seconds: float = Field(default=60.0, gt=0, le=900)
    forecast_finalization_days: int = Field(default=7, ge=0, le=90)
    # Lead-time and delivery-risk intelligence (server-enforced bounds)
    leadtime_max_import_rows: int = Field(default=20_000, ge=10, le=200_000)
    leadtime_max_records_per_run: int = Field(default=20_000, ge=100, le=200_000)
    leadtime_max_active_runs: int = Field(default=4, ge=1, le=20)
    leadtime_time_budget_seconds: float = Field(default=60.0, gt=0, le=900)
    # Model evaluation, explainability and governance (server-enforced bounds)
    modelops_max_active_evaluations: int = Field(default=4, ge=1, le=20)
    modelops_time_budget_seconds: float = Field(default=60.0, gt=0, le=900)
    # Route optimization and transportation intelligence (server-enforced bounds)
    routing_max_lanes: int = Field(default=5000, ge=10, le=100_000)
    routing_max_bulk_lanes: int = Field(default=200, ge=1, le=2000)
    routing_max_active_runs: int = Field(default=4, ge=1, le=20)
    routing_time_budget_seconds: float = Field(default=30.0, gt=0, le=900)
    routing_max_expansions: int = Field(default=200_000, ge=1000, le=5_000_000)
    routing_min_reliability_sample: int = Field(default=20, ge=3, le=1000)
    routing_max_delivery_rows: int = Field(default=50_000, ge=100, le=500_000)
    routing_stale_days: int = Field(default=365, ge=1, le=3650)
    sourcing_max_candidates: int = Field(default=40, ge=2, le=500)
    sourcing_max_active_runs: int = Field(default=4, ge=1, le=20)
    sourcing_time_budget_seconds: float = Field(default=30.0, gt=0, le=900)
    sourcing_max_nodes: int = Field(default=200_000, ge=1000, le=5_000_000)
    sourcing_max_grid_units: int = Field(default=20_000, ge=100, le=200_000)
    sourcing_min_reliability_sample: int = Field(default=20, ge=3, le=1000)
    sourcing_max_delivery_rows: int = Field(default=50_000, ge=100, le=500_000)
    sourcing_stale_days: int = Field(default=365, ge=1, le=3650)
    invopt_max_active_runs: int = Field(default=4, ge=1, le=20)
    invopt_time_budget_seconds: float = Field(default=30.0, gt=0, le=900)
    invopt_max_sim_steps: int = Field(default=3_000_000, ge=10_000, le=50_000_000)
    invopt_max_replications: int = Field(default=1000, ge=10, le=10_000)
    invopt_max_delivery_rows: int = Field(default=50_000, ge=100, le=500_000)
    invopt_max_pairs_overview: int = Field(default=2000, ge=10, le=50_000)
    resilience_max_active_runs: int = Field(default=3, ge=1, le=20)
    resilience_time_budget_seconds: float = Field(default=40.0, gt=0, le=900)
    resilience_max_candidates: int = Field(default=40_000, ge=100, le=500_000)
    resilience_max_scenarios: int = Field(default=8, ge=1, le=20)
    resilience_max_sensitivity_variants: int = Field(default=24, ge=1, le=100)
    decision_max_evidence: int = Field(default=12, ge=2, le=40)
    decision_max_events_per_case: int = Field(default=500, ge=10, le=5000)
    decision_max_evaluations_per_case: int = Field(default=50, ge=1, le=500)
    decision_max_impact_rows: int = Field(default=200, ge=10, le=2000)
    analytics_cache_ttl_seconds: float = Field(default=5.0, ge=0, le=300)
    analytics_export_max_rows: int = Field(default=5000, ge=10, le=20000)
    # 0 disables the in-process timer; POST /alerting/tick still works
    # --- process topology ---
    embedded_worker: bool = (
        True  # run the alert tick and integration worker inside the API process; false when a separate worker runs
    )
    # --- performance, reliability, capacity ---
    cache_enabled: bool = True
    cache_max_entries: int = Field(default=256, ge=8, le=100_000)
    cache_ttl_seconds: float = Field(default=60.0, ge=1, le=3600)
    readiness_schema_cache_seconds: float = Field(default=30.0, ge=0, le=600)
    api_rate_limit_per_minute: int = Field(default=1200, ge=0, le=1_000_000)  # per credential, per worker; 0 disables
    db_pool_size: int = Field(default=10, ge=1, le=200)  # non-SQLite engines only
    db_max_overflow: int = Field(default=10, ge=0, le=200)
    db_pool_timeout_seconds: float = Field(default=10.0, ge=1, le=120)
    db_pool_recycle_seconds: int = Field(default=1800, ge=30, le=86400)
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=0, le=60_000)
    sqlite_wal: bool = False  # opt in on local disks; WAL is unreliable on network/virtual file systems
    job_lease_seconds: int = Field(default=120, ge=10, le=3600)
    sync_max_seconds: int = Field(default=900, ge=10, le=14_400)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=100)  # consecutive failed sync runs
    circuit_cooldown_seconds: int = Field(default=300, ge=10, le=86_400)
    worker_stale_seconds: int = Field(default=300, ge=10, le=86_400)
    alerting_tick_seconds: float = Field(default=0.0, ge=0, le=86400)
    # JSON {"name": "https://..."}; URLs are secrets and are never returned or logged
    alerting_webhooks: SecretStr | None = None
    # comma-separated host allow-list; empty means the webhook channel is unavailable
    alerting_webhook_hosts: str = ""
    alerting_webhook_timeout_seconds: float = Field(default=5.0, ge=0.5, le=30)
    alerting_max_attempts: int = Field(default=4, ge=1, le=10)
    alerting_retry_base_seconds: int = Field(default=60, ge=1, le=3600)
    alerting_inbox_retention_days: int = Field(default=90, ge=1, le=3650)

    # Integrations. No secret is stored in the database; see docs/api.md.
    integrations_master_key: SecretStr | None = None  # derives webhook signing secrets
    integration_hosts: str = ""  # comma-separated host allow-list for outbound webhooks and REST connectors
    webhook_max_attempts: int = Field(default=6, ge=1, le=12)
    webhook_retry_base_seconds: int = Field(default=30, ge=1, le=3600)
    webhook_timeout_seconds: float = Field(default=5.0, ge=0.5, le=30)
    webhook_tolerance_seconds: int = Field(default=300, ge=30, le=3600)  # inbound timestamp window
    sync_max_records: int = Field(default=10_000, ge=1, le=1_000_000)
    sync_page_retry_attempts: int = Field(default=3, ge=1, le=8)
    export_ttl_minutes: int = Field(default=60, ge=1, le=10_080)
    export_max_rows: int = Field(default=100_000, ge=1, le=5_000_000)
    # Authentication (users, organisations, server-side sessions); static API tokens remain as service accounts
    auth_session_ttl_minutes: int = Field(default=480, ge=5, le=60 * 24 * 14)  # absolute session lifetime
    auth_session_idle_minutes: int = Field(default=60, ge=1, le=60 * 24)  # inactivity timeout
    auth_reset_ttl_minutes: int = Field(default=30, ge=5, le=24 * 60)
    auth_scrypt_n: int = Field(default=2**15, ge=2**10, le=2**20)  # work factor; must be a power of two
    auth_scrypt_r: int = Field(default=8, ge=1, le=32)
    auth_scrypt_p: int = Field(default=1, ge=1, le=16)
    auth_min_password_length: int = Field(default=12, ge=8, le=128)
    auth_login_max_failures: int = Field(default=5, ge=1, le=100)  # per (username, client) before a back-off applies
    auth_login_window_seconds: int = Field(default=900, ge=30, le=86400)
    auth_rate_limit_per_minute: int = Field(default=30, ge=1, le=10_000)  # per client address on /auth endpoints
    security_headers_enabled: bool = True

    @field_validator("auth_scrypt_n")
    @classmethod
    def _check_scrypt_n(cls, value: int) -> int:
        if value & (value - 1):
            raise ValueError("auth_scrypt_n must be a power of two")
        return value

    @field_validator("api_prefix")
    @classmethod
    def _check_prefix(cls, value: str) -> str:
        if not value.startswith("/") or value.endswith("/") or " " in value:
            raise ValueError("api_prefix must start with '/' and must not end with '/'")
        return value

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("data_dir", mode="after")
    @classmethod
    def _absolute_data_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (REPO_ROOT / value).resolve()

    @model_validator(mode="after")
    def _production_rules(self) -> Settings:
        """Fail fast: staging and production must never start with development-grade security settings."""
        if self.production_like:
            where = f"ATLAS_ENVIRONMENT={self.environment}"
            if any(origin == "*" for origin in self.cors_allowed_origins):
                raise ValueError("Wildcard CORS origin is not allowed in production")
            if not self.cors_allowed_origins:
                raise ValueError(f"{where} requires explicit ATLAS_CORS_ALLOWED_ORIGINS")
            if any(
                o.startswith("http://") and "localhost" not in o and "127.0.0.1" not in o
                for o in self.cors_allowed_origins
            ):
                raise ValueError(
                    f"{where}: ATLAS_CORS_ALLOWED_ORIGINS must use https:// (plain http only for localhost)"
                )
            if not self.security_headers_enabled:
                raise ValueError(f"{where} requires ATLAS_SECURITY_HEADERS_ENABLED=true")
            if self.log_level.upper() == "DEBUG":
                raise ValueError(f"{where} must not run with ATLAS_LOG_LEVEL=DEBUG")
            if self.integrations_master_key is not None and len(self.integrations_master_key.get_secret_value()) < 32:
                raise ValueError(f"{where}: ATLAS_INTEGRATIONS_MASTER_KEY must be at least 32 characters")
        if any(origin == "*" for origin in self.cors_allowed_origins) and self.environment != "development":
            raise ValueError("Wildcard CORS origin is only tolerated in development")
        return self

    @property
    def production_like(self) -> bool:
        """True for staging and production: no unauthenticated dev actor, no API docs, strict validation."""
        return self.environment in ("staging", "production")

    @field_validator("api_tokens", mode="after")
    @classmethod
    def _check_tokens(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            from app.core.auth import parse_token_table

            parse_token_table(value.get_secret_value())  # raises ValueError with a safe message
        return value

    @property
    def resolved_database_url(self) -> str:
        """Configured URL, or a SQLite file inside the data directory."""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'atlas.db').as_posix()}"

    @property
    def safe_database_url(self) -> str:
        """Database URL with any password masked (safe for diagnostics)."""
        return make_url(self.resolved_database_url).render_as_string(hide_password=True)

    @property
    def write_api_allowed(self) -> bool:
        """Write endpoints (ingestion) are off by default in production until authentication exists."""
        if self.write_api_enabled is not None:
            return self.write_api_enabled
        return self.environment != "production"

    @property
    def demo_data_allowed(self) -> bool:
        if self.demo_data_enabled is not None:
            return self.demo_data_enabled
        return self.environment != "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
