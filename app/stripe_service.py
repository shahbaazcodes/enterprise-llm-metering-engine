"""Stripe (test mode) integration: signature verification and plan sync.

Responsibilities
----------------
1. **Verify the signature.** The raw request body is verified against the
   ``whsec_...`` signing secret with Stripe's own HMAC scheme and a
   replay-tolerance window.  A forged signature is rejected with ``400``
   *before* any state is touched.
2. **Deduplicate.** Every event id (``evt_...``) is inserted into
   ``processed_webhooks`` under a UNIQUE constraint inside the same
   transaction as the state change.  A redelivered event is therefore
   processed exactly once, and a failed processing is rolled back so that
   Stripe's retry can safely re-drive it.
3. **Flip the plan.** ``checkout.session.completed`` upgrades the tenant to
   *Pro*; ``customer.subscription.deleted`` downgrades it to *Free*.
   ``customer.subscription.updated`` keeps the tenant's plan in sync with
   the subscribed Stripe price, and invoice events drive the
   active / past_due status that the ``402`` decision depends on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
import stripe

from app import metrics
from app.exceptions import InvalidWebhookSignatureError
from app.models import Plan, ProcessedWebhook, Subscription, Tenant
from app.settings import Settings, get_settings

logger = logging.getLogger("llm.metering.stripe")

#: Events the service acts upon.  Everything else is acknowledged and ignored.
HANDLED_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.paid",
        "invoice.payment_failed",
        "invoice.payment_succeeded",
    }
)

#: Stripe subscription statuses mapped onto our internal vocabulary.
STRIPE_STATUS_MAP = {
    "active": "active",
    "trialing": "trialing",
    "past_due": "past_due",
    "unpaid": "unpaid",
    "canceled": "canceled",
    "incomplete": "incomplete",
    "incomplete_expired": "incomplete_expired",
    "paused": "paused",
}


@dataclass(frozen=True, slots=True)
class WebhookOutcome:
    """Result of handling a single Stripe event."""

    event_id: str
    event_type: str
    status: str  # processed | duplicate | ignored | failed
    message: str = ""
    plan: str | None = None
    tenant_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "status": self.status,
            "message": self.message,
            "plan": self.plan,
            "tenant_id": self.tenant_id,
        }


class StripeService:
    """Verifies, deduplicates and applies Stripe subscription events."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()
        if self._settings.stripe_secret_key:
            stripe.api_key = self._settings.stripe_secret_key

    # -- signature verification ---------------------------------------------
    def verify_signature(self, payload: bytes, signature: str | None) -> stripe.Event:
        """Verify the ``Stripe-Signature`` header.

        Raises :class:`InvalidWebhookSignatureError` (HTTP 400) for a missing
        header, a missing secret, a malformed body or a bad signature.
        """
        if not signature:
            raise InvalidWebhookSignatureError(
                "Missing Stripe-Signature header.", reason="missing_signature"
            )
        secret = self._settings.stripe_webhook_secret
        if not secret:
            raise InvalidWebhookSignatureError(
                "Stripe webhook secret is not configured on the server.",
                reason="secret_not_configured",
            )
        try:
            event = stripe.Webhook.construct_event(
                payload,
                signature,
                secret,
                tolerance=self._settings.stripe_webhook_tolerance_seconds,
            )
        except ValueError as exc:  # malformed JSON body
            raise InvalidWebhookSignatureError(
                "Webhook payload could not be parsed.", reason="invalid_payload"
            ) from exc
        except stripe.error.StripeError as exc:
            raise InvalidWebhookSignatureError(
                "Stripe signature verification failed.", reason="signature_mismatch"
            ) from exc
        return event

    # -- plan resolution ----------------------------------------------------
    async def _plan_by_code(self, code: str) -> Plan | None:
        return (
            (await self._session.execute(select(Plan).where(Plan.code == code))).scalars().first()
        )

    async def _plan_by_price(self, price_id: str | None) -> Plan | None:
        if not price_id:
            return None
        if self._settings.stripe_price_pro and price_id == self._settings.stripe_price_pro:
            return await self._plan_by_code(self._settings.pro_plan_code)
        if self._settings.stripe_price_free and price_id == self._settings.stripe_price_free:
            return await self._plan_by_code(self._settings.free_plan_code)
        return (
            (await self._session.execute(select(Plan).where(Plan.stripe_price_id == price_id)))
            .scalars()
            .first()
        )

    async def _find_tenant(self, tenant_id: str | None, customer_id: str | None) -> Tenant | None:
        if tenant_id:
            from uuid import UUID

            try:
                parsed = UUID(tenant_id)
            except (ValueError, AttributeError):
                parsed = None
            if parsed is not None:
                tenant = (
                    (await self._session.execute(select(Tenant).where(Tenant.id == parsed)))
                    .scalars()
                    .first()
                )
                if tenant is not None:
                    return tenant
            tenant = (
                (await self._session.execute(select(Tenant).where(Tenant.slug == tenant_id)))
                .scalars()
                .first()
            )
            if tenant is not None:
                return tenant
        if customer_id:
            return (
                (
                    await self._session.execute(
                        select(Tenant).where(Tenant.stripe_customer_id == customer_id)
                    )
                )
                .scalars()
                .first()
            )
        return None

    async def _set_plan(self, tenant: Tenant, plan: Plan | None) -> str | None:
        """Materialise a plan change onto the tenant row."""
        if plan is None:
            return tenant.plan.code if tenant.plan else None
        tenant.plan_id = plan.id
        return plan.code

    # -- event handling -----------------------------------------------------
    async def handle_event(self, event: stripe.Event) -> WebhookOutcome:
        """Apply a verified event, exactly once."""
        event_id = str(event.id)
        event_type = str(event.type)

        existing = (
            (
                await self._session.execute(
                    select(ProcessedWebhook).where(ProcessedWebhook.event_id == event_id)
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            metrics.WEBHOOK_EVENTS_TOTAL.labels(event_type=event_type, outcome="duplicate").inc()
            return WebhookOutcome(
                event_id=event_id,
                event_type=event_type,
                status="duplicate",
                message="Event already processed; ignored.",
            )

        if event_type not in HANDLED_EVENTS:
            record = ProcessedWebhook(
                event_id=event_id,
                event_type=event_type,
                api_version=getattr(event, "api_version", None),
                livemode=bool(getattr(event, "livemode", False)),
                payload=_event_payload(event),
                outcome="ignored",
                processed_at=datetime.now(UTC),
            )
            self._session.add(record)
            await self._session.commit()
            metrics.WEBHOOK_EVENTS_TOTAL.labels(event_type=event_type, outcome="ignored").inc()
            return WebhookOutcome(
                event_id=event_id,
                event_type=event_type,
                status="ignored",
                message="Event type is not relevant for plan synchronisation.",
            )

        record = ProcessedWebhook(
            event_id=event_id,
            event_type=event_type,
            api_version=getattr(event, "api_version", None),
            livemode=bool(getattr(event, "livemode", False)),
            payload=_event_payload(event),
            outcome="processed",
        )
        self._session.add(record)
        try:
            # Flush early so the UNIQUE constraint fires before any side effect.
            await self._session.flush()
        except IntegrityError:
            await self._session.rollback()
            metrics.WEBHOOK_EVENTS_TOTAL.labels(event_type=event_type, outcome="duplicate").inc()
            return WebhookOutcome(
                event_id=event_id,
                event_type=event_type,
                status="duplicate",
                message="Event already processed; ignored.",
            )

        outcome = await self._apply_event(event)
        record.outcome = outcome.status
        record.processed_at = datetime.now(UTC)
        await self._session.commit()
        metrics.WEBHOOK_EVENTS_TOTAL.labels(event_type=event_type, outcome=outcome.status).inc()
        return outcome

    async def _apply_event(self, event: stripe.Event) -> WebhookOutcome:
        event_id = str(event.id)
        event_type = str(event.type)
        obj = _event_object(event)

        if event_type == "checkout.session.completed":
            return await self._apply_checkout_completed(event_id, event_type, obj)
        if event_type in {"customer.subscription.created", "customer.subscription.updated"}:
            return await self._apply_subscription_update(event_id, event_type, obj)
        if event_type == "customer.subscription.deleted":
            return await self._apply_subscription_deleted(event_id, event_type, obj)
        if event_type in {"invoice.paid", "invoice.payment_succeeded"}:
            return await self._apply_invoice_status(event_id, event_type, obj, "active")
        if event_type == "invoice.payment_failed":
            return await self._apply_invoice_status(event_id, event_type, obj, "past_due")
        return WebhookOutcome(
            event_id=event_id, event_type=event_type, status="ignored", message="No handler."
        )

    async def _apply_checkout_completed(
        self, event_id: str, event_type: str, obj: dict[str, Any]
    ) -> WebhookOutcome:
        """``checkout.session.completed`` -> upgrade the tenant to *Pro*."""
        customer_id = _as_str(obj.get("customer"))
        metadata = obj.get("metadata") or {}
        tenant_ref = metadata.get("tenant_id") or obj.get("client_reference_id")
        tenant = await self._find_tenant(tenant_ref, customer_id)
        if tenant is None:
            return WebhookOutcome(
                event_id=event_id,
                event_type=event_type,
                status="ignored",
                message="No tenant matched the checkout session.",
            )
        if customer_id:
            tenant.stripe_customer_id = customer_id
        plan = await self._plan_by_code(self._settings.pro_plan_code)
        plan_code = await self._set_plan(tenant, plan)
        return WebhookOutcome(
            event_id=event_id,
            event_type=event_type,
            status="processed",
            message=f"Tenant upgraded to '{plan_code}'.",
            plan=plan_code,
            tenant_id=tenant.slug,
        )

    async def _apply_subscription_update(
        self, event_id: str, event_type: str, obj: dict[str, Any]
    ) -> WebhookOutcome:
        """Keep the tenant's plan and subscription status in sync."""
        customer_id = _as_str(obj.get("customer"))
        metadata = obj.get("metadata") or {}
        tenant = await self._find_tenant(metadata.get("tenant_id"), customer_id)
        subscription_id = _as_str(obj.get("id")) or _as_str(obj.get("subscription"))
        status = STRIPE_STATUS_MAP.get(str(obj.get("status", "active")), "active")

        price_id = _extract_price_id(obj)
        plan = await self._plan_by_price(price_id)
        if plan is None:
            plan = await self._plan_by_code(self._settings.pro_plan_code)

        if tenant is None and subscription_id:
            existing = (
                (
                    await self._session.execute(
                        select(Subscription).where(
                            Subscription.stripe_subscription_id == subscription_id
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                tenant = existing.tenant
        if tenant is None:
            return WebhookOutcome(
                event_id=event_id,
                event_type=event_type,
                status="ignored",
                message="No tenant matched the subscription.",
            )

        if customer_id:
            tenant.stripe_customer_id = customer_id
        plan_code = await self._set_plan(tenant, plan)

        subscription = (
            (
                await self._session.execute(
                    select(Subscription).where(
                        Subscription.stripe_subscription_id == subscription_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if subscription is None:
            subscription = Subscription(
                tenant_id=tenant.id,
                plan_id=plan.id if plan else tenant.plan_id,
                stripe_subscription_id=subscription_id,
                stripe_customer_id=customer_id,
                status=status,
            )
            self._session.add(subscription)
        else:
            subscription.status = status
            if plan is not None:
                subscription.plan_id = plan.id
            if customer_id:
                subscription.stripe_customer_id = customer_id
        return WebhookOutcome(
            event_id=event_id,
            event_type=event_type,
            status="processed",
            message=f"Subscription synchronised (status={status}, plan={plan_code}).",
            plan=plan_code,
            tenant_id=tenant.slug,
        )

    async def _apply_subscription_deleted(
        self, event_id: str, event_type: str, obj: dict[str, Any]
    ) -> WebhookOutcome:
        """``customer.subscription.deleted`` -> downgrade the tenant to *Free*."""
        subscription_id = _as_str(obj.get("id")) or _as_str(obj.get("subscription"))
        customer_id = _as_str(obj.get("customer"))
        metadata = obj.get("metadata") or {}

        tenant = await self._find_tenant(metadata.get("tenant_id"), customer_id)
        subscription = None
        if subscription_id:
            subscription = (
                (
                    await self._session.execute(
                        select(Subscription).where(
                            Subscription.stripe_subscription_id == subscription_id
                        )
                    )
                )
                .scalars()
                .first()
            )
        if tenant is None and subscription is not None:
            tenant = subscription.tenant
        if tenant is None:
            return WebhookOutcome(
                event_id=event_id,
                event_type=event_type,
                status="ignored",
                message="No tenant matched the deleted subscription.",
            )
        if subscription is not None:
            subscription.status = "canceled"

        free_plan = await self._plan_by_code(self._settings.free_plan_code)
        plan_code = await self._set_plan(tenant, free_plan)
        return WebhookOutcome(
            event_id=event_id,
            event_type=event_type,
            status="processed",
            message=f"Subscription cancelled; tenant downgraded to '{plan_code}'.",
            plan=plan_code,
            tenant_id=tenant.slug,
        )

    async def _apply_invoice_status(
        self, event_id: str, event_type: str, obj: dict[str, Any], status: str
    ) -> WebhookOutcome:
        """Drive the subscription status from invoice payment events."""
        customer_id = _as_str(obj.get("customer"))
        subscription_id = _as_str(obj.get("subscription"))
        tenant = await self._find_tenant(None, customer_id)
        subscription = None
        if subscription_id:
            subscription = (
                (
                    await self._session.execute(
                        select(Subscription).where(
                            Subscription.stripe_subscription_id == subscription_id
                        )
                    )
                )
                .scalars()
                .first()
            )
        if tenant is None and subscription is not None:
            tenant = subscription.tenant
        if subscription is None:
            return WebhookOutcome(
                event_id=event_id,
                event_type=event_type,
                status="ignored",
                message="No subscription matched the invoice.",
            )
        subscription.status = status
        return WebhookOutcome(
            event_id=event_id,
            event_type=event_type,
            status="processed",
            message=f"Subscription status set to '{status}'.",
            plan=tenant.plan.code if tenant and tenant.plan else None,
            tenant_id=tenant.slug if tenant else None,
        )


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------
def _event_object(event: stripe.Event) -> dict[str, Any]:
    """Return the event's data object as a plain dictionary.

    ``stripe.StripeObject`` stopped being a ``dict`` subclass in the v9 line;
    it now exposes ``to_dict()`` and raises ``AttributeError`` for the mapping
    methods.  Relying on ``isinstance(obj, dict)`` alone therefore silently
    yields an *empty* payload, which would make every event look unmatched.
    """
    data = getattr(event, "data", None)
    obj = getattr(data, "object", None) if data is not None else None
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            return dict(to_dict())
        except (TypeError, ValueError):
            return {}
    try:
        return dict(obj)
    except (TypeError, ValueError):
        return {}


def _event_payload(event: stripe.Event) -> dict[str, Any]:
    """Serialisable snapshot of the event, stored for the audit trail."""
    try:
        return dict(event.to_dict())
    except Exception:  # noqa: BLE001 - never fail on audit serialisation
        return {"id": getattr(event, "id", None), "type": getattr(event, "type", None)}


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _extract_price_id(obj: dict[str, Any]) -> str | None:
    """Find the Stripe price id in a subscription payload."""
    items = obj.get("items")
    if isinstance(items, dict):
        items = items.get("data")
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            price = first.get("price")
            if isinstance(price, dict):
                return _as_str(price.get("id"))
            return _as_str(first.get("price"))
    price = obj.get("price")
    if isinstance(price, dict):
        return _as_str(price.get("id"))
    return _as_str(price)
