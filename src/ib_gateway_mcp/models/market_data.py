"""Models for quote snapshots and streaming market data (quotes, depth, tick-by-tick, bars)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, Field

from ib_gateway_mcp.models.common import (
    ContractOut,
    MarketDataTypeName,
    QuoteOut,
    RealtimeWhatToShow,
    WhatToShow,
)

__all__ = [
    "GENERIC_TICK_IDS",
    "BidAskTick",
    "CancelledSubscription",
    "DepthData",
    "DepthLevel",
    "DividendsOut",
    "GenericTick",
    "LiveBarsData",
    "MarketDataNotice",
    "MarketDataTypeOut",
    "MidPointTick",
    "QuoteError",
    "QuoteExtras",
    "QuoteList",
    "QuoteStreamData",
    "RealtimeBar",
    "RealtimeBarsData",
    "StreamBar",
    "StreamStatus",
    "SubscriptionEntry",
    "SubscriptionList",
    "TickByTickData",
    "TickByTickType",
    "TradeTick",
    "UnsubscribeResult",
]

GenericTick = Literal[
    "option_volume",
    "option_open_interest",
    "historical_volatility",
    "avg_option_volume",
    "implied_volatility",
    "index_future_premium",
    "misc_stats",
    "mark_price",
    "auction",
    "rt_volume",
    "shortable",
    "fundamental_ratios",
    "trade_count",
    "trade_rate",
    "volume_rate",
    "rt_trade_volume",
    "rt_historical_volatility",
    "dividends",
    "futures_open_interest",
    "last_rth_trade",
    "bond_factor_multiplier",
    "etf_nav_bid_ask",
    "etf_nav_last",
    "etf_nav_close",
    "ipo_prices",
    "short_term_volume",
    "etf_nav_high_low",
    "etf_nav_frozen_last",
]
"""Optional extra fields a quote stream can carry (TWS API generic tick types)."""

GENERIC_TICK_IDS: Mapping[GenericTick, int] = MappingProxyType(
    {
        "option_volume": 100,
        "option_open_interest": 101,
        "historical_volatility": 104,
        "avg_option_volume": 105,
        "implied_volatility": 106,
        "index_future_premium": 162,
        "misc_stats": 165,
        "mark_price": 221,
        "auction": 225,
        "rt_volume": 233,
        "shortable": 236,
        "fundamental_ratios": 258,
        "trade_count": 293,
        "trade_rate": 294,
        "volume_rate": 295,
        "rt_trade_volume": 375,
        "rt_historical_volatility": 411,
        "dividends": 456,
        "futures_open_interest": 588,
        "last_rth_trade": 318,
        "bond_factor_multiplier": 460,
        "etf_nav_bid_ask": 576,
        "etf_nav_last": 577,
        "etf_nav_close": 578,
        "ipo_prices": 586,
        "short_term_volume": 595,
        "etf_nav_high_low": 614,
        "etf_nav_frozen_last": 623,
    }
)
"""TWS API generic tick ids for :data:`GenericTick` names (``reqMktData(genericTickList)``)."""

TickByTickType = Literal["Last", "AllLast", "BidAsk", "MidPoint"]
"""Tick-by-tick data types: trades on the primary tape (Last), trades on every venue
including odd lots and dark pools (AllLast), quote changes (BidAsk) and midpoints."""


# --- snapshots ---------------------------------------------------------------------------


class QuoteError(BaseModel):
    """Why one contract of a ``get_quotes`` call has no quote."""

    contract: str = Field(description="The contract as requested.")
    code: str = Field(
        description="Error kind: not_found, ambiguous_contract, ib_api_error, request_timeout..."
    )
    ib_error_code: int | None = Field(None, description="TWS API error code, when IBKR sent one.")
    message: str


class QuoteList(BaseModel):
    """Quote snapshots, in the order the contracts were requested."""

    quotes: list[QuoteOut]
    errors: list[QuoteError] = Field(
        default_factory=list, description="Contracts that got no quote, and why."
    )
    notices: list[str] = Field(
        default_factory=list, description="Things to know about the data (fees, missing data)."
    )
    regulatory_snapshots: int = Field(
        0, description="How many fee-bearing regulatory snapshots were requested."
    )


class MarketDataTypeOut(BaseModel):
    """The market data type the connection now requests."""

    data_type: MarketDataTypeName
    code: int = Field(description="TWS API code: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen.")
    previous: MarketDataTypeName | None = None
    note: str


# --- streams -----------------------------------------------------------------------------


class MarketDataNotice(BaseModel):
    """A message IBKR sent about one stream (a warning, or the error that stopped it)."""

    code: int = Field(description="TWS API error or warning code.")
    message: str
    hint: str | None = Field(None, description="What it means and what to do.")
    at: datetime = Field(description="When it arrived (UTC).")
    fatal: bool = Field(description="True when the stream stopped because of it.")


class StreamStatus(BaseModel):
    """Fields every market-data stream snapshot carries."""

    contract: ContractOut
    active: bool = Field(
        True, description="False once IBKR stopped the stream with an error (see error)."
    )
    error: MarketDataNotice | None = Field(None, description="The error that stopped the stream.")
    notices: list[MarketDataNotice] = Field(
        default_factory=list, description="Recent warnings from IBKR about this stream."
    )
    truncated: bool = Field(
        False, description="True when older items were left out (see limit and since)."
    )


class DividendsOut(BaseModel):
    """Dividend summary (generic tick ``dividends``)."""

    past_12_months: float | None = None
    next_12_months: float | None = None
    next_date: date | None = None
    next_amount: float | None = None


class QuoteExtras(BaseModel):
    """Values from the generic ticks a quote stream asked for; null when not (yet) sent."""

    put_volume: float | None = Field(None, description="option_volume: today's put volume.")
    call_volume: float | None = Field(None, description="option_volume: today's call volume.")
    put_open_interest: float | None = Field(None, description="option_open_interest (puts).")
    call_open_interest: float | None = Field(None, description="option_open_interest (calls).")
    historical_volatility: float | None = Field(
        None, description="historical_volatility: 30-day historical volatility (decimal)."
    )
    avg_option_volume: float | None = Field(
        None, description="avg_option_volume: 90-day average option volume."
    )
    implied_volatility: float | None = Field(
        None, description="implied_volatility: 30-day implied volatility of the underlying."
    )
    index_future_premium: float | None = None
    low_13_week: float | None = Field(None, description="misc_stats")
    high_13_week: float | None = Field(None, description="misc_stats")
    low_26_week: float | None = Field(None, description="misc_stats")
    high_26_week: float | None = Field(None, description="misc_stats")
    low_52_week: float | None = Field(None, description="misc_stats")
    high_52_week: float | None = Field(None, description="misc_stats")
    avg_volume: float | None = Field(None, description="misc_stats: 90-day average volume.")
    mark_price: float | None = Field(None, description="mark_price: IBKR's mark price.")
    auction_volume: float | None = None
    auction_price: float | None = None
    auction_imbalance: float | None = None
    rt_volume: float | None = Field(None, description="rt_volume: cumulative volume (T&S).")
    rt_time: datetime | None = None
    vwap: float | None = Field(None, description="rt_volume: today's VWAP.")
    shortable: float | None = Field(
        None,
        description=(
            "shortable: >2.5 at least 1000 shares available to short, 1.5-2.5 shares must "
            "be located first, <=1.5 not shortable."
        ),
    )
    shortable_shares: float | None = Field(None, description="shortable: shares available.")
    fundamental_ratios: dict[str, float | str | None] | None = Field(
        None, description="fundamental_ratios: Refinitiv ratios (P/E, EPS, ...)."
    )
    trade_count: float | None = None
    trade_rate: float | None = Field(None, description="Trades per minute.")
    volume_rate: float | None = Field(None, description="Shares per minute.")
    rt_trade_volume: float | None = None
    rt_historical_volatility: float | None = None
    dividends: DividendsOut | None = None
    futures_open_interest: float | None = None
    last_rth_trade: float | None = Field(
        None, description="last_rth_trade: last trade price in regular trading hours."
    )
    bond_factor_multiplier: float | None = Field(
        None, description="bond_factor_multiplier: remaining principal factor of a bond."
    )
    etf_nav_bid: float | None = Field(None, description="etf_nav_bid_ask: NAV-based bid.")
    etf_nav_ask: float | None = Field(None, description="etf_nav_bid_ask: NAV-based ask.")
    etf_nav_last: float | None = Field(None, description="etf_nav_last: intraday NAV.")
    etf_nav_close: float | None = Field(None, description="etf_nav_close: NAV at the close.")
    etf_nav_prior_close: float | None = Field(
        None, description="etf_nav_close: NAV at the prior close."
    )
    etf_nav_high: float | None = Field(None, description="etf_nav_high_low: today's NAV high.")
    etf_nav_low: float | None = Field(None, description="etf_nav_high_low: today's NAV low.")
    etf_nav_frozen_last: float | None = Field(
        None, description="etf_nav_frozen_last: last NAV outside the NAV hours."
    )
    estimated_ipo_midpoint: float | None = Field(
        None, description="ipo_prices: midpoint of the expected IPO price range."
    )
    final_ipo_last: float | None = Field(None, description="ipo_prices: final IPO price.")
    volume_3_min: float | None = Field(None, description="short_term_volume: last 3 minutes.")
    volume_5_min: float | None = Field(None, description="short_term_volume: last 5 minutes.")
    volume_10_min: float | None = Field(None, description="short_term_volume: last 10 minutes.")


class QuoteStreamData(StreamStatus):
    """A live top-of-book quote (``kind`` quotes)."""

    quote: QuoteOut
    generic_ticks: list[GenericTick] = Field(default_factory=list)
    extras: QuoteExtras | None = Field(None, description="Present when generic_ticks were asked.")
    updates: int = Field(0, description="Updates received since the stream opened.")


class DepthLevel(BaseModel):
    """One price level of an order book side (position 0 is the best price)."""

    position: int
    price: float | None
    size: float | None
    market_maker: str | None = Field(None, description="Venue or market maker (smart depth).")


class DepthData(StreamStatus):
    """The order book (``kind`` depth)."""

    rows: int = Field(description="Levels requested per side.")
    smart_depth: bool
    bids: list[DepthLevel]
    asks: list[DepthLevel]
    time: datetime | None = Field(None, description="Last update (UTC).")


class TradeTick(BaseModel):
    """A trade (tick types Last and AllLast)."""

    time: datetime = Field(description="When ib_async received it (UTC).")
    price: float | None
    size: float | None
    exchange: str | None = None
    special_conditions: str | None = None
    past_limit: bool = False
    unreported: bool = False


class BidAskTick(BaseModel):
    """A quote change (tick type BidAsk)."""

    time: datetime = Field(description="When ib_async received it (UTC).")
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    bid_past_low: bool = False
    ask_past_high: bool = False


class MidPointTick(BaseModel):
    """A midpoint change (tick type MidPoint)."""

    time: datetime = Field(description="When ib_async received it (UTC).")
    mid_point: float | None


class TickByTickData(StreamStatus):
    """The newest tick-by-tick ticks, oldest first (``kind`` tick_by_tick)."""

    tick_type: TickByTickType
    ignore_size: bool
    buffer_size: int = Field(description="How many ticks the server keeps.")
    ticks: list[TradeTick | BidAskTick | MidPointTick]
    received: int = Field(0, description="Ticks received since the stream opened.")


class RealtimeBar(BaseModel):
    """One 5-second bar."""

    time: datetime = Field(description="Bar start (UTC).")
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    wap: float | None = Field(None, description="Volume-weighted average price.")
    count: int | None = Field(None, description="Number of trades.")


class RealtimeBarsData(StreamStatus):
    """The newest 5-second bars, oldest first (``kind`` realtime_bars)."""

    what_to_show: RealtimeWhatToShow
    use_rth: bool
    buffer_size: int
    bars: list[RealtimeBar]


class StreamBar(BaseModel):
    """One bar of a live-updating bar series."""

    time: datetime = Field(description="Bar start (UTC); daily and longer bars at 00:00.")
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    average: float | None = Field(None, description="Average (VWAP-like) price of the bar.")
    bar_count: int | None = Field(None, description="Number of trades.")


class LiveBarsData(StreamStatus):
    """Historical bars whose last bar keeps updating (``kind`` bars), oldest first."""

    bar_size: str
    duration: str
    what_to_show: WhatToShow
    use_rth: bool
    bars: list[StreamBar]


# --- subscription management -------------------------------------------------------------


class SubscriptionEntry(BaseModel):
    """One open subscription, as ``list_subscriptions`` shows it."""

    subscription_id: str
    kind: str
    key: str
    contract: ContractOut | None = None
    created_at: datetime
    last_read_at: datetime | None = None
    idle_expires_at: datetime = Field(description="When it is cancelled unless read before (UTC).")
    stale: bool = Field(False, description="True while the stream is not flowing.")
    params: dict[str, Any] = Field(
        default_factory=dict, description="Request parameters (generic ticks, rows, ...)."
    )


class SubscriptionList(BaseModel):
    """Open subscriptions and how much capacity is left."""

    subscriptions: list[SubscriptionEntry]
    used: int
    max: int = Field(description="IBKR_MCP_MAX_SUBSCRIPTIONS.")
    depth_used: int
    depth_max: int
    tick_by_tick_used: int
    tick_by_tick_max: int
    idle_ttl_s: float
    market_data_type: MarketDataTypeName | None = Field(
        None, description="What later market data requests on this connection get."
    )


class CancelledSubscription(BaseModel):
    """A subscription that was cancelled."""

    subscription_id: str
    kind: str
    key: str


class UnsubscribeResult(BaseModel):
    """What ``unsubscribe`` cancelled."""

    cancelled: list[CancelledSubscription]
    remaining: int = Field(description="Subscriptions still open.")
