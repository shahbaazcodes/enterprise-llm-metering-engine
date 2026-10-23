"""Synchronous HTTP client wrapper for the FastAPI LLM Metering & Billing backend.

This module provides a clean, typed interface to all backend endpoints using `httpx`
in synchronous mode, which integrates naturally with Streamlit's execution model.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class MeteringApiClient:
    """Synchronous REST client for the LLM metering & billing engine.

    All methods raise `httpx.HTTPStatusError` on non-2xx responses, allowing
    the UI layer to catch and display errors consistently.
    """

    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        """Lazily create and return the underlying httpx.Client."""
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        return self._client

    def close(self) -> None:
        """Close the underlying HTTP client and release resources."""
        if self._client is not None:
            self._client.close()
            self._client = None

    # --------------------------------------------------------------------- #
    # Public endpoint methods
    # --------------------------------------------------------------------- #

    def check_health(self) -> dict[str, Any]:
        """GET /health — Dependency health probe.

        Returns:
            Dict with keys: status, database, redis, version, environment.
        """
        response = self.client.get("/health")
        response.raise_for_status()
        return response.json()

    def generate_completion(
        self,
        tenant_id: str,
        model: str,
        prompt: str,
        idempotency_key: str | None = None,
        max_output_tokens: int = 256,
        temperature: float = 0.7,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /v1/generate — Billable LLM completion with metering.

        Args:
            tenant_id: The tenant slug (sent as X-Tenant-ID header).
            model: Model identifier (e.g., "gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet").
            prompt: The user prompt text.
            idempotency_key: Optional key for exactly-once billing (Idempotency-Key header).
            max_output_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.
            metadata: Arbitrary metadata attached to the usage event.

        Returns:
            Dict matching GenerateResponse schema.
        """
        headers: dict[str, str] = {"X-Tenant-ID": tenant_id}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        payload: dict[str, Any] = {
            "prompt": prompt,
            "model": model,
            "max_output_tokens": max_output_tokens,
            "temperature": temperature,
            "metadata": metadata or {},
        }

        response = self.client.post("/v1/generate", json=payload, headers=headers)
        response.raise_for_status()
        return response.json()

    def get_tenant_usage(self, tenant_id: str) -> dict[str, Any]:
        """GET /v1/usage — Tenant usage rollup and cost breakdown.

        Args:
            tenant_id: The tenant slug (sent as X-Tenant-ID header).

        Returns:
            Dict matching UsageSummaryResponse schema.
        """
        headers: dict[str, str] = {"X-Tenant-ID": tenant_id}
        response = self.client.get("/v1/usage", headers=headers)
        response.raise_for_status()
        return response.json()

    def get_metrics(self) -> str:
        """GET /metrics — Prometheus exposition endpoint.

        Returns:
            Raw Prometheus text format metrics.
        """
        response = self.client.get("/metrics")
        response.raise_for_status()
        return response.text

    def trigger_stripe_webhook(
        self,
        event_payload: dict[str, Any],
        signature: str,
    ) -> dict[str, Any]:
        """POST /v1/webhooks/stripe — Simulate a Stripe webhook event.

        The raw JSON body must be sent exactly as Stripe would sign it.

        Args:
            event_payload: The Stripe event object (e.g., checkout.session.completed).
            signature: The Stripe-Signature header value.

        Returns:
            Dict matching WebhookResponse schema.
        """
        headers: dict[str, str] = {
            "Stripe-Signature": signature,
            "Content-Type": "application/json",
        }
        response = self.client.post(
            "/v1/webhooks/stripe",
            content=json.dumps(event_payload),
            headers=headers,
        )
        response.raise_for_status()
        return response.json()


def create_client(base_url: str) -> MeteringApiClient:
    """Factory function for Streamlit session state initialization."""
    return MeteringApiClient(base_url=base_url)
