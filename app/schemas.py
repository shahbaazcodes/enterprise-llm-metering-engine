"""Pydantic v2 request / response schemas for the public API.

Money is **always** exposed as an integer count of micro-cents
(``*_microcents``).  A ``usd_display`` string is added next to it purely for
human consumption - it is never used for accounting.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UsageOverride(BaseModel):
    """Explicit token counters for a request.

    This is a *benchmark / simulation* affordance: it lets a caller (or the
    evaluation harness) pin the exact token consumption of a request so the
    metering path can be exercised deterministically without upstream LLM
    credentials.  It is disabled in production.
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    cached_input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    output_tokens: int = Field(default=0, ge=0, le=10_000_000)
    reasoning_tokens: int = Field(default=0, ge=0, le=10_000_000)
    reasoning_included_in_output: bool = True


class GenerateRequest(BaseModel):
    """Body of ``POST /v1/generate``."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=200_000)
    model: str = Field(default="gpt-4o", max_length=128)
    max_output_tokens: int = Field(default=256, ge=1, le=128_000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    simulated_usage: UsageOverride | None = None


class TokenUsageModel(BaseModel):
    """Token counters returned to the caller."""

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    billable_output_tokens: int
    total_tokens: int


class CostModel(BaseModel):
    """Itemised cost, denominated in micro-cents."""

    model: str
    input_microcents: int
    cached_input_microcents: int
    output_microcents: int
    reasoning_microcents: int
    total_microcents: int
    currency: str
    usd_display: str


class QuotaModel(BaseModel):
    """Quota state after the request was settled."""

    allowed: bool
    reason: str
    limit_microcents: int
    used_microcents: int
    requested_microcents: int
    remaining_microcents: int
    period: str


class AnomalyModel(BaseModel):
    """Verdict of the IsolationForest guard."""

    is_anomaly: bool
    score: float
    anomaly_type: str | None = None
    reason: str


class GenerateResponse(BaseModel):
    """Successful response of ``POST /v1/generate``."""

    request_id: str
    tenant_id: str
    model: str
    output: str
    usage: TokenUsageModel
    cost: CostModel
    quota: QuotaModel
    anomaly: AnomalyModel
    idempotent_replay: bool = False
    created_at: str


class ModelBreakdown(BaseModel):
    """Per-model rollup."""

    model: str
    requests: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    cost_microcents: int
    usd_display: str


class UsageSummaryResponse(BaseModel):
    """Response of ``GET /v1/usage``."""

    tenant_id: str
    slug: str
    plan: str
    period: str
    since: str
    until: str
    total_requests: int
    total_tokens: int
    total_cost_microcents: int
    usd_display: str
    plan_limit_microcents: int
    remaining_microcents: int
    by_model: list[ModelBreakdown]


class WebhookResponse(BaseModel):
    """Response of ``POST /v1/webhooks/stripe``."""

    received: bool
    event_id: str
    event_type: str
    status: str
    message: str
    plan: str | None = None
    tenant_id: str | None = None


class HealthResponse(BaseModel):
    """Response of ``GET /health``."""

    status: str
    database: str
    redis: str
    version: str
    environment: str
