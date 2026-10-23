"""Formatting and display utilities for the Streamlit dashboard.

All micro-cent amounts in the backend are integers where 1 USD = 1_000_000
micro-cents.  These helpers provide consistent human-facing display formatting.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: 1 USD = 1_000_000 micro-cents (matches the backend's MICROCENTS_PER_USD).
MICROCENTS_PER_USD = 1_000_000


def microcents_to_usd(microcents: int | float) -> Decimal:
    """Convert micro-cents to a USD Decimal (display only, never store USD)."""
    value = Decimal(str(microcents)) / Decimal(MICROCENTS_PER_USD)
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def microcents_to_usd_str(microcents: int | float) -> str:
    """Render micro-cents as a fixed 6-decimal USD string: ``$1.234567``."""
    return f"${microcents_to_usd(microcents)}"


def format_microcents(microcents: int | float) -> str:
    """Format a micro-cent amount with thousands separators, e.g. ``1,234,567 µ¢``."""
    return f"{int(microcents):,} µ¢"


def format_cost_card(usd_amount: Decimal | float | int | str) -> str:
    """Format a USD amount for display on a cost card, e.g. ``$1.23``."""
    value = Decimal(str(usd_amount))
    return f"${value:,.2f}"


def format_tokens(tokens: int) -> str:
    """Format a token count with thousands separators, e.g. ``1,234``."""
    return f"{int(tokens):,}"


def usage_to_usd(microcents: int) -> str:
    """Convert micro-cents to a 2-decimal USD string for dashboard display."""
    usd = microcents / MICROCENTS_PER_USD
    return f"${usd:,.2f}"
