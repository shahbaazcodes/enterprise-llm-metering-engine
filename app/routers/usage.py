"""``GET /v1/usage`` - the tenant's billable usage rollup.

The response is a **reporting** view over the immutable ledger: the same
micro-cent integers that were charged at request time are re-aggregated, so
the report can never disagree with the invoice.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calculator import microcents_to_usd_str
from app.deps import get_session, resolve_tenant
from app.metering import billing_period
from app.models import Tenant, UsageEvent
from app.schemas import ModelBreakdown, UsageSummaryResponse

router = APIRouter(tags=["metering"])


@router.get(
    "/usage",
    response_model=UsageSummaryResponse,
    summary="Tenant usage rollup and cost breakdown",
)
async def usage_summary(
    period: str | None = Query(
        default=None,
        description="Billing period as YYYY-MM. Defaults to the current UTC period.",
    ),
    model: str | None = Query(default=None, description="Filter by model name."),
    tenant: Tenant = Depends(resolve_tenant),
    session: AsyncSession = Depends(get_session),
) -> UsageSummaryResponse:
    """Aggregate the tenant's usage for a billing period."""
    period_key = period or billing_period()
    try:
        start = datetime.strptime(period_key, "%Y-%m").replace(tzinfo=UTC)
    except ValueError:
        now = datetime.now(UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        period_key = start.strftime("%Y-%m")
    if start.month == 12:
        end = start.replace(
            year=start.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        end = start.replace(month=start.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)

    statement = select(
        UsageEvent.model,
        func.count(UsageEvent.id),
        func.coalesce(func.sum(UsageEvent.input_tokens), 0),
        func.coalesce(func.sum(UsageEvent.cached_input_tokens), 0),
        func.coalesce(func.sum(UsageEvent.output_tokens), 0),
        func.coalesce(func.sum(UsageEvent.reasoning_tokens), 0),
        func.coalesce(func.sum(UsageEvent.total_tokens), 0),
        func.coalesce(func.sum(UsageEvent.cost_microcents), 0),
    ).where(
        UsageEvent.tenant_id == tenant.id,
        UsageEvent.created_at >= start,
        UsageEvent.created_at < end,
    )
    if model:
        statement = statement.where(UsageEvent.model == model)
    statement = statement.group_by(UsageEvent.model).order_by(UsageEvent.model)

    rows = (await session.execute(statement)).all()

    breakdown: list[ModelBreakdown] = []
    total_requests = 0
    total_tokens = 0
    total_cost = 0
    for row in rows:
        (
            row_model,
            requests,
            input_tokens,
            cached_tokens,
            output_tokens,
            reasoning_tokens,
            row_total_tokens,
            row_cost,
        ) = row
        total_requests += int(requests)
        total_tokens += int(row_total_tokens)
        total_cost += int(row_cost)
        breakdown.append(
            ModelBreakdown(
                model=row_model,
                requests=int(requests),
                input_tokens=int(input_tokens),
                cached_input_tokens=int(cached_tokens),
                output_tokens=int(output_tokens),
                reasoning_tokens=int(reasoning_tokens),
                total_tokens=int(row_total_tokens),
                cost_microcents=int(row_cost),
                usd_display=microcents_to_usd_str(int(row_cost)),
            )
        )

    plan = tenant.plan
    limit = plan.monthly_limit_microcents if plan else 0
    return UsageSummaryResponse(
        tenant_id=tenant.slug,
        slug=tenant.slug,
        plan=plan.code if plan else "free",
        period=period_key,
        since=start.isoformat(),
        until=end.isoformat(),
        total_requests=total_requests,
        total_tokens=total_tokens,
        total_cost_microcents=total_cost,
        usd_display=microcents_to_usd_str(total_cost),
        plan_limit_microcents=limit,
        remaining_microcents=max(0, limit - total_cost),
        by_model=breakdown,
    )
