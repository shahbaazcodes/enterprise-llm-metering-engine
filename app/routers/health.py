"""``GET /health`` - dependency health probe."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session
from app.redis_client import ping as redis_ping
from app.schemas import HealthResponse
from app.settings import Settings, get_settings

router = APIRouter(tags=["ops"])


@router.get("/health", response_model=HealthResponse, summary="Health probe")
async def health(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    """Report the health of the database and Redis dependencies."""
    database = "up"
    try:
        await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - any failure means the dependency is down
        database = "down"
    redis_state = "up" if await redis_ping() else "down"
    overall = "ok" if database == "up" and redis_state == "up" else "degraded"
    return HealthResponse(
        status=overall,
        database=database,
        redis=redis_state,
        version="1.0.0",
        environment=settings.environment,
    )
