"""P5 - Stripe webhook signature verification and plan synchronisation.

The webhook is the only path through which a plan can change without a
human action, so it is the most security-critical endpoint of the service.
These probes exercise the *real* Stripe signature scheme (``t=``/``v1=`` with
an HMAC-SHA256 over ``"<timestamp>.<raw body>"``) against the production
``stripe.Webhook.construct_event`` verifier:

* a valid signature is applied,
* a forged / tampered signature is refused with ``400`` and touches **no**
  state,
* a missing signature header is refused,
* a replayed (too old) timestamp is refused, a recent one is accepted,
* a redelivered event id is applied exactly once,
* the plan flip (upgrade / downgrade) is materialised on the tenant row.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Plan, ProcessedWebhook, Subscription, Tenant
from eval.conftest import STRIPE_PRICE_PRO, STRIPE_WEBHOOK_SECRET

WEBHOOK_URL = "/v1/webhooks/stripe"


# ---------------------------------------------------------------------------
# Helpers - Stripe's own signing scheme
# ---------------------------------------------------------------------------
def _sign(
    payload: bytes, *, secret: str = STRIPE_WEBHOOK_SECRET, timestamp: int | None = None
) -> str:
    """Build a ``Stripe-Signature`` header the way Stripe does."""
    ts = int(time.time()) if timestamp is None else timestamp
    signed_payload = f"{ts}.".encode() + payload
    digest = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def _event_bytes(event_id: str, event_type: str, obj: dict[str, Any]) -> bytes:
    """Serialise a Stripe event exactly as it will be signed and delivered."""
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "api_version": "2024-06-20",
            "created": int(time.time()),
            "livemode": False,
            "pending_webhooks": 0,
            "request": {"id": None, "idempotency_key": None},
            "type": event_type,
            "data": {"object": obj},
        },
        separators=(",", ":"),
    ).encode("utf-8")


async def _post(client: Any, payload: bytes, signature: str | None) -> Any:
    """Deliver a webhook payload with (or without) a signature header."""
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["Stripe-Signature"] = signature
    return await client.post(WEBHOOK_URL, content=payload, headers=headers)


async def _tenant_plan(
    db_session_factory: async_sessionmaker[AsyncSession], slug: str
) -> str | None:
    """Read the tenant's current plan code in a fresh session."""
    async with db_session_factory() as session:
        tenant = (
            (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalars().first()
        )
        if tenant is None:
            return None
        plan = (
            (await session.execute(select(Plan).where(Plan.id == tenant.plan_id))).scalars().first()
        )
        return plan.code if plan else None


async def _processed_ids(db_session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """Every event id that was written to the audit trail."""
    async with db_session_factory() as session:
        result = await session.execute(select(ProcessedWebhook.event_id))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# P5 - signature verification
# ---------------------------------------------------------------------------
async def test_webhook_signature_verification(
    client: Any,
    seeded: dict[str, Any],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A valid signature is applied; a forged one is refused and changes nothing."""
    tenant = seeded["eval-free"]
    payload = _event_bytes(
        "evt_signature_ok",
        "checkout.session.completed",
        {
            "object": "checkout.session",
            "customer": tenant.stripe_customer_id,
            "metadata": {"tenant_id": tenant.slug},
        },
    )

    # --- a forged signature must be refused -------------------------------
    forged = _sign(payload, secret="whsec_wrong_secret")
    response = await _post(client, payload, forged)
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"] == "invalid_webhook_signature"
    assert body["details"]["reason"] == "signature_mismatch"

    # A tampered body (signature computed over the original) is refused too.
    tampered = payload.replace(b"eval-free", b"eval-pro!")
    assert tampered != payload
    response = await _post(client, payload, _sign(tampered))
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid_webhook_signature"

    # ... and a missing header is refused.
    response = await _post(client, payload, None)
    assert response.status_code == 400, response.text
    assert response.json()["details"]["reason"] == "missing_signature"

    # None of the forged attempts touched the state.
    assert await _processed_ids(db_session_factory) == []
    assert await _tenant_plan(db_session_factory, "eval-free") == "free"

    # --- the genuine signature is accepted --------------------------------
    response = await _post(client, payload, _sign(payload))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["received"] is True
    assert body["event_id"] == "evt_signature_ok"
    assert body["event_type"] == "checkout.session.completed"
    assert body["status"] == "processed"
    assert body["plan"] == "pro"
    assert body["tenant_id"] == "eval-free"


async def test_webhook_signature_timestamp_tolerance(
    client: Any,
    seeded: dict[str, Any],
    settings: Any,
) -> None:
    """A replayed (stale) timestamp is refused; a recent one is accepted."""
    tenant = seeded["eval-pro"]
    tolerance = settings.stripe_webhook_tolerance_seconds

    payload = _event_bytes(
        "evt_stale",
        "checkout.session.completed",
        {
            "object": "checkout.session",
            "customer": tenant.stripe_customer_id,
            "metadata": {"tenant_id": tenant.slug},
        },
    )
    stale_ts = int(time.time()) - tolerance - 60
    response = await _post(client, payload, _sign(payload, timestamp=stale_ts))
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid_webhook_signature"

    # Just inside the window the very same event is accepted.
    fresh_ts = int(time.time()) - (tolerance - 30)
    response = await _post(client, payload, _sign(payload, timestamp=fresh_ts))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "processed"


# ---------------------------------------------------------------------------
# P5 - plan state transitions
# ---------------------------------------------------------------------------
async def test_webhook_checkout_session_upgrades_tenant_to_pro(
    client: Any,
    seeded: dict[str, Any],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``checkout.session.completed`` moves a tenant from Free to Pro."""
    tenant = seeded["eval-free"]
    assert await _tenant_plan(db_session_factory, "eval-free") == "free"

    payload = _event_bytes(
        "evt_checkout_completed",
        "checkout.session.completed",
        {
            "object": "checkout.session",
            "customer": tenant.stripe_customer_id,
            "metadata": {"tenant_id": tenant.slug},
        },
    )
    response = await _post(client, payload, _sign(payload))
    assert response.status_code == 200, response.text
    assert response.json()["plan"] == "pro"
    assert await _tenant_plan(db_session_factory, "eval-free") == "pro"


async def test_webhook_subscription_deleted_downgrades_tenant_to_free(
    client: Any,
    seeded: dict[str, Any],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``customer.subscription.deleted`` moves a tenant from Pro back to Free."""
    tenant = seeded["eval-pro"]
    assert await _tenant_plan(db_session_factory, "eval-pro") == "pro"

    payload = _event_bytes(
        "evt_subscription_deleted",
        "customer.subscription.deleted",
        {
            "object": "subscription",
            "id": "sub_eval_deleted",
            "customer": tenant.stripe_customer_id,
            "status": "canceled",
            "metadata": {"tenant_id": tenant.slug},
        },
    )
    response = await _post(client, payload, _sign(payload))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "processed"
    assert body["plan"] == "free"
    assert await _tenant_plan(db_session_factory, "eval-pro") == "free"


async def test_webhook_subscription_update_syncs_plan_and_status(
    client: Any,
    seeded: dict[str, Any],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``customer.subscription.updated`` syncs both the plan and the status."""
    tenant = seeded["eval-free"]
    payload = _event_bytes(
        "evt_subscription_updated",
        "customer.subscription.updated",
        {
            "object": "subscription",
            "id": "sub_eval_updated",
            "customer": tenant.stripe_customer_id,
            "status": "active",
            "metadata": {"tenant_id": tenant.slug},
            "items": {"data": [{"price": {"id": STRIPE_PRICE_PRO}}]},
        },
    )
    response = await _post(client, payload, _sign(payload))
    assert response.status_code == 200, response.text
    assert response.json()["plan"] == "pro"
    assert await _tenant_plan(db_session_factory, "eval-free") == "pro"

    async with db_session_factory() as session:
        subscription = (
            (
                await session.execute(
                    select(Subscription).where(
                        Subscription.stripe_subscription_id == "sub_eval_updated"
                    )
                )
            )
            .scalars()
            .first()
        )
    assert subscription is not None
    assert subscription.status == "active"
    assert subscription.tenant_id == tenant.id


# ---------------------------------------------------------------------------
# P5 - deduplication
# ---------------------------------------------------------------------------
async def test_webhook_duplicate_event_is_applied_exactly_once(
    client: Any,
    seeded: dict[str, Any],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A redelivered ``evt_`` id is acknowledged but never applied twice."""
    tenant = seeded["eval-free"]
    payload = _event_bytes(
        "evt_duplicate",
        "checkout.session.completed",
        {
            "object": "checkout.session",
            "customer": tenant.stripe_customer_id,
            "metadata": {"tenant_id": tenant.slug},
        },
    )
    signature = _sign(payload)

    first = await _post(client, payload, signature)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "processed"

    second = await _post(client, payload, signature)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["status"] == "duplicate"
    assert body["event_id"] == "evt_duplicate"

    # The audit trail holds the event exactly once.
    assert await _processed_ids(db_session_factory) == ["evt_duplicate"]


async def test_webhook_invoice_payment_failed_marks_subscription_past_due(
    client: Any,
    seeded: dict[str, Any],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``invoice.payment_failed`` drives the subscription into ``past_due``."""
    tenant = seeded["eval-pro"]
    async with db_session_factory() as session:
        session.add(
            Subscription(
                tenant_id=tenant.id,
                plan_id=tenant.plan_id,
                stripe_subscription_id="sub_eval_invoiced",
                stripe_customer_id=tenant.stripe_customer_id,
                status="active",
            )
        )
        await session.commit()

    payload = _event_bytes(
        "evt_invoice_failed",
        "invoice.payment_failed",
        {
            "object": "invoice",
            "customer": tenant.stripe_customer_id,
            "subscription": "sub_eval_invoiced",
        },
    )
    response = await _post(client, payload, _sign(payload))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "processed"

    async with db_session_factory() as session:
        subscription = (
            (
                await session.execute(
                    select(Subscription).where(
                        Subscription.stripe_subscription_id == "sub_eval_invoiced"
                    )
                )
            )
            .scalars()
            .first()
        )
    assert subscription is not None
    assert subscription.status == "past_due"


async def test_webhook_unhandled_event_is_acknowledged_and_ignored(
    client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An event we do not act upon is acknowledged and audited, not rejected."""
    payload = _event_bytes("evt_unhandled", "customer.created", {"object": "customer"})
    response = await _post(client, payload, _sign(payload))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ignored"
    assert body["event_id"] == "evt_unhandled"
    assert await _processed_ids(db_session_factory) == ["evt_unhandled"]
