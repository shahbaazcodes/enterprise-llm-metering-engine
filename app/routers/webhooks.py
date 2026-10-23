"""``POST /v1/webhooks/stripe`` - signature-verified subscription sync.

The handler is intentionally strict:

* the **raw** body is read (never the re-serialised JSON) so the HMAC is
  computed over exactly the bytes Stripe signed;
* the ``Stripe-Signature`` header is verified before the event is parsed;
* a forged signature produces ``400`` and touches **no** state.

A valid event is applied exactly once - the ``processed_webhooks`` table
carries a UNIQUE ``event_id`` constraint inside the same transaction as the
plan change, so a Stripe retry can never double-flip a plan.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session
from app.schemas import WebhookResponse
from app.settings import Settings, get_settings
from app.stripe_service import StripeService

logger = logging.getLogger("llm.metering.webhooks")

router = APIRouter(tags=["billing"])


@router.post(
    "/webhooks/stripe",
    response_model=WebhookResponse,
    summary="Stripe subscription webhook (signature verified)",
)
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WebhookResponse:
    """Verify and apply a Stripe event."""
    payload = await request.body()
    service = StripeService(session, settings)

    # Raises InvalidWebhookSignatureError -> HTTP 400 (no state is touched).
    event = service.verify_signature(payload, stripe_signature)
    outcome = await service.handle_event(event)

    return WebhookResponse(
        received=True,
        event_id=outcome.event_id,
        event_type=outcome.event_type,
        status=outcome.status,
        message=outcome.message,
        plan=outcome.plan,
        tenant_id=outcome.tenant_id,
    )
