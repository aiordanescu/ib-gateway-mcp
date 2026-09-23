"""Models for IBKR's option calculators and chain-slice quotes (``get_option_quotes``)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field

from ib_gateway_mcp.models.common import (
    ContractOut,
    MarketDataTypeName,
    OptionGreeks,
    QuoteOut,
    Right,
    Truncatable,
)

__all__ = [
    "ImpliedVolatilityOut",
    "OptionPriceOut",
    "OptionQuoteList",
    "OptionRight",
    "SkippedOptionLeg",
]

_RIGHT_ALIASES = {"C": "C", "CALL": "C", "P": "P", "PUT": "P"}


def _normalize_right(value: object) -> object:
    if isinstance(value, str):
        return _RIGHT_ALIASES.get(value.strip().upper(), value)
    return value


OptionRight = Annotated[Right, BeforeValidator(_normalize_right)]
"""An option right, C or P; also accepts CALL/PUT in any case."""


class ImpliedVolatilityOut(BaseModel):
    """IBKR's implied volatility (and greeks) for one option at a given option price."""

    contract: ContractOut = Field(description="The option the calculation ran for.")
    option_price: float = Field(description="Option price the volatility was implied from.")
    underlying_price: float = Field(description="Underlying price the calculation assumed.")
    implied_vol: float = Field(
        description="Implied volatility, annualized, as a decimal (0.25 = 25%)."
    )
    greeks: OptionGreeks = Field(
        description="IBKR's full model output at that volatility: delta, gamma, vega, theta..."
    )


class OptionPriceOut(BaseModel):
    """IBKR's theoretical price (and greeks) for one option at a given volatility."""

    contract: ContractOut = Field(description="The option the calculation ran for.")
    volatility: float = Field(description="Volatility the price assumes, as a decimal.")
    underlying_price: float = Field(description="Underlying price the calculation assumed.")
    option_price: float = Field(description="Theoretical option price, per share (unmultiplied).")
    greeks: OptionGreeks = Field(
        description="IBKR's full model output at that price: delta, gamma, vega, theta..."
    )


class SkippedOptionLeg(BaseModel):
    """A strike and right that was selected but has no quote in the result."""

    strike: float = Field(description="Strike of the missing leg.")
    right: Right = Field(description="Right of the missing leg: C or P.")
    con_id: int | None = Field(None, description="Contract id, when the option exists.")
    reason: str = Field(description="Why it is missing, e.g. not listed for this expiration.")


class OptionQuoteList(Truncatable):
    """Snapshot quotes with greeks for a slice of one option expiration."""

    underlying: ContractOut = Field(description="The instrument the options are on.")
    underlying_quote: QuoteOut | None = Field(
        None,
        description=(
            "Snapshot of the underlying, taken to find the at-the-money strikes; null when "
            "strike_min/strike_max chose the strikes (each leg's greeks.und_price has it)."
        ),
    )
    underlying_price: float | None = Field(
        None,
        description="Price the at-the-money strikes were chosen around (mid, else last or close).",
    )
    expiration: str = Field(description="Expiration date, YYYYMMDD.")
    exchange: str = Field(description="Exchange of the chain the legs were taken from.")
    trading_class: str = Field(description="Trading class of the legs, e.g. SPX or SPXW.")
    multiplier: str | None = Field(None, description="Contract multiplier, e.g. 100.")
    market_data_type: MarketDataTypeName | None = Field(
        None,
        description="Market data type this connection requests (see set_market_data_type).",
    )
    total: int = Field(description="Legs (strike and right pairs) selected before the limit.")
    legs: list[QuoteOut] = Field(description="One quote per option, by strike then right (C, P).")
    skipped: list[SkippedOptionLeg] = Field(
        default_factory=list,
        description="Selected legs without a quote: strike not listed for the expiry, or no data.",
    )
