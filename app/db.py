"""Async SQLAlchemy engine, session lifecycle and schema management.

Responsibilities
----------------
* Build a pooled :class:`AsyncEngine` tuned for an ASGI workload
  (``pool_pre_ping`` + recycle guards against stale connections behind
  PgBouncer / a rolling restart).
* Expose the :func:`get_session` FastAPI dependency and the
  :func:`session_scope` async context manager for background work.
* Apply the SQL migrations in ``db/migrations`` exactly once, tracked by the
  ``schema_migrations`` table with a SHA-256 checksum.
* Seed the reference data (plans + price book) from configuration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hashlib
import logging
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models import Base, ModelPricing, Plan
from app.settings import Settings, get_settings

logger = logging.getLogger("llm.metering.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


# ---------------------------------------------------------------------------
# Engine / session lifecycle
# ---------------------------------------------------------------------------
def init_engine(settings: Settings | None = None, *, url: str | None = None) -> AsyncEngine:
    """Create (or recreate) the process-wide async engine."""
    global _engine, _session_factory
    settings = settings or get_settings()
    target_url = url or settings.database_url
    kwargs: dict[str, object] = {
        "echo": settings.db_echo,
        "pool_pre_ping": True,
        "pool_recycle": settings.db_pool_recycle,
    }
    if not target_url.startswith("sqlite"):
        kwargs |= {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "pool_timeout": settings.db_pool_timeout,
        }
    _engine = create_async_engine(target_url, **kwargs)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    logger.info("database engine initialised", extra={"url": _redact_url(target_url)})
    return _engine


def get_engine() -> AsyncEngine:
    """Return the initialised engine, creating it on demand."""
    if _engine is None:
        return init_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory, creating the engine on demand."""
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None  # noqa: S101 - narrowed by init_engine
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a transactional session.

    The session is rolled back on error and closed on exit; successful
    requests commit explicitly at the call site so that a failed ledger
    write can never be silently swallowed.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Standalone transactional scope for background tasks."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose the engine and drop the connection pool."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def _redact_url(url: str) -> str:
    """Strip credentials from a database URL before logging it."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
def _split_sql_statements(script: str) -> list[str]:
    """Split a SQL script into individual statements.

    Understands single-quoted strings, double-quoted identifiers, dollar
    quoted blocks and ``--`` / ``/* */`` comments so that a semicolon inside
    a literal never splits a statement.
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(script)
    while index < length:
        char = script[index]
        nxt = script[index + 1] if index + 1 < length else ""
        if char == "-" and nxt == "-":
            while index < length and script[index] != "\n":
                index += 1
            continue
        if char == "/" and nxt == "*":
            index += 2
            while index < length and not (
                script[index] == "*" and index + 1 < length and script[index + 1] == "/"
            ):
                index += 1
            index += 2
            continue
        if char == "'":
            buffer.append(char)
            index += 1
            while index < length:
                buffer.append(script[index])
                if script[index] == "'":
                    if index + 1 < length and script[index + 1] == "'":
                        buffer.append("'")
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char == '"':
            buffer.append(char)
            index += 1
            while index < length:
                buffer.append(script[index])
                if script[index] == '"':
                    index += 1
                    break
                index += 1
            continue
        if char == "$":
            end = script.find("$", index + 1)
            if end > index + 1:
                tag = script[index : end + 1]
                close = script.find(tag, end + 1)
                if close != -1:
                    buffer.append(script[index : close + len(tag)])
                    index = close + len(tag)
                    continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


async def apply_migrations(engine: AsyncEngine, migrations_dir: Path | None = None) -> list[str]:
    """Apply every pending ``*.sql`` migration in lexical order.

    Returns the list of versions applied by this call.  Each migration is
    recorded with a checksum so that a modified migration is detected.
    """
    directory = migrations_dir or MIGRATIONS_DIR
    files = sorted(directory.glob("*.sql"))
    if not files:
        logger.warning("no migrations found in %s", directory)
        return []

    applied: list[str] = []
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version VARCHAR(128) PRIMARY KEY, "
                "checksum VARCHAR(64) NOT NULL, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        rows = (
            await connection.execute(text("SELECT version, checksum FROM schema_migrations"))
        ).all()
        existing = {row[0]: row[1] for row in rows}

        for path in files:
            version = path.stem
            script = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()
            if version in existing:
                if existing[version] != checksum:
                    logger.warning("migration %s checksum changed after being applied", version)
                continue
            logger.info("applying migration %s", version)
            # asyncpg's driver refuses to execute several statements in one
            # call, and a failed bulk exec aborts the surrounding transaction
            # (so the fallback could never run).  Statements are therefore
            # always executed one by one.
            for statement in _split_sql_statements(script):
                await connection.exec_driver_sql(statement)
            await connection.execute(
                text("INSERT INTO schema_migrations (version, checksum) VALUES (:v, :c)"),
                {"v": version, "c": checksum},
            )
            applied.append(version)
    return applied


async def create_all(engine: AsyncEngine) -> None:
    """Create every table from the ORM metadata (used by the offline harness)."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


# ---------------------------------------------------------------------------
# Reference data seeding
# ---------------------------------------------------------------------------
async def seed_reference_data(
    session: AsyncSession, settings: Settings | None = None
) -> dict[str, int]:
    """Upsert the plan catalogue and the model price book. Idempotent.

    The price book is treated as append-only: an existing price card for a
    model is never mutated, so historical ledger rows always keep the rate
    that was in force when they were written.
    """
    settings = settings or get_settings()
    counts = {"plans": 0, "model_pricing": 0}

    plan_specs = {
        settings.free_plan_code: {
            "name": "Free",
            "description": "Entry tier - capped monthly spend.",
            "monthly_limit_microcents": settings.free_monthly_limit_microcents,
            "rate_limit_rpm": settings.free_rate_limit_rpm,
            "stripe_price_id": settings.stripe_price_free or None,
        },
        settings.pro_plan_code: {
            "name": "Pro",
            "description": "Production tier - higher spend cap and throughput.",
            "monthly_limit_microcents": settings.pro_monthly_limit_microcents,
            "rate_limit_rpm": settings.pro_rate_limit_rpm,
            "stripe_price_id": settings.stripe_price_pro or None,
        },
    }

    existing_plans = {
        plan.code: plan for plan in (await session.execute(select(Plan))).scalars().all()
    }
    for code, spec in plan_specs.items():
        plan = existing_plans.get(code)
        if plan is None:
            session.add(Plan(code=code, **spec))
            counts["plans"] += 1
        else:
            for field, value in spec.items():
                setattr(plan, field, value)
    await session.flush()

    for model, entry in settings.model_pricing.items():
        existing_price = (
            (
                await session.execute(
                    select(ModelPricing).where(ModelPricing.model == model).limit(1)
                )
            )
            .scalars()
            .first()
        )
        if existing_price is None:
            session.add(
                ModelPricing(
                    model=model,
                    provider=entry.provider,
                    input_microcents_per_million=entry.input_microcents_per_million,
                    output_microcents_per_million=entry.output_microcents_per_million,
                    cached_input_microcents_per_million=entry.cached_input_microcents_per_million,
                )
            )
            counts["model_pricing"] += 1

    await session.commit()
    return counts
