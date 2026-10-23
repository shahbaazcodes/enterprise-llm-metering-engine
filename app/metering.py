"""Redis-backed idempotency locks, quota enforcement and entitlement checks.

Concurrency model
-----------------
Every state transition that must be race free is executed **inside Redis**
with a server-side Lua script, so the check and the mutation can never be
interleaved by another replica:

* ``reserve_spend`` - read the period spend + reservations, compare against
  the plan cap and reserve the requested amount, all in one atomic step.
* ``settle_spend``  - release the reservation and book the actual cost.
* ``sliding_window``- prune the expired entries of a sorted set and append
  the current request in one atomic step (a true sliding-window log, O(log n)
  and a single round trip - comfortably sub-millisecond on a local socket).

The idempotency lock is the canonical ``SET key value NX EX 300`` pattern:
the lock carries a random token, and the release is a compare-and-delete so
a slow request can never delete a lock that has already been handed to
somebody else.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import time
import uuid

from redis.asyncio import Redis

from app import metrics
from app.exceptions import (
    IdempotencyConflictError,
    PaymentRequiredError,
    QuotaExceededError,
    RateLimitExceededError,
)
from app.pricing import load_price_card
from app.settings import Settings, get_settings

__all__ = [
    "ACTIVE_SUBSCRIPTION_STATUSES",
    "Entitlement",
    "IdempotencyLock",
    "IdempotencyManager",
    "QuotaChecker",
    "QuotaDecision",
    "acquire_or_conflict",
    "billing_period",
    "enforce_entitlement",
    "enforce_quota",
    "load_price_card",
    "resolve_entitlement",
]

logger = logging.getLogger("llm.metering.metering")

ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})

# ---------------------------------------------------------------------------
# Server-side Lua scripts
# ---------------------------------------------------------------------------
LUA_RELEASE_LOCK = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""

LUA_RESERVE_SPEND = """
local limit = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])
local spent = tonumber(redis.call("HGET", KEYS[1], "spent") or "0")
local reserved = tonumber(redis.call("HGET", KEYS[1], "reserved") or "0")
local used = spent + reserved
if limit > 0 and used + amount > limit then
    return {0, used, limit}
end
redis.call("HINCRBY", KEYS[1], "reserved", amount)
redis.call("EXPIRE", KEYS[1], tonumber(ARGV[3]))
return {1, used + amount, limit}
"""

LUA_SETTLE_SPEND = """
local reserved_amount = tonumber(ARGV[1])
local actual = tonumber(ARGV[2])
local reserved = tonumber(redis.call("HGET", KEYS[1], "reserved") or "0")
local remaining = reserved - reserved_amount
if remaining < 0 then
    remaining = 0
end
redis.call("HSET", KEYS[1], "reserved", remaining)
if actual > 0 then
    redis.call("HINCRBY", KEYS[1], "spent", actual)
end
redis.call("EXPIRE", KEYS[1], tonumber(ARGV[3]))
return tonumber(redis.call("HGET", KEYS[1], "spent") or "0")
"""

LUA_SLIDING_WINDOW = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", now - window)
local count = redis.call("ZCARD", KEYS[1])
if limit > 0 and count >= limit then
    redis.call("EXPIRE", KEYS[1], math.ceil(window / 1000) + 1)
    return {0, count}
end
redis.call("ZADD", KEYS[1], now, ARGV[4])
redis.call("EXPIRE", KEYS[1], math.ceil(window / 1000) + 1)
return {1, count + 1}
"""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QuotaDecision:
    """Outcome of a quota evaluation."""

    allowed: bool
    reason: str
    limit_microcents: int = 0
    used_microcents: int = 0
    requested_microcents: int = 0
    remaining_microcents: int = 0
    rate_limit: int = 0
    window_count: int = 0

    def to_dict(self) -> dict[str, int | str | bool]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "limit_microcents": self.limit_microcents,
            "used_microcents": self.used_microcents,
            "requested_microcents": self.requested_microcents,
            "remaining_microcents": self.remaining_microcents,
            "rate_limit": self.rate_limit,
            "window_count": self.window_count,
        }


@dataclass(frozen=True, slots=True)
class Entitlement:
    """Resolved billing entitlement of a tenant."""

    allowed: bool
    status: str
    subscription_status: str | None = None
    plan_code: str = "free"
    is_trial: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "status": self.status,
            "subscription_status": self.subscription_status,
            "plan_code": self.plan_code,
            "is_trial": self.is_trial,
        }


@dataclass(slots=True)
class IdempotencyLock:
    """A held idempotency lock."""

    tenant_id: str
    key: str
    token: str
    acquired_at: float = field(default_factory=time.monotonic)

    @property
    def redis_key(self) -> str:
        return f"mm:idem:lock:{self.tenant_id}:{self.key}"


def billing_period(now: datetime | None = None) -> str:
    """Return the current UTC billing period key (``YYYY-MM``)."""
    moment = now or datetime.now(UTC)
    return moment.strftime("%Y-%m")


def _seconds_until_next_period(now: datetime | None = None) -> int:
    """Seconds remaining in the current billing period (+ one day of grace)."""
    moment = now or datetime.now(UTC)
    if moment.month == 12:
        start = moment.replace(
            year=moment.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        start = moment.replace(
            month=moment.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return int((start - moment).total_seconds()) + 86_400


class IdempotencyManager:
    """Atomic distributed idempotency locks plus a result cache.

    ``acquire`` implements the canonical ``SET key value NX EX 300`` pattern.
    ``release`` performs a compare-and-delete so a request that outlived its
    lock can never delete the lock owned by the next request.
    """

    def __init__(self, redis: Redis, settings: Settings | None = None) -> None:
        self._redis = redis
        self._settings = settings or get_settings()
        self._lock_ttl = self._settings.redis_lock_ttl_seconds
        self._result_ttl = self._settings.redis_idempotency_ttl_seconds
        self._release_script = redis.register_script(LUA_RELEASE_LOCK)

    @staticmethod
    def lock_key(tenant_id: str, key: str) -> str:
        return f"mm:idem:lock:{tenant_id}:{key}"

    @staticmethod
    def result_key(tenant_id: str, key: str) -> str:
        return f"mm:idem:result:{tenant_id}:{key}"

    async def acquire(self, tenant_id: str, key: str) -> IdempotencyLock | None:
        """Try to take the lock. ``None`` means somebody else holds it."""
        token = uuid.uuid4().hex
        acquired = await self._redis.set(
            self.lock_key(tenant_id, key), token, nx=True, ex=self._lock_ttl
        )
        if not acquired:
            return None
        metrics.ACTIVE_IDEMPOTENCY_LOCKS.inc()
        return IdempotencyLock(tenant_id=tenant_id, key=key, token=token)

    async def release(self, lock: IdempotencyLock) -> bool:
        """Release a lock we own. ``True`` when the lock was actually removed."""
        try:
            deleted = await self._release_script(keys=[lock.redis_key], args=[lock.token])
            return bool(int(deleted))
        except Exception:  # noqa: BLE001 - a stuck lock expires on its own
            logger.warning("failed to release idempotency lock", exc_info=True)
            return False
        finally:
            metrics.ACTIVE_IDEMPOTENCY_LOCKS.dec()

    async def store_result(
        self, tenant_id: str, key: str, payload: bytes, ttl: int | None = None
    ) -> None:
        """Cache the response so that a retry can be served without re-billing."""
        await self._redis.set(self.result_key(tenant_id, key), payload, ex=ttl or self._result_ttl)

    async def get_result(self, tenant_id: str, key: str) -> bytes | None:
        """Return the cached response payload, if any."""
        return await self._redis.get(self.result_key(tenant_id, key))

    async def wait_for_result(
        self, tenant_id: str, key: str, timeout: float = 2.0, interval: float = 0.05
    ) -> bytes | None:
        """Poll for the result of a concurrent in-flight request."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            payload = await self.get_result(tenant_id, key)
            if payload:
                return payload
            await asyncio.sleep(interval)
        return None


class QuotaChecker:
    """Spend-cap and request-rate enforcement backed by Redis.

    Both checks are executed by Lua scripts so that the read and the write
    are a single atomic operation - two concurrent requests can never both
    observe "just under the limit" and both proceed.
    """

    def __init__(self, redis: Redis, settings: Settings | None = None) -> None:
        self._redis = redis
        self._settings = settings or get_settings()
        self._window_seconds = self._settings.redis_rate_limit_window_seconds
        self._reserve_script = redis.register_script(LUA_RESERVE_SPEND)
        self._settle_script = redis.register_script(LUA_SETTLE_SPEND)
        self._slide_script = redis.register_script(LUA_SLIDING_WINDOW)

    @staticmethod
    def spend_key(tenant_id: str, period: str) -> str:
        return f"mm:spend:{tenant_id}:{period}"

    @staticmethod
    def rate_key(tenant_id: str) -> str:
        return f"mm:rl:{tenant_id}"

    async def check_rate_limit(
        self, tenant_id: str, limit: int, member: str | None = None
    ) -> tuple[bool, int]:
        """Sliding-window log check. Returns ``(allowed, window_count)``."""
        now_ms = int(time.time() * 1000)
        window_ms = self._window_seconds * 1000
        entry = member or f"{now_ms}-{uuid.uuid4().hex}"
        result = await self._slide_script(
            keys=[self.rate_key(tenant_id)],
            args=[now_ms, window_ms, limit, entry],
        )
        return bool(int(result[0])), int(result[1])

    async def reserve(
        self, tenant_id: str, period: str, amount: int, limit: int
    ) -> tuple[bool, int, int]:
        """Atomically reserve ``amount`` against the period spend cap.

        Returns ``(allowed, used_after_reservation, limit)``.
        """
        result = await self._reserve_script(
            keys=[self.spend_key(tenant_id, period)],
            args=[limit, amount, _seconds_until_next_period()],
        )
        return bool(int(result[0])), int(result[1]), int(result[2])

    async def settle(
        self, tenant_id: str, period: str, reserved_amount: int, actual_amount: int
    ) -> int:
        """Release a reservation and book the actual cost. Returns the new spend."""
        result = await self._settle_script(
            keys=[self.spend_key(tenant_id, period)],
            args=[reserved_amount, actual_amount, _seconds_until_next_period()],
        )
        return int(result)

    async def current_spend(self, tenant_id: str, period: str) -> int:
        """Read the committed spend for a billing period."""
        raw = await self._redis.hget(self.spend_key(tenant_id, period), "spent")
        return int(raw) if raw else 0

    async def reset(self, tenant_id: str, period: str) -> None:
        """Drop the spend counter (administrative / test helper)."""
        await self._redis.delete(self.spend_key(tenant_id, period))

    async def reset_rate_limit(self, tenant_id: str) -> None:
        """Drop the sliding-window log (administrative / test helper)."""
        await self._redis.delete(self.rate_key(tenant_id))


# ---------------------------------------------------------------------------
# Entitlement resolution
# ---------------------------------------------------------------------------
async def resolve_entitlement(session, tenant) -> Entitlement:
    """Resolve the billing entitlement of a tenant.

    A tenant without any subscription row is a *free* tenant: free plans are
    granted implicitly, they simply have a low spend cap.  A tenant whose
    subscription exists but is not active (``past_due``, ``canceled``,
    ``unpaid``, ...) is **not** entitled and receives ``402``.
    """
    from sqlalchemy import select

    from app.models import Subscription

    plan_code = tenant.plan.code if tenant.plan is not None else "free"
    subscription = (
        (
            await session.execute(
                select(Subscription)
                .where(Subscription.tenant_id == tenant.id)
                .order_by(Subscription.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    if subscription is None:
        return Entitlement(allowed=True, status="free", plan_code=plan_code)
    if subscription.status in ACTIVE_SUBSCRIPTION_STATUSES:
        return Entitlement(
            allowed=True,
            status=subscription.status,
            subscription_status=subscription.status,
            plan_code=plan_code,
            is_trial=subscription.status == "trialing",
        )
    return Entitlement(
        allowed=False,
        status=subscription.status,
        subscription_status=subscription.status,
        plan_code=plan_code,
    )


def enforce_entitlement(entitlement: Entitlement, tenant_slug: str) -> None:
    """Raise ``402 Payment Required`` when the tenant is not entitled."""
    if entitlement.allowed:
        return
    metrics.PAYMENT_REQUIRED_TOTAL.labels(
        subscription_status=entitlement.subscription_status or "unknown"
    ).inc()
    raise PaymentRequiredError(
        f"Subscription for tenant '{tenant_slug}' is {entitlement.status}; "
        "payment is required before further usage.",
        tenant=tenant_slug,
        subscription_status=entitlement.status,
        plan_code=entitlement.plan_code,
    )


def enforce_quota(decision: QuotaDecision, tenant_slug: str) -> None:
    """Raise ``429 Too Many Requests`` when a quota is exhausted."""
    if decision.allowed:
        return
    if decision.reason == "rate_limited":
        metrics.RATE_LIMIT_REJECTIONS_TOTAL.labels(rejection_type="rate_limited").inc()
        raise RateLimitExceededError(
            f"Request rate limit exceeded for tenant '{tenant_slug}'.",
            tenant=tenant_slug,
            rate_limit=decision.rate_limit,
            window_count=decision.window_count,
            retry_after_seconds=30,
        )
    metrics.RATE_LIMIT_REJECTIONS_TOTAL.labels(rejection_type="quota_exceeded").inc()
    raise QuotaExceededError(
        f"Monthly spend quota exhausted for tenant '{tenant_slug}'.",
        tenant=tenant_slug,
        limit_microcents=decision.limit_microcents,
        used_microcents=decision.used_microcents,
        requested_microcents=decision.requested_microcents,
        remaining_microcents=decision.remaining_microcents,
    )


async def acquire_or_conflict(
    manager: IdempotencyManager, tenant_id: str, key: str, wait_seconds: float = 2.0
) -> tuple[IdempotencyLock | None, bytes | None]:
    """Acquire the lock or return the result of the concurrent request.

    Returns ``(lock, None)`` when the lock was taken, ``(None, payload)``
    when a concurrent request already produced the answer, and raises
    ``409`` when the request is still in flight after ``wait_seconds``.
    """
    existing = await manager.get_result(tenant_id, key)
    if existing:
        metrics.IDEMPOTENT_REPLAYS_TOTAL.inc()
        return None, existing

    lock = await manager.acquire(tenant_id, key)
    if lock is not None:
        return lock, None

    payload = await manager.wait_for_result(tenant_id, key, timeout=wait_seconds)
    if payload:
        metrics.IDEMPOTENT_REPLAYS_TOTAL.inc()
        return None, payload

    raise IdempotencyConflictError(
        "A request with the same Idempotency-Key is currently in progress.",
        tenant_id=tenant_id,
        idempotency_key=key,
        retry_after_seconds=1,
    )
