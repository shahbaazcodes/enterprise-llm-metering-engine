"""Shared fixtures for the offline evaluation suite.

The harness replaces the two stateful dependencies of the service with
in-process equivalents that behave identically:

* **PostgreSQL** -> a throw-away SQLite file created from the ORM metadata
  (``app.db.create_all``).  The ORM models are already dialect-portable.
* **Redis** -> ``fakeredis`` compiled against ``lupa``, so the server-side
  Lua scripts (the sliding window, the spend reservation, the lock release)
  are executed by a *real* Lua interpreter instead of being stubbed out.

Everything else - the routers, the pricing engine, the anomaly guard, the
Stripe verification - is the production code path, untouched.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
import uuid

import fakeredis.aioredis
import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict
import pytest
import redis.asyncio as redis
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import db as app_db, redis_client as app_redis
from app.deps import hash_api_key
from app.main import create_app
from app.ml_guard import AnomalyGuard
from app.models import ModelPricing, Plan, Tenant
from app.providers import SimulatedProvider
from app.settings import Settings, get_settings

EVAL_DIR = Path(__file__).resolve().parent
EVAL_SET_PATH = EVAL_DIR / "eval_set.json"

#: A fixed, offline-only Stripe signing secret.  It is a test double - the
#: real secret is supplied through ``STRIPE_WEBHOOK_SECRET`` in deployment.
STRIPE_WEBHOOK_SECRET = "whsec_eval_offline_secret"

#: Deterministic Stripe price ids wired to the plan catalogue.
STRIPE_PRICE_PRO = "price_eval_pro"
STRIPE_PRICE_FREE = "price_eval_free"


# ---------------------------------------------------------------------------
# Scenario matrix
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def eval_set() -> dict[str, Any]:
    """The parsed ``eval_set.json`` scenario matrix."""
    return json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))


def api_key_for(slug: str) -> str:
    """The deterministic API key belonging to a seeded tenant."""
    return f"sk_eval_{slug.replace('-', '_')}"


@dataclass(frozen=True, slots=True)
class SeededTenant:
    """A tenant seeded from the scenario matrix (detached from its session)."""

    slug: str
    id: uuid.UUID
    plan_id: uuid.UUID
    plan_code: str
    monthly_limit_microcents: int
    rate_limit_rpm: int
    cached_input_discount_bps: int
    api_key: str
    stripe_customer_id: str


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@pytest.fixture
def settings() -> Settings:
    """Runtime configuration for the offline harness.

    Every knob that could make a probe non-deterministic is pinned here:
    the API key check is off (the probes address tenants by slug), the usage
    override is on (so token counters can be pinned exactly) and the anomaly
    guard is disabled (it would otherwise fit a scikit-learn model mid-suite
    and make the assertions depend on the sample order).
    """
    return Settings(
        environment="test",
        api_v1_prefix="/v1",
        api_key_required=False,
        allow_usage_override=True,
        auto_migrate=False,
        auto_seed=False,
        ml_guard_enabled=False,
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://offline-harness/0",
        redis_rate_limit_window_seconds=60,
        stripe_secret_key="",
        stripe_webhook_secret=STRIPE_WEBHOOK_SECRET,
        stripe_price_pro=STRIPE_PRICE_PRO,
        stripe_price_free=STRIPE_PRICE_FREE,
        stripe_webhook_tolerance_seconds=300,
        log_level="WARNING",
    )


# ---------------------------------------------------------------------------
# Redis (fakeredis + real Lua, or the live container)
# ---------------------------------------------------------------------------
#: Redis database index reserved for the live-container probes.  The service
#: itself talks to DB 0 (see ``REDIS_URL`` in ``.env``), so the live probes get
#: a database of their own and can never flush the development data.
LIVE_REDIS_DB = 15

#: The project root - used to locate ``.env`` whatever the working directory.
PROJECT_ROOT = EVAL_DIR.parent


class _LiveRedisConfig(BaseSettings):
    """Reads ``REDIS_URL`` only - the address of the live Redis container."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    redis_url: str = "redis://localhost:6379/0"


def live_redis_url() -> str:
    """The live-probe database: the configured Redis on a dedicated index."""
    parts = urlsplit(_LiveRedisConfig().redis_url)
    return urlunsplit(parts._replace(path=f"/{LIVE_REDIS_DB}"))


@asynccontextmanager
async def _live_redis_client() -> AsyncIterator[redis.Redis]:
    """A client bound to the live Redis container, isolated in its own DB."""
    url = live_redis_url()
    client = redis.Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - any failure means the container is down
        await client.aclose()
        pytest.skip(
            f"live Redis is not reachable at {url} ({exc!r}); start it with `docker compose up -d`"
        )

    await client.flushdb()
    app_redis.set_redis_override(client)
    try:
        yield client
    finally:
        app_redis.set_redis_override(None)
        try:
            await client.flushdb()
        finally:
            await client.aclose()


@pytest.fixture
async def redis_client(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """The Redis client the probes run against.

    By default this is an in-process ``fakeredis`` that executes the
    server-side Lua through ``lupa``, so the suite stays hermetic and offline.

    A test marked ``live_redis`` instead gets a client bound to the **real**
    Redis container (``docker compose up -d``).  That is what actually proves
    the atomicity of the distributed lock and the sub-millisecond budget of
    the sliding window on the production engine rather than on a simulator.
    """
    if request.node.get_closest_marker("live_redis") is not None:
        async with _live_redis_client() as client:
            yield client
        return

    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    app_redis.set_redis_override(client)
    try:
        yield client
    finally:
        app_redis.set_redis_override(None)
        await client.aclose()


# ---------------------------------------------------------------------------
# Database (SQLite)
# ---------------------------------------------------------------------------
@pytest.fixture
async def db_engine(settings: Settings, tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A throw-away SQLite database built from the ORM metadata."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}"
    engine = app_db.init_engine(settings, url=url)

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        # WAL keeps the writers (the API) and the readers (the assertions)
        # from blocking each other; the busy timeout absorbs the tiny races.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    await app_db.create_all(engine)
    try:
        yield engine
    finally:
        await app_db.dispose_engine()


@pytest.fixture
async def db_session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Factory for short-lived sessions used by the assertions.

    A *fresh* session is opened for every assertion block so that a probe
    can never observe a stale transaction snapshot.
    """
    return async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------
@pytest.fixture
async def seeded(
    db_session_factory: async_sessionmaker[AsyncSession], eval_set: dict[str, Any]
) -> dict[str, SeededTenant]:
    """Seed the plans, price cards and tenants declared by the scenario matrix."""
    # A plan label may be shared by two tenants that nevertheless declare
    # *different* spend caps and rate limits (``free`` is such a case).  The
    # second occurrence gets a dedicated plan code so that every tenant keeps
    # exactly the limits written in the manifest.
    plan_specs: dict[str, dict[str, Any]] = {}
    plan_codes: dict[str, str] = {}
    for spec in eval_set["tenants"]:
        code = spec["plan"]
        existing = plan_specs.get(code)
        if existing is not None and (
            existing["monthly_limit_microcents"] != spec["monthly_limit_microcents"]
            or existing["rate_limit_rpm"] != spec["rate_limit_rpm"]
        ):
            code = spec["slug"].replace("-", "_")
        plan_specs[code] = spec
        plan_codes[spec["slug"]] = code

    async with db_session_factory() as session:
        plans: dict[str, Plan] = {}
        for code, spec in plan_specs.items():
            plan = Plan(
                code=code,
                name=code.replace("_", " ").title(),
                description=spec.get("description", ""),
                monthly_limit_microcents=spec["monthly_limit_microcents"],
                rate_limit_rpm=spec["rate_limit_rpm"],
                cached_input_discount_bps=spec["cached_input_discount_bps"],
                reasoning_included_in_output=True,
                stripe_price_id={
                    "free": STRIPE_PRICE_FREE,
                    "pro": STRIPE_PRICE_PRO,
                }.get(code),
            )
            session.add(plan)
            plans[code] = plan
        await session.flush()

        for entry in eval_set["pricing_models"]:
            session.add(
                ModelPricing(
                    model=entry["model"],
                    provider=entry["provider"],
                    input_microcents_per_million=entry["input_microcents_per_million"],
                    output_microcents_per_million=entry["output_microcents_per_million"],
                    cached_input_microcents_per_million=entry[
                        "cached_input_microcents_per_million"
                    ],
                )
            )

        tenants: dict[str, SeededTenant] = {}
        for spec in eval_set["tenants"]:
            slug = spec["slug"]
            code = plan_codes[slug]
            key = api_key_for(slug)
            customer_id = f"cus_{slug.replace('-', '_')}"
            tenant = Tenant(
                slug=slug,
                name=slug,
                email=f"{slug}@eval.local",
                api_key_hash=hash_api_key(key),
                plan_id=plans[code].id,
                stripe_customer_id=customer_id,
                status="active",
            )
            session.add(tenant)
            await session.flush()
            tenants[slug] = SeededTenant(
                slug=slug,
                id=tenant.id,
                plan_id=tenant.plan_id,
                plan_code=code,
                monthly_limit_microcents=spec["monthly_limit_microcents"],
                rate_limit_rpm=spec["rate_limit_rpm"],
                cached_input_discount_bps=spec["cached_input_discount_bps"],
                api_key=key,
                stripe_customer_id=customer_id,
            )
        await session.commit()
    return tenants


# ---------------------------------------------------------------------------
# Application under test
# ---------------------------------------------------------------------------
@pytest.fixture
async def app(settings: Settings, redis_client: Any) -> Any:
    """The FastAPI application, wired to the offline dependencies."""
    application = create_app(settings=settings)
    application.dependency_overrides[get_settings] = lambda: settings
    # The lifespan normally installs these; ASGI transports do not run it.
    application.state.anomaly_guard = AnomalyGuard(settings)
    application.state.provider = SimulatedProvider(settings.default_model)
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client bound to the application under test."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://eval.local") as http:
        yield http


# ---------------------------------------------------------------------------
# Helpers shared by the probes
# ---------------------------------------------------------------------------
async def fetch_tenant(
    db_session_factory: async_sessionmaker[AsyncSession], slug: str
) -> Tenant | None:
    """Load a tenant (and its plan) in a fresh session."""
    async with db_session_factory() as session:
        return (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalars().first()
