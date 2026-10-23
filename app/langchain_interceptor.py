"""LangChain / LangGraph token-usage interceptor.

The handler is a standard ``langchain_core.callbacks.AsyncCallbackHandler``,
so it can be attached to any chain, chat model or LangGraph graph::

    handler = MeteringCallbackHandler(sink, tenant_id=tenant.id, model="gpt-4o")
    result = await graph.ainvoke(state, config={"callbacks": [handler]})

It intercepts **every** LLM call of the run and streams a normalised
:class:`TokenUsage` record (input, cached input, output and reasoning
tokens) into the metering pipeline.  The normalisation is deliberately
tolerant: OpenAI, Anthropic and LangChain's own ``usage_metadata`` shapes all
reduce to the same four counters.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
from typing import Any
import uuid

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from app.calculator import TokenUsage
from app.metrics import TOKENS_TOTAL

logger = logging.getLogger("llm.metering.interceptor")


@dataclass(frozen=True, slots=True)
class InterceptedUsage:
    """One intercepted LLM call, normalised for the metering pipeline."""

    tenant_id: str
    request_id: str
    model: str
    usage: TokenUsage
    run_id: str | None = None
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "request_id": self.request_id,
            "model": self.model,
            "run_id": self.run_id,
            "tags": list(self.tags),
            "input_tokens": self.usage.input_tokens,
            "cached_input_tokens": self.usage.cached_input_tokens,
            "output_tokens": self.usage.output_tokens,
            "reasoning_tokens": self.usage.reasoning_tokens,
            "total_tokens": self.usage.total_tokens,
        }


#: A sink receives every intercepted usage record.
UsageSink = Callable[[InterceptedUsage], Awaitable[None]]


def _as_int(value: Any) -> int:
    """Coerce provider token counters to a non-negative ``int``."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _dig(source: Any, *path: str) -> Any:
    """Safely walk a nested mapping/attribute structure."""
    current = source
    for key in path:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return current


def extract_token_usage(response: LLMResult) -> TokenUsage | None:
    """Normalise a LangChain ``LLMResult`` into :class:`TokenUsage`.

    Supports, in priority order:

    1. ``response.generations[0][0].message.usage_metadata`` (chat models).
    2. ``response.llm_output["token_usage"]`` (completion models, OpenAI).
    3. ``response.llm_output["usage"]`` (Anthropic style).

    Returns ``None`` when the provider reported no usage at all.
    """
    usage_metadata = _dig(response, "generations", "0", "message", "usage_metadata")
    if isinstance(usage_metadata, dict) and usage_metadata:
        input_tokens = _as_int(usage_metadata.get("input_tokens"))
        output_tokens = _as_int(usage_metadata.get("output_tokens"))
        cached = _as_int(
            _dig(usage_metadata, "input_token_details", "cached_tokens")
            or _dig(usage_metadata, "input_token_details", "cache_read_input_tokens")
        )
        reasoning = _as_int(_dig(usage_metadata, "output_token_details", "reasoning_tokens"))
        if input_tokens or output_tokens or reasoning:
            return TokenUsage(
                input_tokens=input_tokens,
                cached_input_tokens=cached,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning,
                reasoning_included_in_output=True,
            )

    llm_output = response.llm_output or {}
    if not isinstance(llm_output, dict):
        return None

    token_usage = llm_output.get("token_usage") or llm_output.get("usage")
    if not isinstance(token_usage, dict) or not token_usage:
        return None

    input_tokens = _as_int(token_usage.get("prompt_tokens", token_usage.get("input_tokens")))
    output_tokens = _as_int(token_usage.get("completion_tokens", token_usage.get("output_tokens")))
    cached = _as_int(
        _dig(token_usage, "prompt_tokens_details", "cached_tokens")
        or _dig(token_usage, "input_token_details", "cached_tokens")
        or token_usage.get("cache_read_input_tokens")
    )
    reasoning = _as_int(
        _dig(token_usage, "completion_tokens_details", "reasoning_tokens")
        or _dig(token_usage, "output_token_details", "reasoning_tokens")
    )
    if not (input_tokens or output_tokens or reasoning):
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning,
        reasoning_included_in_output=True,
    )


class MeteringCallbackHandler(AsyncCallbackHandler):
    """Streams intercepted token usage into the metering pipeline.

    The handler is intentionally *fail-safe*: a broken metering sink can
    never take down the agent that is being metered.
    """

    raise_error = False

    def __init__(
        self,
        sink: UsageSink,
        *,
        tenant_id: str,
        request_id: str | None = None,
        model: str | None = None,
        reasoning_included_in_output: bool = True,
    ) -> None:
        super().__init__()
        self._sink = sink
        self.tenant_id = tenant_id
        self.request_id = request_id or uuid.uuid4().hex
        self.default_model = model
        self.reasoning_included_in_output = reasoning_included_in_output
        self.records: list[InterceptedUsage] = []
        self.errors: list[str] = []
        self._model = model
        self._steps = 0

    # -- introspection ------------------------------------------------------
    @property
    def steps(self) -> int:
        """Number of LLM calls intercepted for this run."""
        return self._steps

    @property
    def total_usage(self) -> TokenUsage:
        """Aggregate of every intercepted call."""
        return TokenUsage(
            input_tokens=sum(record.usage.input_tokens for record in self.records),
            cached_input_tokens=sum(record.usage.cached_input_tokens for record in self.records),
            output_tokens=sum(record.usage.output_tokens for record in self.records),
            reasoning_tokens=sum(record.usage.reasoning_tokens for record in self.records),
            reasoning_included_in_output=self.reasoning_included_in_output,
        )

    # -- LangChain callbacks ------------------------------------------------
    async def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any
    ) -> None:
        """Capture the invoked model before the call completes."""
        self._steps += 1
        name = _dig(serialized or {}, "name") or _dig(serialized or {}, "id", "2")
        if isinstance(name, list) and name:
            name = name[-1]
        if name:
            self._model = str(name)

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Extract the usage of a finished LLM call and stream it downstream."""
        usage = extract_token_usage(response)
        if usage is None:
            return
        if not usage.reasoning_included_in_output:
            usage = TokenUsage(
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                reasoning_included_in_output=False,
            )

        model = self._model or self.default_model or "unknown"
        run_id = kwargs.get("run_id")
        record = InterceptedUsage(
            tenant_id=self.tenant_id,
            request_id=self.request_id,
            model=model,
            usage=usage,
            run_id=str(run_id) if run_id is not None else None,
            tags=tuple(kwargs.get("tags") or ()),
            metadata=dict(kwargs.get("metadata") or {}),
            raw=dict(response.llm_output or {}),
        )
        self.records.append(record)

        for kind, value in (
            ("input", usage.input_tokens),
            ("cached_input", usage.cached_input_tokens),
            ("output", usage.output_tokens),
            ("reasoning", usage.reasoning_tokens),
        ):
            if value:
                TOKENS_TOTAL.labels(model=model, kind=kind).inc(value)

        try:
            await self._sink(record)
        except Exception:  # noqa: BLE001 - metering must never break the agent
            logger.exception("metering sink failed; dropping usage record")

    async def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """Record a failed LLM call (no usage, but visible in the audit trail)."""
        self.errors.append(str(error))
        logger.warning("intercepted llm error: %s", error)


def build_callbacks(
    sink: UsageSink, *, tenant_id: str, request_id: str | None = None, model: str | None = None
) -> list[MeteringCallbackHandler]:
    """Convenience helper returning the callback list for ``ainvoke``."""
    return [MeteringCallbackHandler(sink, tenant_id=tenant_id, request_id=request_id, model=model)]
