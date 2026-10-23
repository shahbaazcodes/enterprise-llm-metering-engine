"""FastAPI application factory for the LLM metering & billing engine.

The app wires four routers behind the ``/v1`` prefix and exposes the
Prometheus scrape endpoint at ``/metrics``:

===========================  =================================================
``POST /v1/generate``        Billable LLM endpoint (Idempotency-Key aware)
``GET  /v1/usage``           Tenant rollup + per-model cost breakdown
``POST /v1/webhooks/stripe`` Signature-verified Stripe subscription sync
``GET  /metrics``            Prometheus exposition format
``GET  /health``             Dependency health probe
===========================  =================================================
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import time
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

from app import metrics
from app.exceptions import MeteringError
from app.ml_guard import AnomalyGuard
from app.providers import SimulatedProvider
from app.routers import generate, health, usage, webhooks
from app.settings import Settings, get_settings

logger = logging.getLogger("llm.metering.main")

APP_VERSION = "1.0.0"

#: Label used for requests that did not match any declared route.  A fixed
#: template keeps the ``route`` label cardinality bounded; using the raw
#: request path would create one time series per unique 404 URL.
UNMATCHED_ROUTE_LABEL = "/unmatched"


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Records request counters and latency histograms.

    The ``route`` label is the *matched route template* (e.g. ``/generate``),
    never the raw URL, so the label cardinality stays bounded.  Unmatched
    requests collapse onto :data:`UNMATCHED_ROUTE_LABEL`.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            # ``route`` is set by Starlette on every successful match; anything
            # that did not match any declared route falls back to a fixed
            # template instead of ``request.url.path``, so the label is always
            # bounded (no per-request unique cardinality leak).
            route_path = getattr(route, "path", None) or UNMATCHED_ROUTE_LABEL
            method = request.method
            elapsed = time.perf_counter() - started
            metrics.HTTP_REQUESTS_TOTAL.labels(
                route=route_path, method=method, status=str(status_code)
            ).inc()
            metrics.HTTP_REQUEST_DURATION_SECONDS.labels(route=route_path, method=method).observe(
                elapsed
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot the engine: migrate, seed and warm the shared components."""
    from app.db import apply_migrations, dispose_engine, get_engine, seed_reference_data
    from app.redis_client import close_redis

    settings: Settings = app.state.settings
    app.state.anomaly_guard = AnomalyGuard(settings)
    app.state.provider = SimulatedProvider(settings.default_model)

    if settings.auto_migrate:
        engine = get_engine()
        applied = await apply_migrations(engine)
        if applied:
            logger.info("applied migrations: %s", ", ".join(applied))
    if settings.auto_seed:
        from app.db import session_scope

        async with session_scope() as session:
            counts = await seed_reference_data(session, settings)
            logger.info("seeded reference data: %s", counts)

    try:
        yield
    finally:
        await close_redis()
        await dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    app = FastAPI(
        title="Enterprise LLM Metering & Billing Engine",
        description=(
            "Multi-tenant LLM usage metering with exact micro-cent accounting, "
            "Redis-backed quota and idempotency guards, ML anomaly detection, "
            "Stripe subscription sync and Prometheus instrumentation."
        ),
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    app.add_middleware(PrometheusMiddleware)

    prefix = settings.api_v1_prefix
    app.include_router(generate.router, prefix=prefix)
    app.include_router(usage.router, prefix=prefix)
    app.include_router(webhooks.router, prefix=prefix)
    app.include_router(health.router)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        """Prometheus exposition endpoint."""
        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "service": settings.app_name,
            "version": APP_VERSION,
            "docs": "/docs",
            "metrics": "/metrics",
        }

    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Map the typed application errors onto their HTTP status codes."""

    @app.exception_handler(MeteringError)
    async def _metering_error(request: Request, exc: MeteringError) -> JSONResponse:
        headers = None
        retry_after = exc.details.get("retry_after_seconds")
        if retry_after:
            headers = {"Retry-After": str(retry_after)}
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "message": exc.message, "details": exc.details},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "Request payload failed validation.",
                "details": {"errors": exc.errors()},
            },
        )


app = create_app()


def run() -> None:
    """Console-script entry point (``llm-metering``).

    The application object is imported *by name* so that uvicorn's reloader
    and its ``--workers`` mode can fork a clean child process instead of
    re-using the already-imported module state.
    """
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=settings.uvicorn_reload,
        workers=settings.uvicorn_workers if not settings.uvicorn_reload else 1,
    )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    run()
