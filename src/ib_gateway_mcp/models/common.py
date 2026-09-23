"""Models shared by every domain: contracts, enums, quotes, subscriptions, truncation and
confirmation requests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

__all__ = [
    "MARKET_DATA_TYPE_CODES",
    "MARKET_DATA_TYPE_NAMES",
    "Action",
    "BarSize",
    "ComboLegOut",
    "ComboLegSpec",
    "ConfirmationRequest",
    "ContractOut",
    "ContractSpec",
    "LiveBarSize",
    "MarketDataTypeName",
    "OptionGreeks",
    "PlainText",
    "QuoteOut",
    "RealtimeWhatToShow",
    "Right",
    "SecIdType",
    "SecType",
    "SubscriptionDataOut",
    "SubscriptionKind",
    "SubscriptionOut",
    "Truncatable",
    "WhatToShow",
    "quoted",
]


def _one_printable_line(value: str) -> str:
    if not value.isprintable():
        raise ValueError(
            "must be one line of printable text: no line breaks, tabs, control or "
            "invisible formatting characters"
        )
    return value


PlainText = Annotated[str, AfterValidator(_one_printable_line)]
"""Free text that stays on one visible line (``str.isprintable``: no line breaks, tabs,
control, format or separator characters other than the space). Used for model-supplied
text that can end up in a human confirmation prompt."""


def quoted(text: str) -> str:
    """``text`` in double quotes, with quotes, backslashes and anything invisible escaped.

    For values the requester (the model) supplied that appear in text a human approves:
    the quotes mark where such a value starts and ends, and nothing inside can start a
    new line or hide characters.
    """
    out: list[str] = []
    for char in text:
        if char in '"\\':
            out.append("\\" + char)
        elif char.isprintable():
            out.append(char)
        elif ord(char) <= 0xFFFF:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(f"\\U{ord(char):08x}")
    return '"' + "".join(out) + '"'


SecType = Literal[
    "STK",
    "OPT",
    "FUT",
    "CONTFUT",
    "CASH",
    "IND",
    "CFD",
    "BOND",
    "CMDTY",
    "FOP",
    "FUND",
    "WAR",
    "IOPT",
    "BAG",
    "CRYPTO",
    "NEWS",
    "EVENT",
]
"""IBKR security types (``Contract.secType``)."""

Right = Literal["C", "P"]
"""Option right: call or put."""

Action = Literal["BUY", "SELL"]
"""Order or combo-leg side."""

SecIdType = Literal["ISIN", "CUSIP", "FIGI"]
"""Kinds of security identifier IBKR accepts for contract lookups (``Contract.secIdType``)."""

BarSize = Literal[
    "1 secs",
    "5 secs",
    "10 secs",
    "15 secs",
    "30 secs",
    "1 min",
    "2 mins",
    "3 mins",
    "5 mins",
    "10 mins",
    "15 mins",
    "20 mins",
    "30 mins",
    "1 hour",
    "2 hours",
    "3 hours",
    "4 hours",
    "8 hours",
    "1 day",
    "1 week",
    "1 month",
]
"""Bar sizes IBKR accepts for historical bars, spelled exactly as the TWS API wants them.

Bars of 30 seconds or less fall under IBKR's strict historical pacing rules (about 60
requests per 10 minutes) and only reach back about six months.
"""

LiveBarSize = Literal[
    "5 secs",
    "10 secs",
    "15 secs",
    "30 secs",
    "1 min",
    "2 mins",
    "3 mins",
    "5 mins",
    "10 mins",
    "15 mins",
    "20 mins",
    "30 mins",
    "1 hour",
    "2 hours",
    "3 hours",
    "4 hours",
    "8 hours",
    "1 day",
    "1 week",
    "1 month",
]
"""The :data:`BarSize` values IBKR keeps updating live (``keepUpToDate``): all but 1 secs."""

WhatToShow = Literal[
    "TRADES",
    "MIDPOINT",
    "BID",
    "ASK",
    "BID_ASK",
    "ADJUSTED_LAST",
    "HISTORICAL_VOLATILITY",
    "OPTION_IMPLIED_VOLATILITY",
    "REBATE_RATE",
    "FEE_RATE",
    "YIELD_BID",
    "YIELD_ASK",
    "YIELD_BID_ASK",
    "YIELD_LAST",
    "AGGTRADES",
]
"""Data a historical bar is built from.

TRADES: traded prices and volume (not for forex). MIDPOINT, BID, ASK: quote-based bars.
BID_ASK: time-averaged bid (as low/open) and ask (as high/close); counts double for pacing.
ADJUSTED_LAST: trades adjusted for splits and dividends (needs an empty end time).
HISTORICAL_VOLATILITY and OPTION_IMPLIED_VOLATILITY: for stocks and indexes.
REBATE_RATE and FEE_RATE: stock-loan rates. YIELD_*: bonds. AGGTRADES: crypto trades.
Real-time 5-second bars accept only :data:`RealtimeWhatToShow`.
"""

RealtimeWhatToShow = Literal["TRADES", "MIDPOINT", "BID", "ASK"]
"""Data a real-time 5-second bar is built from (a subset of :data:`WhatToShow`)."""

MarketDataTypeName = Literal["live", "frozen", "delayed", "delayed_frozen"]
"""Kinds of market data IBKR sends.

live: real-time (needs a market-data subscription). frozen: the last live values from
the close. delayed: 15-20 minutes late, free for most exchanges. delayed_frozen: the last
delayed values.
"""

MARKET_DATA_TYPE_CODES: Mapping[MarketDataTypeName, int] = MappingProxyType(
    {"live": 1, "frozen": 2, "delayed": 3, "delayed_frozen": 4}
)
"""TWS API codes for :data:`MarketDataTypeName` (``reqMarketDataType``)."""

MARKET_DATA_TYPE_NAMES: Mapping[int, MarketDataTypeName] = MappingProxyType(
    {code: name for name, code in MARKET_DATA_TYPE_CODES.items()}
)
"""Names for TWS API market data type codes 1-4 (``Ticker.marketDataType``)."""

SubscriptionKind = Literal[
    "quotes",
    "depth",
    "tick_by_tick",
    "realtime_bars",
    "bars",
    "scanner",
    "news_bulletins",
    "news",
    "display_group",
]
"""The ``kind`` of every subscription the services open (``SubscriptionRegistry.add``).

P&L is not among them: ``get_pnl`` and ``get_position_pnl`` subscribe, wait for IBKR's
first update and cancel within the call, so no P&L stream outlives a request."""

_RIGHT_ALIASES = {"C": "C", "CALL": "C", "P": "P", "PUT": "P"}


class ComboLegSpec(BaseModel):
    """One leg of a combo (``BAG``) contract, referenced by contract id."""

    model_config = ConfigDict(extra="forbid")

    con_id: int = Field(gt=0, description="IBKR contract id of the leg (from contract details).")
    ratio: int = Field(1, ge=1, description="Leg ratio relative to the other legs.")
    action: Action = Field(description="BUY or SELL for this leg when the combo is bought.")
    exchange: str = Field("SMART", description="Routing exchange for the leg.")


class ContractSpec(BaseModel):
    """Describes an instrument the way a person would, for tools to resolve at IBKR.

    Give a ``con_id`` when you have one; it is unambiguous. Otherwise give at least a
    ``symbol`` (or ``local_symbol``) plus ``sec_type``, and for derivatives the expiry,
    strike and right. An ISIN, CUSIP or FIGI (``sec_id_type`` plus ``sec_id``) or a bond
    ``issuer_id`` also works without a symbol. Combos (``sec_type="BAG"``) need
    ``combo_legs``.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str | None = Field(None, description="Ticker or underlying symbol, e.g. AAPL or ES.")
    sec_type: SecType = Field("STK", description="Security type: STK, OPT, FUT, CASH, BAG, ...")
    exchange: str = Field("SMART", description="Destination exchange; SMART for IBKR routing.")
    currency: str = Field("USD", description="Trading currency (ISO code).")
    primary_exchange: str | None = Field(
        None, description="Listing exchange, to disambiguate SMART-routed stocks (e.g. NASDAQ)."
    )
    last_trade_date_or_contract_month: str | None = Field(
        None, description="Expiry as YYYYMMDD, or contract month as YYYYMM (options, futures)."
    )
    strike: float | None = Field(None, gt=0, description="Option strike price.")
    right: Right | None = Field(None, description="Option right: C (call) or P (put).")
    multiplier: str | None = Field(None, description="Contract multiplier, e.g. 100.")
    trading_class: str | None = Field(None, description="Trading class, e.g. SPXW or FGBL.")
    local_symbol: str | None = Field(
        None, description="Exchange-local symbol, e.g. an OCC option symbol or ESZ6."
    )
    con_id: int | None = Field(None, gt=0, description="IBKR contract id; unambiguous when given.")
    sec_id_type: SecIdType | None = Field(
        None, description="Kind of identifier in sec_id: ISIN, CUSIP or FIGI (needs sec_id)."
    )
    sec_id: str | None = Field(
        None,
        description=(
            "Security identifier, e.g. ISIN US0378331005 (needs sec_id_type). Enough on its "
            "own to find an instrument; add exchange and currency to pick one listing."
        ),
    )
    issuer_id: str | None = Field(
        None, description="IBKR bond issuer id (sec_type BOND), e.g. from contract details."
    )
    include_expired: bool = Field(
        False,
        description=(
            "Also match expired futures (contract details, historical data). IBKR offers "
            "no data on expired options."
        ),
    )
    combo_legs: list[ComboLegSpec] = Field(
        default_factory=list, description="Legs of a BAG (combo) contract."
    )

    @field_validator("right", mode="before")
    @classmethod
    def _normalize_right(cls, value: object) -> object:
        if isinstance(value, str):
            return _RIGHT_ALIASES.get(value.strip().upper(), value)
        return value

    @field_validator("exchange", "currency", "primary_exchange", "sec_id_type", mode="before")
    @classmethod
    def _uppercase_codes(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("sec_id", "issuer_id", mode="before")
    @classmethod
    def _strip_identifiers(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def _check_identifiable(self) -> Self:
        if self.sec_type == "BAG":
            if len(self.combo_legs) < 2:
                raise ValueError("a BAG (combo) contract needs at least two combo_legs")
        elif self.combo_legs:
            raise ValueError("combo_legs are only valid with sec_type BAG")
        if (self.sec_id is None) != (self.sec_id_type is None):
            raise ValueError("sec_id and sec_id_type go together")
        if not (self.con_id or self.symbol or self.local_symbol or self.sec_id or self.issuer_id):
            raise ValueError(
                "give at least one of con_id, symbol, local_symbol, sec_id (with sec_id_type) "
                "or issuer_id"
            )
        return self


class ComboLegOut(BaseModel):
    """A combo leg as reported by IBKR."""

    con_id: int
    ratio: int
    action: str
    exchange: str


class ContractOut(BaseModel):
    """A contract as IBKR knows it; ``con_id`` identifies it in later calls."""

    con_id: int | None = Field(description="IBKR contract id (None if not yet qualified).")
    symbol: str
    sec_type: str
    exchange: str | None = None
    primary_exchange: str | None = None
    currency: str | None = None
    local_symbol: str | None = None
    trading_class: str | None = None
    last_trade_date_or_contract_month: str | None = None
    strike: float | None = None
    right: str | None = None
    multiplier: str | None = None
    description: str | None = None
    combo_legs: list[ComboLegOut] = Field(default_factory=list)


class OptionGreeks(BaseModel):
    """IBKR's option model values: implied volatility, greeks, and the prices behind them.

    Any value IBKR has not computed (yet) is null.
    """

    implied_vol: float | None = Field(
        None, description="Implied volatility, annualized, as a decimal (0.25 = 25%)."
    )
    delta: float | None = Field(None, description="Option price change per 1.00 underlying move.")
    gamma: float | None = Field(None, description="Delta change per 1.00 underlying move.")
    vega: float | None = Field(
        None, description="Option price change per 1 percentage point of volatility."
    )
    theta: float | None = Field(None, description="Option price change per calendar day.")
    opt_price: float | None = Field(None, description="Option price the values were computed at.")
    pv_dividend: float | None = Field(
        None, description="Present value of dividends expected before expiry."
    )
    und_price: float | None = Field(None, description="Underlying price the values assume.")


class QuoteOut(BaseModel):
    """A top-of-book snapshot for one contract.

    Prices are in the contract's currency; null means IBKR sent no value (no market, no
    permission for that field, or not traded yet). ``market_data_type`` says whether the
    values are live, frozen or delayed.
    """

    contract: ContractOut
    bid: float | None = Field(None, description="Best bid; null when there is no bid.")
    ask: float | None = Field(None, description="Best ask; null when there is no offer.")
    last: float | None = Field(None, description="Last traded price.")
    bid_size: float | None = Field(None, description="Size at the best bid (0 when no bid).")
    ask_size: float | None = Field(None, description="Size at the best ask (0 when no offer).")
    last_size: float | None = Field(None, description="Size of the last trade.")
    open: float | None = Field(None, description="Today's opening price.")
    high: float | None = Field(None, description="Today's high.")
    low: float | None = Field(None, description="Today's low.")
    close: float | None = Field(None, description="Previous session's closing price.")
    volume: float | None = Field(None, description="Today's volume.")
    halted: bool | None = Field(
        None, description="True while trading is halted; null when IBKR does not say."
    )
    market_data_type: MarketDataTypeName | None = Field(
        None, description="live, frozen, delayed or delayed_frozen."
    )
    bbo_exchange: str | None = Field(
        None, description="SMART BBO exchange code; get_smart_components expands it."
    )
    time: datetime | None = Field(None, description="When the quote was last updated (UTC).")
    greeks: OptionGreeks | None = Field(
        None, description="IBKR model greeks and implied volatility (options only)."
    )


class SubscriptionOut(BaseModel):
    """A handle to a server-side stream, as every ``subscribe_*`` tool returns it.

    Read the stream with ``get_subscription_data(subscription_id)`` and stop it with
    ``unsubscribe``. Streams nobody reads for ``idle_ttl_s`` seconds are cancelled.
    """

    subscription_id: str = Field(description="Handle for get_subscription_data and unsubscribe.")
    kind: str = Field(
        description=(
            "What streams: quotes, depth, tick_by_tick, realtime_bars, bars, scanner, "
            "news_bulletins, news or display_group."
        )
    )
    key: str = Field(description="What it streams for: the contract id plus parameters.")
    contract: ContractOut | None = Field(None, description="The instrument, when there is one.")
    created_at: datetime
    idle_ttl_s: float = Field(description="Seconds without a read before it is cancelled.")
    deduplicated: bool = Field(
        False,
        description=(
            "True when an identical stream was already open and its handle was returned "
            "instead of opening a second one."
        ),
    )


class SubscriptionDataOut(BaseModel):
    """The latest state of one subscription.

    The shape of ``data`` depends on ``kind`` (a quote, an order book, the newest ticks or
    bars, scanner rows, bulletins, headlines, a display group). When ``stale`` is
    true the stream is not flowing (the gateway connection dropped or IBKR lost market
    data) and the values may be old.
    """

    subscription_id: str
    kind: str
    stale: bool = Field(False, description="True when the values may be old; see get_health.")
    created_at: datetime
    last_read_at: datetime | None = Field(
        None, description="The previous read, before this one (UTC)."
    )
    data: dict[str, Any] = Field(description="The snapshot; its fields depend on kind.")


class Truncatable(BaseModel):
    """Base for list results that honour a ``limit``.

    ``truncated`` is True when more items were available than were returned.
    """

    truncated: bool = Field(False, description="True when the result was cut to the limit.")


class ConfirmationRequest(BaseModel):
    """What a human has to approve before a destructive action reaches a live account.

    Services build this from stored state (for orders: the previewed order behind a
    token) without side effects, so the MCP layer can ask the same question on every
    round of an elicitation. The text must be deterministic for a given action.
    """

    account: str = Field(description="Account the action applies to.")
    is_paper: bool = Field(description="True for paper accounts, which never need confirmation.")
    action: str = Field(description="One-line summary, e.g. 'BUY 10 AAPL LMT 150.00 DAY'.")
    details: list[str] = Field(
        default_factory=list, description="Further lines to show: what-if margin, commission."
    )
