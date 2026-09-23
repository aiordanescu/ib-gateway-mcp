"""Models for contract search, details, qualification, option chains and market rules."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from ib_gateway_mcp.models.common import ContractOut, Truncatable

__all__ = [
    "BondDetails",
    "ContractDetailsList",
    "ContractDetailsOut",
    "DepthExchange",
    "DepthExchangeList",
    "MarketRule",
    "MarketRuleList",
    "OptionChainList",
    "OptionChainOut",
    "PriceIncrementOut",
    "SmartComponentList",
    "SmartComponentOut",
    "SymbolMatch",
    "SymbolSearchResult",
    "TradingSessionOut",
]


# --- search_symbols -----------------------------------------------------------------------


class SymbolMatch(BaseModel):
    """One instrument IBKR suggests for a search pattern."""

    contract: ContractOut = Field(
        description="The instrument; description holds the company or instrument name."
    )
    derivative_sec_types: list[str] = Field(
        default_factory=list,
        description="Derivative types listed on it, e.g. OPT, FUT, WAR, CFD, IOPT, BAG.",
    )
    issuer_id: str | None = Field(None, description="IBKR bond issuer id (bonds only).")


class SymbolSearchResult(Truncatable):
    """Instruments matching a symbol or name pattern, best matches first."""

    pattern: str
    total: int = Field(description="How many matches IBKR returned before the limit.")
    matches: list[SymbolMatch]


# --- get_contract_details -------------------------------------------------------------------


class TradingSessionOut(BaseModel):
    """One trading session, in the exchange's time zone (the offset is included)."""

    start: datetime
    end: datetime


class BondDetails(BaseModel):
    """Bond terms from IBKR's contract details (bonds only).

    Without bond reference data on the login, IBKR sends the bond with its terms left
    empty (no coupon, maturity, ratings or type) and an ``IBCID...`` placeholder in
    ``cusip``; ``desc_append`` (e.g. ``IBM 7 10/30/45``) still names coupon and maturity.
    """

    cusip: str | None = None
    ratings: str | None = None
    desc_append: str | None = Field(None, description="Extra description text.")
    bond_type: str | None = None
    coupon_type: str | None = None
    coupon: float | None = Field(
        None, description="Coupon rate in percent; null when IBKR sent no bond terms."
    )
    maturity: str | None = Field(None, description="Maturity date as reported (YYYYMMDD).")
    issue_date: str | None = None
    callable: bool = False
    putable: bool = False
    convertible: bool = False
    next_option_date: str | None = Field(None, description="Next call or put date.")
    next_option_type: str | None = None
    next_option_partial: bool = False
    notes: str | None = None


class ContractDetailsOut(BaseModel):
    """Everything IBKR reports about one contract.

    ``contract.description`` is the long name (e.g. APPLE INC). Trading and liquid hours
    are parsed into sessions in the exchange's time zone; when IBKR's text cannot be
    parsed, the sessions are null and the raw text is in ``trading_hours`` /
    ``liquid_hours`` instead.
    """

    contract: ContractOut
    market_name: str | None = Field(None, description="Market name, often the trading class.")
    industry: str | None = None
    category: str | None = None
    subcategory: str | None = None
    stock_type: str | None = Field(None, description="Stock type, e.g. COMMON, ETF, ADR, REIT.")
    contract_month: str | None = Field(None, description="Contract month (derivatives).")
    real_expiration_date: str | None = Field(
        None, description="Actual expiration date (YYYYMMDD); can differ from the last trade date."
    )
    last_trade_time: str | None = Field(None, description="Time of day trading stops (expiry).")
    under_con_id: int | None = Field(None, description="Underlying contract id (derivatives).")
    under_symbol: str | None = None
    under_sec_type: str | None = None
    time_zone_id: str | None = Field(None, description="Exchange time zone, e.g. US/Eastern.")
    trading_sessions: list[TradingSessionOut] | None = Field(
        None,
        description=(
            "Upcoming sessions including extended hours (about a week); null when IBKR's "
            "text could not be parsed (see trading_hours)."
        ),
    )
    liquid_sessions: list[TradingSessionOut] | None = Field(
        None, description="Upcoming regular (liquid) sessions; null when not parsed."
    )
    closed_dates: list[date] = Field(
        default_factory=list, description="Days in the trading-hours window the market is closed."
    )
    trading_hours: str | None = Field(
        None, description="IBKR's raw trading-hours text; only set when it could not be parsed."
    )
    liquid_hours: str | None = Field(
        None, description="IBKR's raw liquid-hours text; only set when it could not be parsed."
    )
    min_tick: float | None = Field(
        None, description="Smallest price increment; market rules can vary it by price level."
    )
    price_magnifier: int | None = Field(
        None,
        description=(
            "IBKR's price magnifier, which keeps execution and strike prices consistent with "
            "market data for some contracts; usually 1."
        ),
    )
    min_size: float | None = Field(None, description="Smallest order size.")
    size_increment: float | None = Field(None, description="Order size step.")
    suggested_size_increment: float | None = None
    valid_exchanges: list[str] = Field(
        default_factory=list, description="Exchanges the contract can be routed to."
    )
    market_rule_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Market rule id per valid exchange, in the same order as valid_exchanges; "
            "get_market_rule expands them into price increments."
        ),
    )
    order_types: list[str] = Field(
        default_factory=list, description="Order types IBKR accepts for this contract."
    )
    sec_ids: dict[str, str] = Field(
        default_factory=dict, description="Security identifiers, e.g. {'ISIN': 'US0378331005'}."
    )
    ev_rule: str | None = Field(None, description="Economic value rule (some derivatives).")
    ev_multiplier: float | None = None
    agg_group: int | None = Field(None, description="Aggregated group id of the contract.")
    bond: BondDetails | None = Field(None, description="Bond terms (bonds only).")


class ContractDetailsList(Truncatable):
    """Contract details for every instrument a (possibly partial) spec matches."""

    total: int = Field(description="Distinct contracts IBKR matched before the limit.")
    contracts: list[ContractDetailsOut]


# --- get_option_chain -----------------------------------------------------------------------


class OptionChainOut(BaseModel):
    """Expirations and strikes of one option class; identical listings are merged."""

    exchanges: list[str] = Field(description="Exchanges listing exactly this chain.")
    trading_class: str = Field(description="Trading class, e.g. SPX (monthly) or SPXW (weekly).")
    multiplier: str | None = Field(None, description="Contract multiplier, e.g. 100.")
    expirations: list[str] = Field(description="Expiry dates, YYYYMMDD, ascending.")
    strikes: list[float] = Field(
        description="Every strike across all expirations, ascending (not all exist per expiry)."
    )


class OptionChainList(BaseModel):
    """The option chains IBKR lists for an underlying (no prices)."""

    underlying: ContractOut
    chains: list[OptionChainOut]


# --- get_market_rule -----------------------------------------------------------------------


class PriceIncrementOut(BaseModel):
    """From ``low_edge`` upwards (until the next row), prices move in steps of ``increment``."""

    low_edge: float
    increment: float


class MarketRule(BaseModel):
    """A market rule: the tick ladder for one exchange and contract."""

    market_rule_id: int
    increments: list[PriceIncrementOut]


class MarketRuleList(BaseModel):
    """Market rules in the order requested."""

    rules: list[MarketRule]
    missing_ids: list[int] = Field(
        default_factory=list,
        description="Ids IBKR did not answer within 1 second (probably unknown ids).",
    )


# --- get_smart_components ------------------------------------------------------------------


class SmartComponentOut(BaseModel):
    """One exchange inside a SMART BBO exchange code."""

    bit_number: int
    exchange: str
    exchange_letter: str = Field(description="Single-letter code IBKR uses for the exchange.")


class SmartComponentList(BaseModel):
    """The exchanges behind a SMART BBO exchange code."""

    bbo_exchange: str
    components: list[SmartComponentOut]


# --- get_depth_exchanges -------------------------------------------------------------------


class DepthExchange(BaseModel):
    """An exchange that offers market depth (level 2) for a security type."""

    exchange: str
    sec_type: str
    listing_exchange: str | None = None
    service_data_type: str | None = Field(
        None, description="IBKR's depth service type for this exchange: Deep or Deep2."
    )
    agg_group: int | None = Field(None, description="Aggregated group id, if any.")


class DepthExchangeList(BaseModel):
    """Exchanges offering market depth; IBKR's list is static per session."""

    exchanges: list[DepthExchange]
