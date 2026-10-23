"""SQLAlchemy 2.0 ORM mappings for the financial ledger.

Design rules enforced here:

* Money columns are **always** ``BigInteger`` micro-cents.
* Token counters are ``Integer`` (32-bit is ample and keeps row size down).
* Primary keys are application-generated UUIDs so that a row can be written
  by a producer before it is persisted (useful for audit ledgers).
* ``JSONB`` on PostgreSQL, portable ``JSON`` elsewhere (the evaluation
  harness runs on SQLite when Docker is unavailable).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

#: Portable JSON column - native ``JSONB`` on PostgreSQL.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""


class TimestampMixin:
    """``created_at`` / ``updated_at`` bookkeeping columns.

    Used by the *mutable* entities (plans, tenants, subscriptions).
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CreatedAtMixin:
    """``created_at`` only - for append-only ledgers.

    Immutable rows (usage events, price cards) never carry an ``updated_at``
    column: the absence of the column is a schema-level guarantee that the
    row can never be rewritten.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Plan(TimestampMixin, Base):
    """Commercial plan: spend cap, rate limit and pricing modifiers."""

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    monthly_limit_microcents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    cached_input_discount_bps: Mapped[int] = mapped_column(Integer, default=2500, nullable=False)
    reasoning_included_in_output: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    stripe_price_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tenants: Mapped[list[Tenant]] = relationship(back_populates="plan", lazy="raise")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Plan {self.code} limit={self.monthly_limit_microcents}mc>"


class Tenant(TimestampMixin, Base):
    """A billable customer."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    api_key_hash: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)

    plan: Mapped[Plan] = relationship(back_populates="tenants", lazy="selectin")
    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="tenant", lazy="raise", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Tenant {self.slug}>"


class Subscription(TimestampMixin, Base):
    """Stripe-backed entitlement driving the 402 / 429 decision."""

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False, index=True)
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="subscriptions", lazy="selectin")
    plan: Mapped[Plan] = relationship(lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Subscription {self.stripe_subscription_id} status={self.status}>"


class ModelPricing(CreatedAtMixin, Base):
    """Immutable price card, micro-cents per 1_000_000 tokens."""

    __tablename__ = "model_pricing"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    model: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), default="openai", nullable=False)
    input_microcents_per_million: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_microcents_per_million: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cached_input_microcents_per_million: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("model", "effective_at", name="model_pricing_unique"),)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ModelPricing {self.model}>"


class UsageEvent(CreatedAtMixin, Base):
    """Immutable ledger row: one metered LLM invocation."""

    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), default="simulated", nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_microcents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_anomaly: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    anomaly_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, default=dict, nullable=False
    )

    __table_args__ = (
        Index("usage_events_tenant_created_at_idx", "tenant_id", text("created_at DESC")),
        Index("usage_events_tenant_model_idx", "tenant_id", "model"),
        Index("usage_events_created_at_idx", text("created_at DESC")),
        # The Redis lock is the fast path; this partial unique index is the
        # race-proof final backstop.  It is partial so that keyless
        # (fire-and-forget) traffic remains allowed.
        Index(
            "usage_events_idempotency_key_uq",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<UsageEvent {self.model} cost={self.cost_microcents}mc>"


class ProcessedWebhook(Base):
    """Ledger of consumed Stripe events - the dedup key is ``event_id``.

    Append-only: the row is written once, when the event is first seen, and
    its ``outcome`` column is the only thing that is ever updated.
    """

    __tablename__ = "processed_webhooks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    api_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    livemode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), default="processed", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("processed_webhooks_type_idx", "event_type"),
        Index("processed_webhooks_received_at_idx", received_at.desc()),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ProcessedWebhook {self.event_id} {self.outcome}>"


class SchemaMigration(Base):
    """Applied-migration ledger."""

    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(128), primary_key=True)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
