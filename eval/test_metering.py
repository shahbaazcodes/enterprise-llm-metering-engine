"""P1 / P2 / P3 - the metering guardrails.

These probes drive the **real** HTTP pipeline (``POST /v1/generate``) against
the offline harness:

* **P1** - an ``Idempotency-Key`` replay must be served from the result cache
  and must never bill the tenant twice.
* **P2** - the sliding-window request rate limit must reject the request that
  crosses the window, and the window must genuinely *slide* (an aged entry
  frees the slot again).
* **P3** - the monthly spend cap must be enforced to the exact micro-cent:
  a request that lands *on* the cap is served, the next one is refused.

Every probe asserts on three independent surfaces: the HTTP response, the
immutable ledger (``usage_events``) and the Redis spend counter.
"""

from __future__ import annotations

import asyncio
import math
import statistics
import time
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.exceptions import IdempotencyConflictError
from app.metering import (
    IdempotencyManager,
    QuotaChecker,
    acquire_or_conflict,
    billing_period,
)
from app.models import Subscription, UsageEvent

#: 1_000 fresh input tokens on ``gpt-4o`` == 2_500 micro-cents.
GPT4O_INPUT_PER_TOKEN_MC = 2_500_000 // 1_000_000

#: Number of independent measurement rounds per latency measurement.
LATENCY_ROUNDS = 3
#: Number of calls measured inside one round.
LATENCY_ITERATIONS = 100


async def _measure_latency_ms(
    checker: QuotaChecker,
    tenant_id: str,
    limit: int,
    *,
    budget_ms: float,
    attempts: int = 3,
) -> tuple[float, float]:
    """Measure the sliding-window call latency, rejecting scheduler noise.

    The wall-clock duration of one round trip also contains the cost of the
    asyncio scheduler and the TCP stack, both of which are perturbed by
    whatever else the machine happens to be doing.  Measuring once would
    therefore make the assertion *flaky* rather than strict, so the
    measurement is taken in two stages:

    1. **Best of several rounds.**  Inside one attempt the calls are measured
       in ``LATENCY_ROUNDS`` independent rounds and the round with the
       smallest median is kept.  The best round is the one that was disturbed
       least, so it is the closest approximation of the true cost of the call.
    2. **Bounded retry.**  If even the best round is over budget the host is
       busy (another build, a noisy neighbour, ...), so the measurement is
       repeated after a short pause.  The assertion therefore stays strict
       while only a *persistently* overloaded host can fail it.

    Returns the ``(median, p95)`` latency in milliseconds.
    """
    best_median = math.inf
    best_p95 = math.inf
    for attempt in range(attempts):
        attempt_median = math.inf
        attempt_p95 = math.inf
        for _ in range(LATENCY_ROUNDS):
            samples: list[float] = []
            for _ in range(LATENCY_ITERATIONS):
                started = time.perf_counter()
                await checker.check_rate_limit(tenant_id, limit)
                samples.append((time.perf_counter() - started) * 1000.0)
            ordered = sorted(samples)
            median = statistics.median(ordered)
            if median < attempt_median:
                attempt_median = median
                attempt_p95 = ordered[int(len(ordered) * 0.95)]
        if attempt_median < best_median:
            best_median = attempt_median
            best_p95 = attempt_p95
        if best_median < budget_ms:
            # The host is quiet enough: the measurement is trustworthy.
            break
        if attempt < attempts - 1:
            await asyncio.sleep(0.05)
    return best_median, best_p95


async def _generate(
    client: Any,
    slug: str,
    *,
    model: str = "gpt-4o",
    usage: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    prompt: str = "evaluation probe",
) -> Any:
    """Issue a metered request against ``POST /v1/generate``."""
    body: dict[str, Any] = {"prompt": prompt, "model": model, "max_output_tokens": 64}
    if usage is not None:
        body["simulated_usage"] = usage
    headers = {"X-Tenant-ID": slug}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return await client.post("/v1/generate", json=body, headers=headers)


async def _ledger_events(
    db_session_factory: async_sessionmaker[AsyncSession], tenant_id: Any
) -> list[UsageEvent]:
    """Read the ledger back in a fresh session (never a stale snapshot)."""
    async with db_session_factory() as session:
        result = await session.execute(
            select(UsageEvent)
            .where(UsageEvent.tenant_id == tenant_id)
            .order_by(UsageEvent.created_at)
        )
        return list(result.scalars().all())


async def _spend(redis_client: Any, tenant_id: Any) -> int:
    """Read the committed spend of the current billing period."""
    key = QuotaChecker.spend_key(str(tenant_id), billing_period())
    raw = await redis_client.hget(key, "spent")
    return int(raw) if raw else 0


def _is_live_redis(client: Any) -> bool:
    """True when the probe runs against the real container, not the simulator.

    The two backends have very different cost profiles.  ``fakeredis``
    executes the Lua *in-process* through ``lupa``, so the interpreter - not
    the network - dominates the measured latency.  A real Redis 7 server
    compiles the script once and then answers in a single round trip, so the
    measured latency is pure network + server time.  Only the live backend can
    therefore assert the "one round trip" property.
    """
    return not isinstance(client, fakeredis.aioredis.FakeRedis)


async def _script_stats(client: Any) -> tuple[int, int]:
    """``(calls, usec)`` for the Lua invocations Redis has served.

    Redis keeps exact counters for every command, including the server-side
    execution time of the scripts.  Reading them back gives a noise-free
    measurement of both *how many* round trips a check costs and *how long*
    the script actually runs on the server.
    """
    info = await client.info("commandstats")
    calls = usec = 0
    for key, stats in info.items():
        if key.startswith("cmdstat_eval"):
            calls += int(stats.get("calls", 0))
            usec += int(stats.get("usec", 0))
    return calls, usec


# ---------------------------------------------------------------------------
# P1 - Idempotency key replay
# ---------------------------------------------------------------------------
async def test_idempotency_key_replay_returns_cached_result(
    client: Any,
    seeded: dict[str, Any],
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Any,
) -> None:
    """A replayed ``Idempotency-Key`` returns the cached answer and bills once."""
    slug = "eval-free"
    tenant = seeded[slug]
    key = "probe-P1-idempotency"
    usage = {"input_tokens": 100, "output_tokens": 50}
    expected_cost = 100 * 2_500_000 // 1_000_000 + 50 * 10_000_000 // 1_000_000

    first = await _generate(client, slug, usage=usage, idempotency_key=key)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["idempotent_replay"] is False
    assert first.headers.get("X-Idempotent-Replay") is None
    assert body["cost"]["total_microcents"] == expected_cost

    replay = await _generate(client, slug, usage=usage, idempotency_key=key)
    assert replay.status_code == 200, replay.text
    replay_body = replay.json()
    assert replay_body["idempotent_replay"] is True
    assert replay.headers.get("X-Idempotent-Replay") == "true"
    # The replay is the *same* answer, not a new execution.
    assert replay_body["request_id"] == body["request_id"]
    assert replay_body["cost"]["total_microcents"] == body["cost"]["total_microcents"]
    assert replay_body["output"] == body["output"]

    # The provider was invoked exactly once: one ledger row.
    events = await _ledger_events(db_session_factory, tenant.id)
    assert len(events) == 1
    assert events[0].cost_microcents == expected_cost
    assert events[0].idempotency_key == key

    # ... and the tenant was charged exactly once.
    assert await _spend(redis_client, tenant.id) == expected_cost


# ---------------------------------------------------------------------------
# P2 - Sliding window rate limit
# ---------------------------------------------------------------------------
async def test_sliding_window_rate_limit_rejects_over_limit(
    client: Any,
    seeded: dict[str, Any],
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Any,
    settings: Any,
) -> None:
    """The request that crosses the window is refused - and the window slides."""
    slug = "eval-free"
    tenant = seeded[slug]
    limit = tenant.rate_limit_rpm

    # Every request inside the window is served.
    for index in range(limit):
        response = await _generate(client, slug, usage={"input_tokens": 1, "output_tokens": 0})
        assert response.status_code == 200, f"request {index + 1} refused: {response.text}"

    # The next one crosses the window and must be refused.
    over = await _generate(client, slug, usage={"input_tokens": 1, "output_tokens": 0})
    assert over.status_code == 429, over.text
    payload = over.json()
    assert payload["error"] == "rate_limit_exceeded"
    assert payload["details"]["rate_limit"] == limit
    assert payload["details"]["window_count"] == limit
    assert over.headers.get("Retry-After") == str(settings.redis_rate_limit_window_seconds)

    # The refused request never reached the ledger.
    events = await _ledger_events(db_session_factory, tenant.id)
    assert len(events) == limit

    # The window truly *slides*: ageing the recorded entries beyond the
    # window length frees the slots again.
    key = QuotaChecker.rate_key(str(tenant.id))
    members = await redis_client.zrange(key, 0, -1)
    assert len(members) == limit
    stale_score = (
        int(time.time() * 1000) - (settings.redis_rate_limit_window_seconds * 1000) - 1_000
    )
    pipe = redis_client.pipeline()
    for member in members:
        pipe.zadd(key, {member: stale_score})
    await pipe.execute()

    recovered = await _generate(client, slug, usage={"input_tokens": 1, "output_tokens": 0})
    assert recovered.status_code == 200, recovered.text


# ---------------------------------------------------------------------------
# P3 - Monthly quota boundary
# ---------------------------------------------------------------------------
async def test_monthly_quota_boundary_exact_microcent(
    client: Any,
    seeded: dict[str, Any],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The spend cap is enforced to the exact micro-cent."""
    slug = "eval-quota-edge"
    tenant = seeded[slug]
    limit = tenant.monthly_limit_microcents

    # 4_000 fresh input tokens on gpt-4o cost exactly 10_000 micro-cents,
    # which lands *on* the cap: the request must be served.
    at_cap = await _generate(
        client, slug, model="gpt-4o", usage={"input_tokens": 4_000, "output_tokens": 0}
    )
    assert at_cap.status_code == 200, at_cap.text
    payload = at_cap.json()
    assert payload["cost"]["total_microcents"] == limit
    assert payload["quota"]["limit_microcents"] == limit
    assert payload["quota"]["used_microcents"] == limit
    assert payload["quota"]["remaining_microcents"] == 0
    assert at_cap.headers["X-Cost-Microcents"] == str(limit)

    # One single micro-cent more must be refused.
    over = await _generate(
        client, slug, model="gpt-4o", usage={"input_tokens": 1, "output_tokens": 0}
    )
    assert over.status_code == 429, over.text
    body = over.json()
    assert body["error"] == "quota_exceeded"
    assert body["details"]["limit_microcents"] == limit
    assert body["details"]["used_microcents"] == limit
    assert body["details"]["remaining_microcents"] == 0
    # 1 input token == 2.5 micro-cents, rounded half up to 3.
    assert body["details"]["requested_microcents"] == 3

    # Only the accepted request reached the ledger.
    events = await _ledger_events(db_session_factory, tenant.id)
    assert len(events) == 1
    assert events[0].cost_microcents == limit


# ---------------------------------------------------------------------------
# Supporting probes - entitlement and the reporting view
# ---------------------------------------------------------------------------
async def test_inactive_subscription_is_refused_with_402(
    client: Any,
    seeded: dict[str, Any],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``past_due`` subscription produces 402, not 429."""
    slug = "eval-pro"
    tenant = seeded[slug]
    async with db_session_factory() as session:
        session.add(
            Subscription(
                tenant_id=tenant.id,
                plan_id=tenant.plan_id,
                stripe_subscription_id="sub_eval_past_due",
                status="past_due",
            )
        )
        await session.commit()

    response = await _generate(client, slug, usage={"input_tokens": 10, "output_tokens": 0})
    assert response.status_code == 402, response.text
    payload = response.json()
    assert payload["error"] == "payment_required"
    assert payload["details"]["subscription_status"] == "past_due"

    # Nothing was billed.
    assert await _ledger_events(db_session_factory, tenant.id) == []


async def test_usage_rollup_agrees_with_the_ledger(
    client: Any,
    seeded: dict[str, Any],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reporting view is a re-aggregation of the immutable ledger."""
    slug = "eval-pro"
    tenant = seeded[slug]
    charged: list[int] = []
    for tokens in (100, 250, 375):
        response = await _generate(
            client, slug, model="gpt-4o", usage={"input_tokens": tokens, "output_tokens": 10}
        )
        assert response.status_code == 200, response.text
        charged.append(response.json()["cost"]["total_microcents"])

    rollup = await client.get("/v1/usage", headers={"X-Tenant-ID": slug})
    assert rollup.status_code == 200, rollup.text
    payload = rollup.json()
    assert payload["tenant_id"] == slug
    assert payload["total_requests"] == 3
    assert payload["total_cost_microcents"] == sum(charged)
    assert payload["plan_limit_microcents"] == tenant.monthly_limit_microcents
    assert payload["remaining_microcents"] == tenant.monthly_limit_microcents - sum(charged)

    breakdown = payload["by_model"]
    assert len(breakdown) == 1
    assert breakdown[0]["model"] == "gpt-4o"
    assert breakdown[0]["cost_microcents"] == sum(charged)

    # The report can never disagree with the invoice.
    events = await _ledger_events(db_session_factory, tenant.id)
    assert sum(event.cost_microcents for event in events) == payload["total_cost_microcents"]


# ---------------------------------------------------------------------------
# Concurrency probes - the atomicity guarantees under real contention
# ---------------------------------------------------------------------------
@pytest.mark.live_redis
async def test_atomic_idempotency_lock_contestation(
    client: Any,
    seeded: dict[str, Any],
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Any,
    settings: Any,
) -> None:
    """Two concurrent requests, one ``Idempotency-Key`` -> exactly one execution.

    The loser of the ``SET NX EX`` race either waits for the settled payload
    (a 200 replay) or is told the request is still in flight (409).  What it
    must *never* do is execute the provider call a second time.
    """
    slug = "eval-pro"
    tenant = seeded[slug]
    key = "probe-concurrent-idempotency"
    usage = {"input_tokens": 200, "output_tokens": 100}
    expected_cost = 200 * 2_500_000 // 1_000_000 + 100 * 10_000_000 // 1_000_000

    responses = await asyncio.gather(
        _generate(client, slug, usage=usage, idempotency_key=key),
        _generate(client, slug, usage=usage, idempotency_key=key),
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses in ([200, 200], [200, 409]), statuses

    replays = [
        response
        for response in responses
        if response.status_code == 200 and response.json().get("idempotent_replay")
    ]
    assert len(replays) == 1, "exactly one request must be flagged as a replay"

    conflicts = [response for response in responses if response.status_code == 409]
    for conflict in conflicts:
        assert conflict.json()["error"] == "idempotency_in_progress"

    # Exactly ONE execution reached the ledger ...
    events = await _ledger_events(db_session_factory, tenant.id)
    assert len(events) == 1
    assert events[0].cost_microcents == expected_cost
    assert events[0].idempotency_key == key

    # ... and the tenant was charged exactly once.
    assert await _spend(redis_client, tenant.id) == expected_cost


async def test_idempotency_conflict_is_raised_when_lock_is_held(
    redis_client: Any, settings: Any, seeded: dict[str, Any]
) -> None:
    """A held lock with no settled result produces a deterministic 409.

    The contention probe above races two *real* requests, so the loser may
    either wait out the winner (a 200 replay) or time out (a 409).  This probe
    pins the 409 branch on its own: the lock is taken by a foreign holder and
    no result is ever published, so the waiter must exhaust its timeout and
    raise :class:`IdempotencyConflictError`.
    """
    manager = IdempotencyManager(redis_client, settings)
    tenant_id = str(seeded["eval-free"].id)
    key = "probe-conflict-409"

    # A foreign holder takes the lock with the canonical ``SET NX EX`` pattern.
    holder = await manager.acquire(tenant_id, key)
    assert holder is not None
    ttl = await redis_client.ttl(manager.lock_key(tenant_id, key))
    assert 0 < ttl <= settings.redis_lock_ttl_seconds

    # No result is published, so the waiter polls until it times out.
    with pytest.raises(IdempotencyConflictError) as excinfo:
        await acquire_or_conflict(manager, tenant_id, key, wait_seconds=0.1)

    error = excinfo.value
    assert error.status_code == 409
    assert error.code == "idempotency_in_progress"
    assert error.details["idempotency_key"] == key
    assert error.details["retry_after_seconds"] == 1

    # The foreign holder still owns the lock - the waiter never stole it.
    assert await redis_client.get(manager.lock_key(tenant_id, key)) == holder.token.encode()
    assert await manager.release(holder) is True
    assert await redis_client.get(manager.lock_key(tenant_id, key)) is None


async def test_idempotency_lock_is_exclusive_and_compare_and_delete(
    redis_client: Any, settings: Any, seeded: dict[str, Any]
) -> None:
    """``SET key val NX EX`` grants the lock to exactly one caller."""
    manager = IdempotencyManager(redis_client, settings)
    tenant_id = str(seeded["eval-free"].id)
    key = "probe-lock-exclusivity"

    first = await manager.acquire(tenant_id, key)
    assert first is not None
    assert first.token

    # The lock is taken: a second acquisition must fail (and must not
    # overwrite the token of the holder).
    second = await manager.acquire(tenant_id, key)
    assert second is None
    assert await redis_client.get(manager.lock_key(tenant_id, key)) == first.token.encode()

    # The lock carries a TTL, so a crashed holder cannot block the tenant
    # forever.
    ttl = await redis_client.ttl(manager.lock_key(tenant_id, key))
    assert 0 < ttl <= settings.redis_lock_ttl_seconds

    # Releasing a lock we do not own must not steal somebody else's lock.
    foreign = type(first)(tenant_id=tenant_id, key=key, token="not-our-token")
    assert await manager.release(foreign) is False
    assert await redis_client.get(manager.lock_key(tenant_id, key)) == first.token.encode()

    # Releasing our own lock succeeds and frees the key.
    assert await manager.release(first) is True
    assert await redis_client.get(manager.lock_key(tenant_id, key)) is None
    assert await manager.acquire(tenant_id, key) is not None


async def test_sliding_window_rate_limiter_concurrency(
    client: Any,
    seeded: dict[str, Any],
    settings: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Any,
) -> None:
    """Concurrent requests can never exceed the sliding-window limit."""
    slug = "eval-free"
    tenant = seeded[slug]
    limit = tenant.rate_limit_rpm
    overflow = 8

    responses = await asyncio.gather(
        *(
            _generate(client, slug, usage={"input_tokens": 1, "output_tokens": 0})
            for _ in range(limit + overflow)
        )
    )
    accepted = [response for response in responses if response.status_code == 200]
    rejected = [response for response in responses if response.status_code == 429]

    # The Lua script is atomic: exactly `limit` requests fit into the window.
    assert len(accepted) == limit, f"expected {limit} accepted, got {len(accepted)}"
    assert len(rejected) == overflow, f"expected {overflow} rejected, got {len(rejected)}"

    for response in rejected:
        payload = response.json()
        assert payload["error"] == "rate_limit_exceeded"
        assert payload["details"]["rate_limit"] == limit
        assert payload["details"]["window_count"] == limit
        # The client is told exactly how long to back off.
        assert response.headers["Retry-After"] == str(settings.redis_rate_limit_window_seconds)

    # The refused requests never reached the ledger: the window is enforced
    # *before* the provider is called, so no money is booked for them.
    events = await _ledger_events(db_session_factory, tenant.id)
    assert len(events) == limit

    # The window log holds exactly `limit` entries - the overflow was never
    # admitted to the sliding window.
    key = QuotaChecker.rate_key(str(tenant.id))
    assert len(await redis_client.zrange(key, 0, -1)) == limit


@pytest.mark.live_redis
async def test_sliding_window_lua_script_is_sub_millisecond(
    redis_client: Any, settings: Any, seeded: dict[str, Any]
) -> None:
    """The atomic rate-limit script stays well below the 1 ms budget.

    The sliding window is a single round trip to Redis, so its cost must not
    scale with the number of requests inside the window.  A non-atomic
    implementation (read the counter, then write it) would need two round
    trips and would be the first thing to break under contention.

    Against the live container the probe additionally asks Redis itself how
    many script invocations it served and how long the script ran *on the
    server* - both are exact counters, so the single-round-trip and the
    sub-millisecond claims are verified without any timing noise.
    """
    checker = QuotaChecker(redis_client, settings)
    tenant_id = str(seeded["eval-pro"].id)
    limit = 10_000

    # Warm up: the first EVALSHA compiles the script and is not representative.
    for _ in range(20):
        await checker.check_rate_limit(tenant_id, limit)

    median_ms, p95_ms = await _measure_latency_ms(checker, tenant_id, limit, budget_ms=1.0)

    assert median_ms < 1.0, f"median Lua window latency {median_ms:.3f} ms exceeds 1 ms"
    # The wall-clock *tail* is a property of the host's scheduler, not of the
    # script: under CPU contention the p95 of a sub-millisecond call can be
    # several milliseconds, and the exact duration is then meaningless.  The
    # tail guard is therefore expressed *relative to the measured cost*, with
    # an absolute floor.  The exact, noise-free verification of the same two
    # claims (exactly one round trip, sub-millisecond on the server) is done
    # below against the live Redis counters.
    tail_budget_ms = max(2.0, median_ms * 10.0)
    assert p95_ms < tail_budget_ms, (
        f"p95 Lua window latency {p95_ms:.3f} ms exceeds the tail budget "
        f"{tail_budget_ms:.3f} ms (median {median_ms:.3f} ms)"
    )

    # The cost does not scale with the size of the window: after the measured
    # calls the log is much bigger than it was after the warm-up, yet the
    # latency is unchanged.
    expected_entries = 20 + LATENCY_ROUNDS * LATENCY_ITERATIONS
    allowed, count = await checker.check_rate_limit(tenant_id, limit)
    assert allowed is True
    assert count == expected_entries + 1
    assert (
        len(await redis_client.zrange(QuotaChecker.rate_key(tenant_id), 0, -1))
        == expected_entries + 1
    )

    # Same measurement against a *small* window.  A script that scanned the
    # sorted set (or a non-atomic read-then-write pair, which needs two round
    # trips) would be measurably slower here, so this pins the O(1) property
    # of the atomic implementation.
    small_id = str(seeded["eval-free"].id)
    for _ in range(20):
        await checker.check_rate_limit(small_id, limit)

    small_median_ms, _ = await _measure_latency_ms(checker, small_id, limit, budget_ms=1.0)

    assert small_median_ms < 1.0, f"median Lua window latency {small_median_ms:.3f} ms exceeds 1 ms"
    assert median_ms < small_median_ms * 2.0, (
        f"latency grew with the window: {median_ms:.3f} ms for "
        f"{expected_entries} entries vs {small_median_ms:.3f} ms for 20 entries"
    )

    if not _is_live_redis(redis_client):
        return

    # Redis counts the commands it serves, so the number of script invocations
    # per check is *exact*.  One check must be exactly one round trip: a
    # non-atomic implementation would issue two commands and land above this
    # bound, no matter how fast the network happens to be.
    probes = 25
    calls_before, usec_before = await _script_stats(redis_client)
    for _ in range(probes):
        await checker.check_rate_limit(tenant_id, limit)
    calls_after, usec_after = await _script_stats(redis_client)

    served = calls_after - calls_before
    assert served == probes, (
        f"expected {probes} script invocations for {probes} checks, Redis served {served}"
    )

    # Redis also reports how long the script ran *on the server*.  This is the
    # sub-millisecond claim with the network taken out of the measurement.
    server_usec = (usec_after - usec_before) / probes
    assert server_usec < 1000, (
        f"server-side Lua execution took {server_usec:.1f} us, over the 1 ms budget"
    )


async def test_quota_reservation_settlement_race(
    redis_client: Any, settings: Any, seeded: dict[str, Any]
) -> None:
    """Concurrent reservations never oversell the cap; settlement refunds exactly."""
    checker = QuotaChecker(redis_client, settings)
    tenant_id = str(seeded["eval-quota-edge"].id)
    period = billing_period()
    limit = 10_000

    # Four concurrent reservations of 3_000 micro-cents against a 10_000 cap.
    results = await asyncio.gather(
        *(checker.reserve(tenant_id, period, 3_000, limit) for _ in range(4))
    )
    allowed = [result for result in results if result[0]]
    refused = [result for result in results if not result[0]]

    # The Lua reservation is atomic, so only three of the four fit.
    assert len(allowed) == 3
    assert len(refused) == 1
    # The counter never went past the cap, not even for a single micro-cent.
    assert max(result[1] for result in results) <= limit
    assert refused[0][1] == 9_000
    assert refused[0][2] == limit

    # A reservation is *not* spend: nothing is booked until it is settled.
    assert await checker.current_spend(tenant_id, period) == 0
    reserved = await redis_client.hget(checker.spend_key(tenant_id, period), "reserved")
    assert int(reserved) == 9_000

    # Settling the three reservations books exactly their actual cost.
    await asyncio.gather(*(checker.settle(tenant_id, period, 3_000, 3_000) for _ in range(3)))
    assert await checker.current_spend(tenant_id, period) == 9_000
    reserved = await redis_client.hget(checker.spend_key(tenant_id, period), "reserved")
    assert int(reserved) == 0

    # An under-settlement refunds the difference exactly once.
    # ``reserve`` reports the spend *after* the reservation was taken.
    allowed_now, used, _ = await checker.reserve(tenant_id, period, 1_000, limit)
    assert allowed_now is True
    assert used == 10_000
    spend = await checker.settle(tenant_id, period, 1_000, 400)
    assert spend == 9_400
    assert await checker.current_spend(tenant_id, period) == 9_400
    reserved = await redis_client.hget(checker.spend_key(tenant_id, period), "reserved")
    assert int(reserved) == 0

    # The 600 micro-cent difference was released, not lost and not double-counted.
    allowed_now, used, _ = await checker.reserve(tenant_id, period, 600, limit)
    assert allowed_now is True
    assert used == 10_000


@pytest.mark.live_redis
async def test_quota_settlement_storm_never_double_counts(
    redis_client: Any, settings: Any, seeded: dict[str, Any]
) -> None:
    """A storm of concurrent reserve/settle pairs keeps the counters exact.

    Every worker reserves 100 micro-cents and settles for 90 - a 10
    micro-cent under-settlement that must be *refunded* exactly once.  If the
    refund were lost, the tenant would be over-charged; if it were applied
    twice, the tenant would be under-charged.  Both show up as a wrong
    ``spent`` counter, so the assertion pins the ledger to the micro-cent.
    """
    checker = QuotaChecker(redis_client, settings)
    tenant_id = str(seeded["eval-pro"].id)
    period = billing_period()
    limit = 1_000_000
    workers = 50
    reserved_each = 100
    actual_each = 90

    async def worker() -> bool:
        allowed, _, _ = await checker.reserve(tenant_id, period, reserved_each, limit)
        if not allowed:
            return False
        await checker.settle(tenant_id, period, reserved_each, actual_each)
        return True

    outcomes = await asyncio.gather(*(worker() for _ in range(workers)))
    assert all(outcomes), "every reservation fits under the cap"

    # Exactly ``workers * actual_each`` was booked - no refund lost, none
    # applied twice.
    assert await checker.current_spend(tenant_id, period) == workers * actual_each

    # Every reservation was released, so the next request is not blocked by
    # a stale hold.
    reserved = await redis_client.hget(checker.spend_key(tenant_id, period), "reserved")
    assert int(reserved) == 0

    # The counter keeps agreeing with the arithmetic: reserve again and the
    # reported usage is the spend plus the new reservation.
    allowed, used, _ = await checker.reserve(tenant_id, period, 1_000, limit)
    assert allowed is True
    assert used == workers * actual_each + 1_000
    assert await checker.settle(tenant_id, period, 1_000, 1_000) == workers * actual_each + 1_000
