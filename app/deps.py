"""Shared FastAPI dependencies for the metering API."""

from __future__ import annotations

from collections.abc import AsyncIterator
import hashlib
import hmac
import uuid

from fastapi import Depends, Header, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AuthenticationError, TenantNotFoundError
from app.metering import (
    IdempotencyManager,
    QuotaChecker,
)
from app.models import Tenant
from app.providers import SimulatedProvider
from app.redis_client import get_redis
from app.settings import Settings, get_settings


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: a transactional DB session."""
    from app.db import get_session as _get_session

    async for session in _get_session():
        yield session


async def get_redis_client(settings: Settings = Depends(get_settings)) -> Redis:
    """FastAPI dependency: the async Redis client."""
    return get_redis()


def get_quota_checker(
    redis: Redis = Depends(get_redis_client), settings: Settings = Depends(get_settings)
) -> QuotaChecker:
    """FastAPI dependency: the quota checker."""
    return QuotaChecker(redis, settings)


def get_idempotency_manager(
    redis: Redis = Depends(get_redis_client), settings: Settings = Depends(get_settings)
) -> IdempotencyManager:
    """FastAPI dependency: the idempotency manager."""
    return IdempotencyManager(redis, settings)


def build_guard(settings: Settings = Depends(get_settings)) -> SimulatedProvider | None:
    """FastAPI dependency factory: the simulated LLM provider (null when unused)."""
    SimulatedProvider()
    return None  # kept for explicit signalling if wiring changes


async def resolve_tenant(
    request: Request,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Tenant:
    """Resolve the tenant from ``X-Tenant-ID`` and validate the bearer token.

    ``X-Tenant-ID`` carries the tenant slug.  When
    ``settings.api_key_required`` is on, the request must additionally carry
    ``Authorization: Bearer <api-key>`` whose SHA-256 digest matches the
    ``api_key_hash`` stored on the tenant row.
    """
    slug = (x_tenant_id or "").strip()
    if not slug:
        raise AuthenticationError("Missing X-Tenant-ID header.", reason="missing_tenant")

    tenant = (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalars().first()
    if tenant is None:
        # A tenant may also be addressed by its UUID.  The cast is guarded:
        # comparing a UUID column with a non-UUID string raises a binding
        # error, so the slug lookup above has to be tried first.
        parsed = _as_uuid(slug)
        if parsed is not None:
            tenant = (
                (await session.execute(select(Tenant).where(Tenant.id == parsed))).scalars().first()
            )
    if tenant is None:
        raise TenantNotFoundError(f"No tenant found for slug '{slug}'.")

    if settings.api_key_required:
        token = _bearer_token(authorization)
        if token is None:
            raise AuthenticationError("Missing bearer token.", reason="missing_api_key")
        if not verify_api_key(tenant, token):
            raise AuthenticationError("Invalid API key.", reason="invalid_api_key")
    return tenant


def _as_uuid(value: str) -> uuid.UUID | None:
    """Parse ``value`` as a UUID, returning ``None`` when it is not one."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def hash_api_key(raw_key: str) -> str:
    """Hash an API key for storage (``sha256:<hex>``)."""
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def verify_api_key(tenant: Tenant, raw_key: str) -> bool:
    """Constant-time comparison of a presented key against the stored hash."""
    stored = tenant.api_key_hash
    if not stored:
        return False
    candidate = hash_api_key(raw_key)
    return hmac.compare_digest(stored, candidate)
