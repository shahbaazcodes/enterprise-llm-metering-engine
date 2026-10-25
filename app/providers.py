"""Upstream LLM provider abstraction.

The metering engine is provider agnostic: it consumes *token counters*, not
text.  This module provides a deterministic, offline stand-in for a real
upstream API so that the whole billing path can be exercised (and priced)
without vendor credentials.

Swapping in a real provider is a two-line change: implement
:class:`LLMProvider` against the OpenAI / Anthropic SDK and return the
provider's own ``usage`` block - the interceptor in
:mod:`app.langchain_interceptor` already normalises every major shape.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import time
from typing import Protocol

from app.calculator import TokenUsage
from app.schemas import GenerateRequest, UsageOverride

#: Average number of characters per token for English text (OpenAI's rule of
#: thumb is ~4 characters per token).
CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """What every provider must return."""

    text: str
    usage: TokenUsage
    model: str
    latency_ms: int
    provider: str = "simulated"


class LLMProvider(Protocol):
    """Protocol implemented by every upstream provider adapter."""

    async def complete(
        self, request: GenerateRequest, override: UsageOverride | None = None
    ) -> ProviderResult: ...


def estimate_input_tokens(prompt: str) -> int:
    """Estimate the prompt token count."""
    return max(1, math.ceil(len(prompt) / CHARS_PER_TOKEN))


class SimulatedProvider:
    """Deterministic offline provider used for benchmarking and tests.

    The generated text and the token counts are a pure function of the
    request, so two identical requests always produce identical billing -
    which is exactly what the evaluation harness needs to assert on.
    """

    provider = "simulated"

    def __init__(self, default_model: str = "gpt-4o") -> None:
        self.default_model = default_model

    def _deterministic_output_tokens(self, request: GenerateRequest) -> int:
        """Derive a stable output length from the prompt digest."""
        digest = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
        spread = int(digest[:8], 16) % 128
        return max(1, min(request.max_output_tokens, 16 + spread))

    async def complete(
        self, request: GenerateRequest, override: UsageOverride | None = None
    ) -> ProviderResult:
        """Produce a completion and the exact token counters to bill for."""
        started = time.perf_counter()
        if override is not None:
            usage = TokenUsage(
                input_tokens=override.input_tokens,
                cached_input_tokens=override.cached_input_tokens,
                output_tokens=override.output_tokens,
                reasoning_tokens=override.reasoning_tokens,
                reasoning_included_in_output=override.reasoning_included_in_output,
            )
        else:
            input_tokens = estimate_input_tokens(request.prompt)
            output_tokens = self._deterministic_output_tokens(request)
            usage = TokenUsage(
                input_tokens=input_tokens,
                cached_input_tokens=0,
                output_tokens=output_tokens,
                reasoning_tokens=0,
                reasoning_included_in_output=True,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        text = (
            f"[{request.model}] simulated completion "
            f"({usage.input_tokens} in / {usage.output_tokens} out)"
        )
        return ProviderResult(
            text=text,
            usage=usage,
            model=request.model,
            latency_ms=latency_ms,
            provider=self.provider,
        )
