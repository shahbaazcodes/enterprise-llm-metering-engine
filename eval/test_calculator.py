"""P4 - exact micro-cent pricing arithmetic.

The money path must never touch a float.  Every assertion in this module is
expressed in **integer micro-cents** and is cross-checked against an
independent reference implementation that uses exact rationals
(:class:`fractions.Fraction`), so a regression in the rounding rule cannot be
hidden by the very code that is under test.
"""

from __future__ import annotations

from fractions import Fraction
import json
import math
from pathlib import Path

import pytest

from app.calculator import (
    TOKENS_PER_PRICE_UNIT,
    ModelPrice,
    TokenUsage,
    calculate_token_cost_micro_cents,
    microcents_to_usd_str,
    round_half_up,
    usd_to_microcents,
)

_EVAL_SET = json.loads((Path(__file__).resolve().parent / "eval_set.json").read_text("utf-8"))

#: The full pricing matrix: every price card against every token shape.
_MATRIX_CASES = [
    pytest.param(price, tokens, id=f"{price['model']}::{tokens['name']}")
    for price in _EVAL_SET["pricing_models"]
    for tokens in _EVAL_SET["token_counts"]
]


# ---------------------------------------------------------------------------
# Independent reference implementation
# ---------------------------------------------------------------------------
def _half_up(value: Fraction) -> int:
    """Round an exact rational half away from zero (values are positive)."""
    return math.floor(value + Fraction(1, 2))


def _reference_cost(
    usage: TokenUsage,
    price: ModelPrice,
    cached_input_discount_bps: int = 2500,
) -> int:
    """Re-derive the cost from the pricing rules, using exact rationals.

    Deliberately written from the *rules* rather than from the production
    code, so that it is an independent check of the implementation.
    """

    def component(tokens: int, rate_per_million: int) -> int:
        if tokens <= 0:
            return 0
        return _half_up(Fraction(tokens * rate_per_million, TOKENS_PER_PRICE_UNIT))

    cached_rate = price.cached_input_microcents_per_million
    if cached_rate <= 0:
        cached_rate = _half_up(
            Fraction(price.input_microcents_per_million * cached_input_discount_bps, 10_000)
        )

    billable_output = usage.output_tokens
    if not usage.reasoning_included_in_output:
        billable_output += usage.reasoning_tokens

    return (
        component(usage.input_tokens, price.input_microcents_per_million)
        + component(usage.cached_input_tokens, cached_rate)
        + component(billable_output, price.output_microcents_per_million)
    )


def _price(entry: dict) -> ModelPrice:
    return ModelPrice(
        model=entry["model"],
        provider=entry["provider"],
        input_microcents_per_million=entry["input_microcents_per_million"],
        output_microcents_per_million=entry["output_microcents_per_million"],
        cached_input_microcents_per_million=entry["cached_input_microcents_per_million"],
    )


def _usage(entry: dict) -> TokenUsage:
    return TokenUsage(
        input_tokens=entry["input_tokens"],
        cached_input_tokens=entry["cached_input_tokens"],
        output_tokens=entry["output_tokens"],
        reasoning_tokens=entry["reasoning_tokens"],
        reasoning_included_in_output=entry["reasoning_included_in_output"],
    )


# ---------------------------------------------------------------------------
# The declared probe
# ---------------------------------------------------------------------------
def test_fresh_input_pricing_exact_microcents() -> None:
    """1K fresh prompt tokens on ``gpt-4o`` cost exactly 2_500 micro-cents.

    ``gpt-4o`` is quoted at $2.50 per 1M input tokens, i.e. 2_500_000
    micro-cents per 1M tokens.  1_000 tokens are therefore
    ``1000 * 2_500_000 / 1_000_000`` = 2_500 micro-cents = $0.0025.
    """
    price = ModelPrice(
        model="gpt-4o",
        input_microcents_per_million=2_500_000,
        output_microcents_per_million=10_000_000,
        cached_input_microcents_per_million=625_000,
    )
    usage = TokenUsage(input_tokens=1_000)

    cost = calculate_token_cost_micro_cents(usage, price)

    assert cost.input_microcents == 2_500
    assert cost.cached_input_microcents == 0
    assert cost.output_microcents == 0
    assert cost.reasoning_microcents == 0
    assert cost.total_microcents == 2_500
    assert cost.total_microcents == 1_000 * 2_500_000 // 1_000_000
    assert cost.to_dict()["usd_display"] == "0.002500"
    assert microcents_to_usd_str(cost.total_microcents) == "0.002500"
    # No float may leak into the money path.
    assert isinstance(cost.total_microcents, int)


def test_cached_input_uses_discounted_rate() -> None:
    """Cached prompt tokens are billed at the discounted cached rate."""
    price = ModelPrice(
        model="gpt-4o",
        input_microcents_per_million=2_500_000,
        output_microcents_per_million=10_000_000,
        cached_input_microcents_per_million=625_000,  # 25% of the input rate
    )
    usage = TokenUsage(cached_input_tokens=1_000)

    cost = calculate_token_cost_micro_cents(usage, price)

    # 1_000 cached tokens at 625_000 / 1M == 625 micro-cents.
    assert cost.cached_input_microcents == 625
    assert cost.input_microcents == 0
    assert cost.total_microcents == 625


def test_reasoning_subset_is_not_billed_twice() -> None:
    """Reasoning folded into the output is billed exactly once."""
    price = ModelPrice(
        model="gpt-4o",
        input_microcents_per_million=2_500_000,
        output_microcents_per_million=10_000_000,
        cached_input_microcents_per_million=625_000,
    )
    usage = TokenUsage(output_tokens=1_000, reasoning_tokens=400, reasoning_included_in_output=True)

    cost = calculate_token_cost_micro_cents(usage, price)

    assert usage.billable_output_tokens == 1_000
    # The output layer already contains the reasoning tokens.
    assert cost.output_microcents == 10_000
    assert cost.reasoning_microcents == 4_000  # reported for analytics only
    assert cost.total_microcents == cost.output_microcents  # never double-counted
    assert cost.total_microcents == 10_000


def test_reasoning_additive_is_billed_on_top() -> None:
    """Anthropic-style split counters bill reasoning on top of output."""
    price = ModelPrice(
        model="claude-sonnet-4",
        input_microcents_per_million=3_000_000,
        output_microcents_per_million=15_000_000,
        cached_input_microcents_per_million=750_000,
    )
    usage = TokenUsage(
        output_tokens=1_000, reasoning_tokens=400, reasoning_included_in_output=False
    )

    cost = calculate_token_cost_micro_cents(usage, price)

    assert usage.billable_output_tokens == 1_400
    # 1_400 billable output tokens at 15_000_000 / 1M == 21_000 micro-cents.
    assert cost.output_microcents == 21_000
    assert cost.reasoning_microcents == 6_000  # reported for analytics only
    assert cost.total_microcents == 21_000
    # The three billed components always sum to the total - the reasoning
    # layer is a *subset* of the output layer, never an extra charge.
    assert (
        cost.input_microcents + cost.cached_input_microcents + cost.output_microcents
        == cost.total_microcents
    )


@pytest.mark.parametrize(("price", "tokens"), _MATRIX_CASES)
def test_pricing_matrix_matches_independent_reference(price: dict, tokens: dict) -> None:
    """Every manifest price card against every token shape matches the reference."""
    expected = _reference_cost(_usage(tokens), _price(price))
    cost = calculate_token_cost_micro_cents(_usage(tokens), _price(price))
    assert cost.total_microcents == expected
    # The billed components always sum to the total.
    assert (
        cost.input_microcents + cost.cached_input_microcents + cost.output_microcents
        == cost.total_microcents
    )
    # The USD rendering round-trips back to the exact micro-cent amount.
    assert usd_to_microcents(cost.to_dict()["usd_display"]) == cost.total_microcents


def test_round_half_up_rounds_away_from_zero() -> None:
    """Halves always round away from zero, never toward it."""
    assert round_half_up(1, 2) == 1  # 0.5 -> 1
    assert round_half_up(2, 2) == 1  # 1.0 -> 1
    assert round_half_up(1_5, 10) == 2  # 1.5 -> 2
    assert round_half_up(-1_5, 10) == -2  # -1.5 -> -2
    assert round_half_up(0, 10) == 0


def test_microcents_and_usd_round_trip_is_lossless() -> None:
    """USD -> micro-cents -> USD is stable for every amount we can bill."""
    for microcents in (1, 625, 2_500, 10_000, 36_250, 500_000_000):
        assert usd_to_microcents(microcents_to_usd_str(microcents)) == microcents
    assert usd_to_microcents("0.0025") == 2_500
