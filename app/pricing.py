"""Price-book resolution.

The price book lives in the database (``model_pricing``) so that a rate
change is an *append-only* event: a new row with a later ``effective_at``
becomes the active price card.  Historical ledger rows keep the rate that
was in force when they were written.

When the database has no card for a model, the static price book from the
configuration is used as a fallback.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calculator import ModelPrice
from app.exceptions import UnknownModelError
from app.models import ModelPricing
from app.settings import Settings, get_settings

logger = logging.getLogger("llm.metering.pricing")


async def load_price_card(
    session: AsyncSession, model: str, settings: Settings | None = None
) -> ModelPrice:
    """Return the active price card for ``model``.

    Raises :class:`UnknownModelError` (HTTP 400) when the model is not in
    the price book - an unbillable model must never be metered.
    """
    settings = settings or get_settings()
    row = (
        (
            await session.execute(
                select(ModelPricing)
                .where(ModelPricing.model == model)
                .order_by(ModelPricing.effective_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    if row is None:
        entry = settings.price_for(model)
        if entry is None:
            raise UnknownModelError(
                f"No price card for model '{model}'.",
                model=model,
                known_models=sorted(settings.model_pricing),
            )
        return ModelPrice(
            model=model,
            input_microcents_per_million=entry.input_microcents_per_million,
            output_microcents_per_million=entry.output_microcents_per_million,
            cached_input_microcents_per_million=entry.cached_input_microcents_per_million,
            provider=entry.provider,
        )

    return ModelPrice(
        model=model,
        input_microcents_per_million=row.input_microcents_per_million,
        output_microcents_per_million=row.output_microcents_per_million,
        cached_input_microcents_per_million=row.cached_input_microcents_per_million,
        provider=row.provider,
    )
