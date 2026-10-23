"""Prometheus instrumentation.

Every instrument is created lazily exactly once per process so that the
registry never sees duplicate collectors (which would raise at import time
during test collection).

Label cardinality is deliberately bounded:

* ``route`` is the *matched* FastAPI route template, never a raw URL.
* ``model`` is limited to the models present in the price book.
* ``tenant`` is **never** used as a label - it would explode the time series
  count.  Per-tenant reporting is served by the ``/v1/usage`` API instead.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "ACTIVE_IDEMPOTENCY_LOCKS",
    "AUDIT_RECORDS_UPLOADED_TOTAL",
    "COST_MICROCENTS_TOTAL",
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION_SECONDS",
    "IDEMPOTENT_REPLAYS_TOTAL",
    "ML_ANOMALIES_DETECTED_TOTAL",
    "PAYMENT_REQUIRED_TOTAL",
    "QUOTA_CHECKS_TOTAL",
    "RATE_LIMIT_REJECTIONS_TOTAL",
    "TOKENS_TOTAL",
    "TOKEN_THROUGHPUT_TOKENS_PER_SECOND",
    "USAGE_EVENTS_PERSISTED_TOTAL",
    "WEBHOOK_EVENTS_TOTAL",
]

# -- HTTP -------------------------------------------------------------------
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the metering API.",
    labelnames=("route", "method", "status"),
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "Latency of the metering API in seconds.",
    labelnames=("route", "method"),
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# -- Token accounting -------------------------------------------------------
TOKENS_TOTAL = Counter(
    "llm_tokens_total",
    "Tokens metered, partitioned by kind.",
    labelnames=("model", "kind"),
)

TOKEN_THROUGHPUT_TOKENS_PER_SECOND = Histogram(
    "llm_token_throughput_tokens_per_second",
    "Observed token throughput per request.",
    labelnames=("model", "kind"),
    buckets=(1, 10, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 50_000, 100_000),
)

COST_MICROCENTS_TOTAL = Counter(
    "llm_cost_microcents_total",
    "Cumulative billed cost in micro-cents (1 USD = 1_000_000 micro-cents).",
    labelnames=("model",),
)

USAGE_EVENTS_PERSISTED_TOTAL = Counter(
    "llm_usage_events_persisted_total",
    "Usage events durably written to the ledger.",
    labelnames=("model", "status"),
)

# -- Guardrails -------------------------------------------------------------
RATE_LIMIT_REJECTIONS_TOTAL = Counter(
    "rate_limit_rejections_total",
    "Requests rejected because a quota or rate limit was exhausted.",
    labelnames=("rejection_type",),
)

PAYMENT_REQUIRED_TOTAL = Counter(
    "payment_required_total",
    "Requests rejected with 402 because the subscription is not active.",
    labelnames=("subscription_status",),
)

QUOTA_CHECKS_TOTAL = Counter(
    "llm_quota_checks_total",
    "Quota checks executed, partitioned by outcome.",
    labelnames=("outcome",),
)

# -- Idempotency ------------------------------------------------------------
IDEMPOTENT_REPLAYS_TOTAL = Counter(
    "llm_idempotent_replays_total",
    "Requests served from the idempotency result cache instead of being re-executed.",
)

ACTIVE_IDEMPOTENCY_LOCKS = Gauge(
    "llm_active_idempotency_locks",
    "Idempotency locks currently held by this process.",
)

# -- ML anomaly guard -------------------------------------------------------
ML_ANOMALIES_DETECTED_TOTAL = Counter(
    "ml_anomalies_detected_total",
    "Anomalies detected by the IsolationForest guard.",
    labelnames=("anomaly_type",),
)

# -- Integrations -----------------------------------------------------------
WEBHOOK_EVENTS_TOTAL = Counter(
    "llm_webhook_events_total",
    "Stripe webhook events handled, partitioned by outcome.",
    labelnames=("event_type", "outcome"),
)

AUDIT_RECORDS_UPLOADED_TOTAL = Counter(
    "llm_audit_records_uploaded_total",
    "Audit records flushed to S3 / LocalStack.",
)
