"""Models for historical bars, ticks, head timestamps, histograms and trading schedules."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from ib_gateway_mcp.models.common import ContractOut, Truncatable

__all__ = [
    "Bar",
    "BarList",
    "HeadTimestamp",
    "Histogram",
    "HistogramEntry",
    "HistoricalTickList",
    "HistoricalTickOut",
    "HistoricalTickType",
    "ScheduleSession",
    "TradingSchedule",
]

HistoricalTickType = Literal["TRADES", "BID_ASK", "MIDPOINT"]
"""Kinds of historical tick IBKR serves: trades, quote changes, or midpoints."""


# --- get_historical_bars --------------------------------------------------------------------


class Bar(BaseModel):
    """One OHLCV bar. Prices are in the contract's currency; null means IBKR sent none."""

    time: date | datetime = Field(
        description=(
            "Bar start: a UTC datetime for intraday bars, or the trading date for daily, "
            "weekly and monthly bars."
        )
    )
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = Field(
        None, description="Traded volume; null for quote-based bars (MIDPOINT, BID, ASK...)."
    )
    wap: float | None = Field(
        None, description="Volume-weighted average price; null when the bar has no volume."
    )
    bar_count: int | None = Field(
        None, description="Number of trades in the bar; null for quote-based bars."
    )


class BarList(Truncatable):
    """Historical bars for one contract, oldest first."""

    contract: ContractOut
    bar_size: str
    duration: str = Field(description="The duration sent to IBKR, e.g. '5 D' or '1800 S'.")
    what_to_show: str
    use_rth: bool = Field(description="True when only regular trading hours are included.")
    end: datetime | None = Field(None, description="Requested end (UTC); null means now.")
    total: int = Field(description="How many bars IBKR returned before the limit.")
    bars: list[Bar] = Field(
        description="Oldest first; when truncated, the newest bars are the ones kept."
    )


# --- get_historical_ticks -------------------------------------------------------------------


class HistoricalTickOut(BaseModel):
    """One historical tick: a trade (TRADES), a quote change (BID_ASK) or a midpoint.

    Fields that do not apply to the tick's kind are null.
    """

    time: datetime = Field(description="When it happened (UTC, whole seconds).")
    price: float | None = Field(None, description="Trade price (TRADES) or midpoint (MIDPOINT).")
    size: float | None = Field(None, description="Trade size (TRADES).")
    exchange: str | None = Field(None, description="Exchange that reported the trade.")
    special_conditions: str | None = Field(
        None, description="Trade condition codes, as the exchange reported them."
    )
    past_limit: bool | None = Field(
        None, description="TRADES: the trade was outside the day's price limits."
    )
    unreported: bool | None = Field(
        None, description="TRADES: an unreported trade (e.g. dark pool, delayed report)."
    )
    bid: float | None = Field(None, description="BID_ASK: best bid.")
    ask: float | None = Field(None, description="BID_ASK: best ask.")
    bid_size: float | None = Field(None, description="BID_ASK: size at the best bid.")
    ask_size: float | None = Field(None, description="BID_ASK: size at the best ask.")
    bid_past_low: bool | None = Field(None, description="BID_ASK: the bid is below the day's low.")
    ask_past_high: bool | None = Field(
        None, description="BID_ASK: the ask is above the day's high."
    )


class HistoricalTickList(Truncatable):
    """Historical ticks for one contract, oldest first."""

    truncated: bool = Field(
        False,
        description=(
            "True when IBKR returned the full count, so more ticks probably exist: page on "
            "with start set to the last tick's time (or end set to the first tick's time)."
        ),
    )
    contract: ContractOut
    what_to_show: HistoricalTickType
    use_rth: bool
    start: datetime | None = Field(None, description="Requested start (UTC), if given.")
    end: datetime | None = Field(None, description="Requested end (UTC), if given.")
    count: int = Field(description="How many ticks were requested.")
    ticks: list[HistoricalTickOut]


# --- get_head_timestamp ---------------------------------------------------------------------


class HeadTimestamp(BaseModel):
    """The earliest point IBKR has historical data for, for one contract and data type."""

    contract: ContractOut
    what_to_show: str
    use_rth: bool
    earliest: datetime = Field(description="Earliest available data (UTC).")


# --- get_histogram --------------------------------------------------------------------------


class HistogramEntry(BaseModel):
    """One price level of the distribution."""

    price: float
    count: int = Field(description="IBKR's count at this price over the period (traded volume).")


class Histogram(Truncatable):
    """How trading was distributed over price levels during a period."""

    contract: ContractOut
    period: str = Field(description="The period sent to IBKR, e.g. '1 week'.")
    use_rth: bool
    total: int = Field(description="How many price levels IBKR returned before the limit.")
    entries: list[HistogramEntry] = Field(
        description=(
            "Sorted by price, lowest first. When truncated, the busiest price levels (highest "
            "count) are the ones kept."
        )
    )


# --- get_trading_schedule -------------------------------------------------------------------


class ScheduleSession(BaseModel):
    """One trading session, in the exchange's time zone (the offset is included).

    Times are null only when IBKR's time zone name could not be interpreted.
    """

    start: datetime | None
    end: datetime | None
    ref_date: date | None = Field(
        None, description="The trading date the session belongs to (overnight sessions)."
    )


class TradingSchedule(BaseModel):
    """Trading sessions for a range of days."""

    contract: ContractOut
    time_zone: str = Field(description="The exchange's time zone, as IBKR names it.")
    use_rth: bool = Field(description="True: regular hours only; false: including extended hours.")
    start: datetime | None = Field(None, description="Start of the covered range.")
    end: datetime | None = Field(None, description="End of the covered range.")
    sessions: list[ScheduleSession] = Field(description="Oldest first.")
