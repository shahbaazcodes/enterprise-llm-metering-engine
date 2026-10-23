"""Exact cost arithmetic in micro-cents.

This module is deliberately **pure**: no I/O, no database, no floats.  It is
the single place where money is computed, which makes it trivially unit
testable and impossible to accidentally introduce rounding drift.

Money model
-----------
* 1 USD = 1_000_000 micro-cents (``MICROCENTS_PER_USD``).
* Prices are quoted per 1_000_000 tokens (``TOKENS_PER_PRICE_UNIT``).
* All arithmetic is integer arithmetic.  A single rounding step (round half
  up) is applied at the very end of each cost component, using exact
  rational intermediates - never a float, never a truncated division.

Examples
--------
``gpt-4o`` costs $2.50 / 1M input tokens  ->  2_500_000 micro-cents / 1M.
1_000 input tokens therefore cost ``1000 * 2_500_000 / 1_000_000`` = 2_500
micro-cents = $0.0025, with **zero** floating point involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.settings import BILLING_CURRENCY, MICROCENTS_PER_USD, TOKENS_PER_PRICE_UNIT

__all__ = [
    "BILLING_CURRENCY",
    "MICROCENTS_PER_USD",
    "TOKENS_PER_PRICE_UNIT",
    "CostBreakdown",
    "ModelPrice",
    "TokenUsage",
    "calculate_token_cost_micro_cents",
    "microcents_to_usd",
    "microcents_to_usd_str",
    "round_half_up",
    "usd_to_microcents",
]


def round_half_up(numerator: int, denominator: int) -> int:
    """Divide two integers, rounding halves away from zero.

    Works for negative numerators as well, so it is safe to use on deltas.
    """
    if denominator <= 0:
        raise ValueError("denominator must be a positive integer")
    if numerator >= 0:
        return (2 * numerator + denominator) // (2 * denominator)
    return -((-2 * numerator + denominator) // (2 * denominator))


def microcents_to_usd(microcents: int) -> Decimal:
    """Convert micro-cents to USD. **Display only** - never store this."""
    return (Decimal(microcents) / Decimal(MICROCENTS_PER_USD)).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )


def microcents_to_usd_str(microcents: int) -> str:
    """Render micro-cents as a fixed 6-decimal USD string."""
    return f"{microcents_to_usd(microcents):.6f}"


def usd_to_microcents(amount: str | Decimal | int) -> int:
    """Convert a USD amount to micro-cents. **Input only** - never store USD."""
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return int((value * MICROCENTS_PER_USD).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token counters for a single LLM invocation.

    ``reasoning_included_in_output`` documents the contract of the upstream
    provider:

    * ``True``  - ``output_tokens`` already contains ``reasoning_tokens``
      (OpenAI / Anthropic semantics).  Reasoning is billed at the output
      rate and is *already covered* by ``output_tokens``.
    * ``False`` - ``output_tokens`` excludes reasoning, so reasoning tokens
      are billed on top of the output tokens.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_included_in_output: bool = True

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")

    @property
    def billable_output_tokens(self) -> int:
        """Output tokens actually billed (reasoning folded in when required)."""
        if self.reasoning_included_in_output:
            return self.output_tokens
        return self.output_tokens + self.reasoning_tokens

    @property
    def total_tokens(self) -> int:
        """Total tokens consumed, counting every billed token exactly once."""
        total = self.input_tokens + self.cached_input_tokens + self.billable_output_tokens
        return total

    def scaled(self, numerator: int, denominator: int) -> TokenUsage:
        """Return a proportionally scaled copy (used for estimation)."""
        return TokenUsage(
            input_tokens=round_half_up(self.input_tokens * numerator, denominator),
            cached_input_tokens=round_half_up(self.cached_input_tokens * numerator, denominator),
            output_tokens=round_half_up(self.output_tokens * numerator, denominator),
            reasoning_tokens=round_half_up(self.reasoning_tokens * numerator, denominator),
            reasoning_included_in_output=self.reasoning_included_in_output,
        )


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Price card for one model, in micro-cents per 1_000_000 tokens."""

    model: str
    input_microcents_per_million: int
    output_microcents_per_million: int
    cached_input_microcents_per_million: int = 0
    provider: str = "openai"

    def __post_init__(self) -> None:
        for name in (
            "input_microcents_per_million",
            "output_microcents_per_million",
            "cached_input_microcents_per_million",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Immutable, fully itemised cost of one request.

    ``reasoning_microcents`` is a **subset** of ``output_microcents`` (it is
    never added on top of it) so that the three components always sum to
    ``total_microcents``.
    """

    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    billable_output_tokens: int
    input_microcents: int
    cached_input_microcents: int
    output_microcents: int
    reasoning_microcents: int
    total_microcents: int
    currency: str = BILLING_CURRENCY

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.cached_input_tokens + self.billable_output_tokens

    @property
    def usd_total(self) -> Decimal:
        """USD view of the total. Display only."""
        return microcents_to_usd(self.total_microcents)

    def to_dict(self) -> dict[str, int | str]:
        """Serialisable payload (integers only - money stays integral)."""
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "billable_output_tokens": self.billable_output_tokens,
            "total_tokens": self.total_tokens,
            "input_microcents": self.input_microcents,
            "cached_input_microcents": self.cached_input_microcents,
            "output_microcents": self.output_microcents,
            "reasoning_microcents": self.reasoning_microcents,
            "total_microcents": self.total_microcents,
            "currency": self.currency,
            "usd_display": microcents_to_usd_str(self.total_microcents),
        }


def _component_cost(tokens: int, rate_microcents_per_million: int) -> int:
    """Cost of ``tokens`` at ``rate`` per 1M tokens.

    The whole expression is evaluated as a single rational number and
    rounded exactly once (round half up), so no floating point is ever
    involved in the money path.
    """
    if tokens <= 0:
        return 0
    return round_half_up(tokens * rate_microcents_per_million, TOKENS_PER_PRICE_UNIT)


def calculate_token_cost_micro_cents(
    usage: TokenUsage,
    price: ModelPrice,
    *,
    cached_input_discount_bps: int = 2500,
    currency: str = BILLING_CURRENCY,
) -> CostBreakdown:
    """Compute the exact cost of ``usage`` under ``price``.

    Pricing rules
    -------------
    * **Input tokens** are billed at the model's input rate.
    * **Cached input tokens** are billed at the *cached input rate*, which is
      the price card's ``cached_input_microcents_per_million``.  The seeded
      price book sets that rate to **25%** of the standard input rate, so
      cached input costs a quarter of what fresh input costs.
    * **Output tokens** are billed at the model's output rate.
    * **Reasoning tokens** are billed at the *output* rate.  When the
      provider already includes reasoning inside ``output_tokens`` the
      reasoning cost is a subset of the output cost and is reported
      separately for analytics only; otherwise reasoning is billed on top.

    ``cached_input_discount_bps`` is the plan-level fallback used only when
    the price card carries no explicit cached rate.  It is expressed in
    basis points **of the standard input rate**: ``2_500`` means cached
    input is charged 25% of the fresh input rate.
    """
    if not 0 <= cached_input_discount_bps <= 10_000:
        raise ValueError("cached_input_discount_bps must be between 0 and 10000")

    input_microcents = _component_cost(usage.input_tokens, price.input_microcents_per_million)

    # The price card is authoritative: it is the append-only artifact that
    # keeps a historical ledger row pinned to the rate in force at the time.
    # The plan-level bps only apply when the card carries no cached rate.
    if price.cached_input_microcents_per_million > 0:
        cached_rate = price.cached_input_microcents_per_million
    else:
        cached_rate = round_half_up(
            price.input_microcents_per_million * cached_input_discount_bps, 10_000
        )
    cached_input_microcents = _component_cost(usage.cached_input_tokens, cached_rate)

    output_microcents = _component_cost(
        usage.billable_output_tokens, price.output_microcents_per_million
    )
    reasoning_microcents = _component_cost(
        usage.reasoning_tokens, price.output_microcents_per_million
    )
    total_microcents = input_microcents + cached_input_microcents + output_microcents

    return CostBreakdown(
        model=price.model,
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        billable_output_tokens=usage.billable_output_tokens,
        input_microcents=input_microcents,
        cached_input_microcents=cached_input_microcents,
        output_microcents=output_microcents,
        reasoning_microcents=reasoning_microcents,
        total_microcents=total_microcents,
        currency=currency,
    )
