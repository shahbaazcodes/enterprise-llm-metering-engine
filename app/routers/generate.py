"""``POST /v1/generate`` - the billable LLM endpoint.

Request pipeline (each stage is a hard gate)::

    tenant auth -> idempotency lock -> entitlement (402) -> rate limit (429)
      -> spend reservation (429) -> provider call -> cost settlement
      -> anomaly guard -> ledger write -> response

Money is reserved **before** the provider is called and settled **after**,
so a tenant can never exceed its cap even if the provider returns more
tokens than estimated.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.calculator import TokenUsage, calculate_token_cost_micro_cents
from app.deps import (
    get_idempotency_manager,
    get_quota_checker,
    get_session,
    resolve_tenant,
)
from app.exceptions import (
    IdempotencyConflictError,
    InvalidRequestError,
    QuotaExceededError,
    RateLimitExceededError,
)
from app.metering import (
    IdempotencyManager,
    QuotaChecker,
    acquire_or_conflict,
    billing_period,
    enforce_entitlement,
    load_price_card,
    resolve_entitlement,
)
from app.ml_guard import AnomalyGuard, UsageSample
from app.models import Tenant, UsageEvent
from app.providers import SimulatedProvider, estimate_input_tokens
from app.schemas import GenerateRequest, GenerateResponse
from app.settings import Settings, get_settings

logger = logging.getLogger("llm.metering.generate")

router = APIRouter(tags=["metering"])


def _guard(request: Request) -> AnomalyGuard:
    """Return the process-wide anomaly guard stored on the app state."""
    guard = getattr(request.app.state, "anomaly_guard", None)
    if guard is None:
        guard = AnomalyGuard()
        request.app.state.anomaly_guard = guard
    return guard


def _provider(request: Request) -> SimulatedProvider:
    """Return the configured upstream provider."""
    provider = getattr(request.app.state, "provider", None)
    if provider is None:
        provider = SimulatedProvider()
        request.app.state.provider = provider
    return provider


def _estimate_usage(request: GenerateRequest) -> TokenUsage:
    """Worst-case token usage used for the spend reservation."""
    if request.simulated_usage is not None:
        override = request.simulated_usage
        return TokenUsage(
            input_tokens=override.input_tokens,
            cached_input_tokens=override.cached_input_tokens,
            output_tokens=override.output_tokens,
            reasoning_tokens=override.reasoning_tokens,
            reasoning_included_in_output=override.reasoning_included_in_output,
        )
    return TokenUsage(
        input_tokens=estimate_input_tokens(request.prompt),
        output_tokens=request.max_output_tokens,
        reasoning_included_in_output=True,
    )


@router.post(
    "/generate",
    response_model=GenerateResponse,
    status_code=200,
    summary="Metered LLM completion",
)
async def generate(
    request: Request,
    response: Response,
    body: GenerateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    tenant: Tenant = Depends(resolve_tenant),
    session: AsyncSession = Depends(get_session),
    quota: QuotaChecker = Depends(get_quota_checker),
    idem: IdempotencyManager = Depends(get_idempotency_manager),
    settings: Settings = Depends(get_settings),
) -> GenerateResponse:
    """Execute a billable LLM call and record it in the ledger."""
    if body.simulated_usage is not None and not settings.allow_usage_override:
        raise InvalidRequestError(
            "Usage overrides are disabled on this deployment.",
            reason="usage_override_disabled",
        )

    request_id = uuid.uuid4().hex
    period = billing_period()
    tenant_id = str(tenant.id)

    # --- 1. idempotency --------------------------------------------------
    lock = None
    if idempotency_key:
        lock, cached = await acquire_or_conflict(idem, tenant_id, idempotency_key)
        if cached is not None:
            payload = json.loads(cached)
            payload["idempotent_replay"] = True
            response.headers["X-Idempotent-Replay"] = "true"
            return GenerateResponse(**payload)

    try:
        result = await _execute(
            request=request,
            response=response,
            body=body,
            tenant=tenant,
            session=session,
            quota=quota,
            settings=settings,
            request_id=request_id,
            period=period,
            idempotency_key=idempotency_key,
        )
    finally:
        if lock is not None:
            await idem.release(lock)

    if idempotency_key:
        await idem.store_result(
            tenant_id, idempotency_key, result.model_dump_json().encode("utf-8")
        )
    return result


async def _execute(
    *,
    request: Request,
    response: Response,
    body: GenerateRequest,
    tenant: Tenant,
    session: AsyncSession,
    quota: QuotaChecker,
    settings: Settings,
    request_id: str,
    period: str,
    idempotency_key: str | None,
) -> GenerateResponse:
    """Run the metering pipeline for a single request."""
    tenant_id = str(tenant.id)

    # --- 2. entitlement (402) -------------------------------------------
    entitlement = await resolve_entitlement(session, tenant)
    enforce_entitlement(entitlement, tenant.slug)

    plan = tenant.plan
    limit_microcents = plan.monthly_limit_microcents if plan else 0
    rate_limit = plan.rate_limit_rpm if plan else settings.free_rate_limit_rpm
    discount_bps = plan.cached_input_discount_bps if plan else settings.cached_input_discount_bps

    # --- 3. rate limit (429) ---------------------------------------------
    allowed, window_count = await quota.check_rate_limit(tenant_id, rate_limit)
    metrics.QUOTA_CHECKS_TOTAL.labels(
        outcome="rate_limit_allowed" if allowed else "rate_limited"
    ).inc()
    if not allowed:
        metrics.RATE_LIMIT_REJECTIONS_TOTAL.labels(rejection_type="rate_limited").inc()
        raise RateLimitExceededError(
            f"Request rate limit exceeded for tenant '{tenant.slug}'.",
            tenant=tenant.slug,
            rate_limit=rate_limit,
            window_count=window_count,
            retry_after_seconds=settings.redis_rate_limit_window_seconds,
        )

    # --- 4. price card + spend reservation (429) ------------------------
    price = await load_price_card(session, body.model, settings)
    estimated = _estimate_usage(body)
    estimated_cost = calculate_token_cost_micro_cents(
        estimated, price, cached_input_discount_bps=discount_bps
    ).total_microcents

    reserved, used_after, limit = await quota.reserve(
        tenant_id, period, estimated_cost, limit_microcents
    )
    if not reserved:
        metrics.QUOTA_CHECKS_TOTAL.labels(outcome="quota_exceeded").inc()
        metrics.RATE_LIMIT_REJECTIONS_TOTAL.labels(rejection_type="quota_exceeded").inc()
        raise QuotaExceededError(
            f"Monthly spend quota exhausted for tenant '{tenant.slug}'.",
            tenant=tenant.slug,
            limit_microcents=limit,
            used_microcents=used_after,
            requested_microcents=estimated_cost,
            remaining_microcents=max(0, limit - used_after),
        )
    metrics.QUOTA_CHECKS_TOTAL.labels(outcome="reserved").inc()

    # --- 5. provider call + settlement ----------------------------------
    provider = _provider(request)
    usage_override = body.simulated_usage if settings.allow_usage_override else None
    provider_result = await provider.complete(body, override=usage_override)
    usage = provider_result.usage

    cost = calculate_token_cost_micro_cents(
        usage,
        price,
        cached_input_discount_bps=discount_bps,
    )
    await quota.settle(tenant_id, period, estimated_cost, cost.total_microcents)
    metrics.COST_MICROCENTS_TOTAL.labels(model=price.model).inc(cost.total_microcents)

    # --- 6. anomaly guard ------------------------------------------------
    guard = _guard(request)
    anomaly = await guard.evaluate(
        UsageSample(
            tenant_id=tenant_id,
            model=price.model,
            total_tokens=usage.total_tokens,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cost_microcents=cost.total_microcents,
            steps=1,
            window_request_count=window_count,
            latency_ms=provider_result.latency_ms,
        )
    )

    # --- 7. ledger write -------------------------------------------------
    event = UsageEvent(
        tenant_id=tenant.id,
        request_id=request_id,
        idempotency_key=idempotency_key,
        model=price.model,
        provider=provider_result.provider,
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        total_tokens=usage.total_tokens,
        cost_microcents=cost.total_microcents,
        currency=cost.currency,
        status="completed",
        latency_ms=provider_result.latency_ms,
        is_anomaly=anomaly.is_anomaly,
        anomaly_type=anomaly.anomaly_type,
        metadata_={**body.metadata, "plan": plan.code if plan else None},
    )
    session.add(event)
    try:
        await session.commit()
        metrics.USAGE_EVENTS_PERSISTED_TOTAL.labels(model=price.model, status="completed").inc()
    except IntegrityError:
        # The partial unique index on ``idempotency_key`` fired: a concurrent
        # request already wrote this row.  Never double-bill.
        await session.rollback()
        metrics.USAGE_EVENTS_PERSISTED_TOTAL.labels(model=price.model, status="duplicate").inc()
        raise IdempotencyConflictError(
            "A request with the same Idempotency-Key was already recorded.",
            tenant=tenant.slug,
            idempotency_key=idempotency_key,
            retry_after_seconds=1,
        ) from None

    used_microcents = await quota.current_spend(tenant_id, period)
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Cost-Microcents"] = str(cost.total_microcents)

    return GenerateResponse(
        request_id=request_id,
        tenant_id=tenant.slug,
        model=price.model,
        output=provider_result.text,
        usage={
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "billable_output_tokens": usage.billable_output_tokens,
            "total_tokens": usage.total_tokens,
        },
        cost=cost.to_dict(),
        quota={
            "allowed": True,
            "reason": "within_quota",
            "limit_microcents": limit_microcents,
            "used_microcents": used_microcents,
            "requested_microcents": cost.total_microcents,
            "remaining_microcents": max(0, limit_microcents - used_microcents),
            "period": period,
        },
        anomaly=anomaly.to_dict(),
        idempotent_replay=False,
        created_at=datetime.now(UTC).isoformat(),
    )
