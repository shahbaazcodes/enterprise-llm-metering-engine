"""Canonical exception hierarchy of the metering engine.

Every typed application error is defined **exactly once**, here, and is
mapped onto an HTTP status code by :func:`app.main._register_exception_handlers`.
There is deliberately no second, parallel hierarchy anywhere in the package:
Python matches ``except`` clauses by *identity*, so a duplicated class would
silently break a handler written against the other path.

The distinction between the two rejection codes is deliberate and is part of
the public contract of this service:

* **402 Payment Required** - the tenant has no valid entitlement
  (subscription missing, ``past_due``, ``canceled``, ``unpaid``).  The
  client must fix billing before retrying.
* **429 Too Many Requests** - the tenant *is* entitled but has exhausted a
  quota (monthly spend cap or request-rate limit).  The client may retry
  later, possibly on a larger plan.

Usage::

    from app.exceptions import PaymentRequiredError, QuotaExceededError

Status-code contract
--------------------
==============================  ======  ==================================
Exception                        HTTP    Raised when
==============================  ======  ==================================
``RateLimitExceededError``       429     Sliding-window request rate exhausted
``QuotaExceededError``           429     Monthly spend cap exhausted
``PaymentRequiredError``         402     Subscription inactive / past due
``IdempotencyConflictError``     409     Same Idempotency-Key still in flight
``InvalidWebhookSignatureError`` 400     Stripe signature did not verify
``UnknownModelError``            400     Model has no price card
``InvalidRequestError``          400     Request not allowed as sent
``TenantNotFoundError``          404     Unknown ``X-Tenant-ID``
``AuthenticationError``          401     Missing / invalid bearer token
==============================  ======  ==================================
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AuthenticationError",
    "EntitlementError",
    "IdempotencyConflictError",
    "InvalidRequestError",
    "InvalidWebhookSignatureError",
    "MeteringError",
    "PaymentRequiredError",
    "QuotaExceededError",
    "RateLimitExceededError",
    "TenantNotFoundError",
    "UnknownModelError",
]


class MeteringError(Exception):
    """Base class for every error raised by the metering plane."""

    status_code: int = 500
    code: str = "metering_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def to_payload(self) -> dict[str, Any]:
        """JSON body returned to the client."""
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
        }


class TenantNotFoundError(MeteringError):
    status_code = 404
    code = "tenant_not_found"


class AuthenticationError(MeteringError):
    status_code = 401
    code = "unauthenticated"


class PaymentRequiredError(MeteringError):
    """402 - the subscription is inactive, past due or cancelled."""

    status_code = 402
    code = "payment_required"


class QuotaExceededError(MeteringError):
    """429 - the monthly spend cap for the current period is exhausted."""

    status_code = 429
    code = "quota_exceeded"


class RateLimitExceededError(MeteringError):
    """429 - the sliding-window request rate limit is exhausted."""

    status_code = 429
    code = "rate_limit_exceeded"


class IdempotencyConflictError(MeteringError):
    """409 - a concurrent request with the same idempotency key is in flight."""

    status_code = 409
    code = "idempotency_in_progress"


class InvalidWebhookSignatureError(MeteringError):
    """400 - the Stripe signature header did not verify."""

    status_code = 400
    code = "invalid_webhook_signature"


class UnknownModelError(MeteringError):
    """400 - the requested model has no price card."""

    status_code = 400
    code = "unknown_model"


class InvalidRequestError(MeteringError):
    """400 - the request is syntactically valid but not allowed as sent."""

    status_code = 400
    code = "invalid_request"


#: Alias kept for callers that prefer the more explicit "entitlement" wording.
EntitlementError = PaymentRequiredError
