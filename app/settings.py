"""Application configuration (Pydantic v2 / pydantic-settings).

All runtime knobs are environment driven.  The module exposes a cached
:func:`get_settings` accessor so the configuration is parsed exactly once
per process.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# --------------------------------------------------------------------------
# Money / unit constants - the single source of truth for unit conversion.
# --------------------------------------------------------------------------
MICROCENTS_PER_USD = 1_000_000
"""1 USD expressed in micro-cents."""

TOKENS_PER_PRICE_UNIT = 1_000_000
"""Prices are quoted per 1M tokens (the industry-standard billing unit)."""

BILLING_CURRENCY = "USD"


class PriceBookEntry(BaseModel):
    """A single model's price card, denominated in micro-cents per 1M tokens."""

    provider: str = "openai"
    input_microcents_per_million: int = Field(ge=0)
    output_microcents_per_million: int = Field(ge=0)
    cached_input_microcents_per_million: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def usd_per_million_input(self) -> float:
        """Human-facing USD figure. NEVER used for accounting."""
        return self.input_microcents_per_million / MICROCENTS_PER_USD


def _default_price_book() -> dict[str, PriceBookEntry]:
    """Static price book used to seed ``model_pricing``.

    Values are micro-cents per 1_000_000 tokens, e.g. $2.50 / 1M tokens is
    ``2_500_000`` micro-cents.
    """
    return {
        "gpt-4o": PriceBookEntry(
            input_microcents_per_million=2_500_000,
            output_microcents_per_million=10_000_000,
            cached_input_microcents_per_million=625_000,
        ),
        "gpt-4o-mini": PriceBookEntry(
            input_microcents_per_million=150_000,
            output_microcents_per_million=600_000,
            cached_input_microcents_per_million=37_500,
        ),
        "o3-mini": PriceBookEntry(
            input_microcents_per_million=1_100_000,
            output_microcents_per_million=4_400_000,
            cached_input_microcents_per_million=275_000,
        ),
        "claude-sonnet-4": PriceBookEntry(
            provider="anthropic",
            input_microcents_per_million=3_000_000,
            output_microcents_per_million=15_000_000,
            cached_input_microcents_per_million=750_000,
        ),
        "claude-opus-4": PriceBookEntry(
            provider="anthropic",
            input_microcents_per_million=15_000_000,
            output_microcents_per_million=75_000_000,
            cached_input_microcents_per_million=3_750_000,
        ),
        "gemini-1.5-pro": PriceBookEntry(
            provider="google",
            input_microcents_per_million=1_250_000,
            output_microcents_per_million=5_000_000,
            cached_input_microcents_per_million=312_500,
        ),
    }


class Settings(BaseSettings):
    """Strongly typed runtime settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- application --------------------------------------------------------
    app_name: str = "llm-metering-engine"
    environment: Literal["development", "staging", "production", "test"] = "development"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    api_v1_prefix: str = "/v1"
    default_model: str = "gpt-4o"

    # -- HTTP server --------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    uvicorn_workers: int = 1
    uvicorn_reload: bool = False
    #: Require a bearer API key in addition to ``X-Tenant-ID``.
    api_key_required: bool = False
    #: Allow a caller to pin the token counters of a request (benchmarking).
    #: Always disabled in production - see :meth:`Settings.__post_init__`.
    allow_usage_override: bool = True

    # -- PostgreSQL ---------------------------------------------------------
    database_url: str = "postgresql+asyncpg://metering:metering@localhost:5432/metering"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800
    db_echo: bool = False
    auto_migrate: bool = True
    auto_seed: bool = True

    # -- Redis --------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    redis_lock_ttl_seconds: int = 300
    redis_idempotency_ttl_seconds: int = 86_400
    redis_rate_limit_window_seconds: int = 60
    redis_socket_timeout: int = 5

    # -- Stripe (test mode) -------------------------------------------------
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_api_version: str = "2024-06-20"
    stripe_price_pro: str = ""
    stripe_price_free: str = ""
    stripe_webhook_tolerance_seconds: int = 300

    # -- AWS S3 / LocalStack ------------------------------------------------
    aws_region: str = "us-east-1"
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"
    s3_endpoint_url: str | None = "http://localhost:4566"
    s3_bucket: str = "llm-metering-audit"
    s3_prefix: str = "audit"
    s3_addressing_style: str = "path"
    audit_enabled: bool = True
    audit_flush_interval_seconds: int = 3600
    audit_batch_size: int = 500

    # -- plan quotas --------------------------------------------------------
    free_plan_code: str = "free"
    pro_plan_code: str = "pro"
    free_monthly_limit_microcents: int = 5_000_000  # $5.00
    pro_monthly_limit_microcents: int = 500_000_000  # $500.00
    free_rate_limit_rpm: int = 60
    pro_rate_limit_rpm: int = 600
    cached_input_discount_bps: int = 2500  # cached input billed at 25% of input rate
    reasoning_included_in_output: bool = True

    # -- ML anomaly guard ---------------------------------------------------
    ml_guard_enabled: bool = True
    ml_contamination: float = 0.05
    ml_min_samples: int = 32
    ml_window_size: int = 512
    ml_refit_interval: int = 16
    ml_score_threshold: float = 0.0
    ml_max_tokens_per_request: int = 400_000
    ml_max_steps_per_request: int = 50

    # -- observability ------------------------------------------------------
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"

    # -- price book ---------------------------------------------------------
    model_pricing: dict[str, PriceBookEntry] = Field(default_factory=_default_price_book)

    # -- derived ------------------------------------------------------------
    def model_post_init(self, __context: Any) -> None:
        """Harden the configuration for production.

        * The usage override is a benchmarking affordance; it must never be
          reachable in production, where it would let a caller choose their
          own price.
        """
        if self.is_production:
            self.allow_usage_override = False
            self.api_key_required = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def redis_prefix(self) -> str:
        """Namespace for every Redis key owned by this service."""
        return "mm"

    def plan_quota(self, plan_code: str) -> tuple[int, int]:
        """Return ``(monthly_limit_microcents, rate_limit_rpm)`` for a plan code."""
        if plan_code == self.pro_plan_code:
            return self.pro_monthly_limit_microcents, self.pro_rate_limit_rpm
        if plan_code == self.free_plan_code:
            return self.free_monthly_limit_microcents, self.free_rate_limit_rpm
        return 0, self.free_rate_limit_rpm

    def price_for(self, model: str) -> PriceBookEntry | None:
        """Look up a price card, falling back to the closest known model."""
        if model in self.model_pricing:
            return self.model_pricing[model]
        for candidate, entry in self.model_pricing.items():
            if model.startswith(candidate):
                return entry
        return None

    def redacted(self) -> dict[str, Any]:
        """Settings snapshot with secrets masked - safe to log."""
        secret_markers = ("secret", "password", "key")
        out: dict[str, Any] = {}
        for name, value in self.model_dump().items():
            if any(marker in name for marker in secret_markers) and value:
                out[name] = "***redacted***"
            else:
                out[name] = value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
