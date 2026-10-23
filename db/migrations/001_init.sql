-- ===========================================================================
-- 001_init.sql
-- LLM Usage Metering & Billing Engine - core schema (PostgreSQL 16)
--
-- MONEY REPRESENTATION
--   Every monetary column is a BIGINT holding MICRO-CENTS (1 USD = 1_000_000
--   micro-cents). Integer arithmetic only - no FLOAT, no NUMERIC rounding
--   ambiguity, no IEEE-754 drift. Costs are rounded exactly once, at the
--   point of computation, using round-half-up.
--
--   Naming convention: *_microcents
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ---------------------------------------------------------------------------
-- plans: commercial catalogue. 0 monthly_limit_microcents == unlimited spend.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS plans (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code                        TEXT        NOT NULL UNIQUE,
    name                        TEXT        NOT NULL,
    description                 TEXT        NOT NULL DEFAULT '',
    monthly_limit_microcents    BIGINT      NOT NULL DEFAULT 0,
    rate_limit_rpm              INTEGER     NOT NULL DEFAULT 60,
    cached_input_discount_bps   INTEGER     NOT NULL DEFAULT 2500,
    reasoning_included_in_output BOOLEAN   NOT NULL DEFAULT TRUE,
    stripe_price_id             TEXT        UNIQUE,
    is_active                   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT plans_code_format_chk   CHECK (code ~ '^[a-z0-9_]{2,64}$'),
    CONSTRAINT plans_limit_nonneg_chk  CHECK (monthly_limit_microcents >= 0),
    CONSTRAINT plans_discount_chk      CHECK (cached_input_discount_bps BETWEEN 0 AND 10000),
    CONSTRAINT plans_rpm_nonneg_chk    CHECK (rate_limit_rpm >= 0)
);

COMMENT ON TABLE plans IS
    'Subscription plans. monthly_limit_microcents is the tenant spend cap per billing period; 0 means unlimited.';

-- ---------------------------------------------------------------------------
-- tenants: the billing entity. plan_id is the effective (materialised) plan.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                TEXT        NOT NULL UNIQUE,
    name                TEXT        NOT NULL,
    email               TEXT        UNIQUE,
    api_key_hash        TEXT        UNIQUE,
    plan_id             UUID        NOT NULL REFERENCES plans (id) ON DELETE RESTRICT,
    stripe_customer_id  TEXT        UNIQUE,
    status              TEXT        NOT NULL DEFAULT 'active',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenants_status_chk CHECK (status IN ('active', 'suspended', 'closed'))
);

CREATE INDEX IF NOT EXISTS tenants_plan_id_idx ON tenants (plan_id);
CREATE INDEX IF NOT EXISTS tenants_stripe_customer_idx ON tenants (stripe_customer_id)
    WHERE stripe_customer_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- subscriptions: Stripe-backed entitlement, 1:N over tenants.
-- status drives the 402 Payment Required decision.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscriptions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    plan_id                 UUID        NOT NULL REFERENCES plans (id) ON DELETE RESTRICT,
    stripe_subscription_id  TEXT        UNIQUE,
    stripe_customer_id      TEXT,
    status                  TEXT        NOT NULL DEFAULT 'active',
    current_period_start    TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,
    cancel_at_period_end    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT subscriptions_status_chk CHECK (
        status IN ('active', 'trialing', 'past_due', 'unpaid', 'canceled', 'incomplete', 'incomplete_expired', 'paused')
    )
);

CREATE INDEX IF NOT EXISTS subscriptions_tenant_id_idx ON subscriptions (tenant_id);
CREATE INDEX IF NOT EXISTS subscriptions_status_idx ON subscriptions (status);

-- ---------------------------------------------------------------------------
-- model_pricing: immutable price book.
-- Prices are micro-cents per 1_000_000 tokens (1M-token billing unit).
--   $2.50 / 1M tokens  -> 2_500_000 micro-cents
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_pricing (
    id                                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model                               TEXT        NOT NULL,
    provider                            TEXT        NOT NULL DEFAULT 'openai',
    input_microcents_per_million        BIGINT      NOT NULL,
    output_microcents_per_million       BIGINT      NOT NULL,
    cached_input_microcents_per_million BIGINT      NOT NULL,
    effective_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT model_pricing_unique UNIQUE (model, effective_at),
    CONSTRAINT model_pricing_input_nonneg_chk  CHECK (input_microcents_per_million >= 0),
    CONSTRAINT model_pricing_output_nonneg_chk CHECK (output_microcents_per_million >= 0),
    CONSTRAINT model_pricing_cached_nonneg_chk CHECK (cached_input_microcents_per_million >= 0)
);

CREATE INDEX IF NOT EXISTS model_pricing_model_idx ON model_pricing (model, effective_at DESC);

-- ---------------------------------------------------------------------------
-- usage_events: immutable financial ledger. One row per metered LLM call.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    request_id          TEXT        NOT NULL,
    idempotency_key     TEXT,
    model               TEXT        NOT NULL,
    provider            TEXT        NOT NULL DEFAULT 'simulated',
    input_tokens        INTEGER     NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER     NOT NULL DEFAULT 0,
    output_tokens       INTEGER     NOT NULL DEFAULT 0,
    reasoning_tokens    INTEGER     NOT NULL DEFAULT 0,
    total_tokens        INTEGER     NOT NULL DEFAULT 0,
    cost_microcents     BIGINT      NOT NULL DEFAULT 0,
    currency            TEXT        NOT NULL DEFAULT 'USD',
    status              TEXT        NOT NULL DEFAULT 'completed',
    latency_ms          INTEGER,
    is_anomaly          BOOLEAN     NOT NULL DEFAULT FALSE,
    anomaly_type        TEXT,
    metadata            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT usage_events_status_chk CHECK (status IN ('completed', 'failed', 'rejected')),
    CONSTRAINT usage_events_tokens_nonneg_chk CHECK (
        input_tokens >= 0 AND cached_input_tokens >= 0
        AND output_tokens >= 0 AND reasoning_tokens >= 0 AND total_tokens >= 0
    ),
    CONSTRAINT usage_events_cost_nonneg_chk CHECK (cost_microcents >= 0)
);

-- Idempotency: the unique index is the final, race-proof backstop for the
-- Redis lock. Partial so that keyless (fire-and-forget) traffic is allowed.
CREATE UNIQUE INDEX IF NOT EXISTS usage_events_idempotency_key_uq
    ON usage_events (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Hot-path reporting index.
CREATE INDEX IF NOT EXISTS usage_events_tenant_created_at_idx
    ON usage_events (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS usage_events_tenant_model_idx
    ON usage_events (tenant_id, model);

CREATE INDEX IF NOT EXISTS usage_events_created_at_idx
    ON usage_events (created_at DESC);

-- ---------------------------------------------------------------------------
-- processed_webhooks: Stripe event ledger for exactly-once processing.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS processed_webhooks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id      TEXT        NOT NULL UNIQUE,
    event_type    TEXT        NOT NULL,
    api_version   TEXT,
    livemode      BOOLEAN     NOT NULL DEFAULT FALSE,
    payload       JSONB       NOT NULL,
    outcome       TEXT        NOT NULL DEFAULT 'processed',
    error         TEXT,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ,
    CONSTRAINT processed_webhooks_outcome_chk CHECK (
        outcome IN ('processed', 'duplicate', 'ignored', 'failed')
    )
);

CREATE INDEX IF NOT EXISTS processed_webhooks_type_idx ON processed_webhooks (event_type);
CREATE INDEX IF NOT EXISTS processed_webhooks_received_at_idx ON processed_webhooks (received_at DESC);

-- ---------------------------------------------------------------------------
-- schema_migrations: applied-migration ledger (managed by app.db).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    checksum    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

