"""Models for account summary and values, positions, portfolio, P&L, executions and orders."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ib_gateway_mcp.models.common import ContractOut, Truncatable

__all__ = [
    "AccountPnl",
    "AccountSummary",
    "AccountValueList",
    "AccountValueOut",
    "CompletedOrder",
    "CompletedOrderList",
    "ExecutionList",
    "ExecutionOut",
    "OpenOrder",
    "OpenOrderList",
    "Portfolio",
    "PortfolioItemOut",
    "PositionList",
    "PositionOut",
    "PositionPnl",
]

_AS_OF = "When this server read the values (UTC)."
_PNL_AS_OF = "When this server read the values (UTC); IBKR refreshes P&L about every second."


class AccountValueOut(BaseModel):
    """One account value: a tag, its value and currency."""

    account: str
    tag: str = Field(description="IBKR tag, e.g. NetLiquidation, BuyingPower, CashBalance.")
    value: str = Field(description="The value exactly as IBKR sent it (numbers and text alike).")
    amount: float | None = Field(
        None, description="The value as a number; null when it is text (e.g. AccountType)."
    )
    currency: str | None = Field(
        None,
        description=(
            "Currency of the value. BASE rows are totals converted to the account's base "
            "currency; null for counts, ratios and text."
        ),
    )
    model_code: str | None = Field(
        None, description="Advisor model code the value is for; null for the whole account."
    )


class AccountSummary(BaseModel):
    """Headline balances and margin figures for one account.

    Amounts are in ``base_currency``. IBKR refreshes the summary about every 3 minutes,
    and immediately after trades.
    """

    account: str
    base_currency: str | None = Field(None, description="Currency the headline amounts are in.")
    net_liquidation: float | None = Field(None, description="Total account value (equity).")
    total_cash_value: float | None = Field(None, description="Cash, including unsettled.")
    settled_cash: float | None = Field(None, description="Settled cash (cash accounts).")
    buying_power: float | None = Field(None, description="What can be bought on margin now.")
    available_funds: float | None = Field(
        None, description="Equity with loan value minus initial margin."
    )
    excess_liquidity: float | None = Field(
        None, description="Equity with loan value minus maintenance margin; <0 risks liquidation."
    )
    equity_with_loan_value: float | None = None
    gross_position_value: float | None = Field(
        None, description="Absolute market value of all positions."
    )
    init_margin_req: float | None = Field(None, description="Initial margin requirement.")
    maint_margin_req: float | None = Field(None, description="Maintenance margin requirement.")
    sma: float | None = Field(None, description="Special memorandum account (Reg T).")
    cushion: float | None = Field(
        None, description="Excess liquidity / net liquidation, as a fraction (0.25 = 25%)."
    )
    leverage: float | None = Field(None, description="Gross position value / net liquidation.")
    day_trades_remaining: int | None = Field(
        None, description="Day trades left under the pattern-day-trader rule; -1 means unlimited."
    )
    values: list[AccountValueOut] = Field(
        default_factory=list,
        description="Every summary row (or the ones matching tags), including per-currency rows.",
    )
    as_of: datetime = Field(description=_AS_OF)


class AccountValueList(Truncatable):
    """Key/value account data for one account (all tags, per currency)."""

    account: str
    model_code: str | None = Field(None, description="Advisor model code, when one was given.")
    values: list[AccountValueOut]
    total: int = Field(description="How many values matched the filters before the limit.")
    as_of: datetime = Field(description=_AS_OF)


class PositionOut(BaseModel):
    """A position: what is held and at what average cost."""

    account: str
    contract: ContractOut = Field(description="The instrument; exchange is not reported.")
    position: float = Field(description="Quantity held; negative for short positions.")
    avg_cost: float | None = Field(
        None,
        description=(
            "Average cost per unit including commissions; for options and futures it "
            "includes the multiplier (per contract, not per share)."
        ),
    )
    model_code: str | None = Field(None, description="Advisor model code, when requested.")


class PositionList(BaseModel):
    """Positions held in one account."""

    account: str
    model_code: str | None = None
    positions: list[PositionOut]
    as_of: datetime = Field(description=_AS_OF)


class PortfolioItemOut(BaseModel):
    """A position with its market value and profit and loss."""

    account: str
    contract: ContractOut
    position: float = Field(description="Quantity held; negative for short positions.")
    market_price: float | None = Field(None, description="IBKR's current price per unit.")
    market_value: float | None = Field(None, description="position x price x multiplier.")
    average_cost: float | None = Field(
        None, description="Average cost per unit (per contract for derivatives)."
    )
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None


class Portfolio(BaseModel):
    """Positions of one account with market values and P&L, in each position's currency."""

    account: str
    items: list[PortfolioItemOut]
    as_of: datetime = Field(description=_AS_OF)


class AccountPnl(BaseModel):
    """Profit and loss for a whole account (or an advisor model), in the base currency."""

    account: str
    model_code: str | None = None
    daily_pnl: float | None = Field(None, description="P&L since the start of today's session.")
    unrealized_pnl: float | None = Field(None, description="Open-position P&L.")
    realized_pnl: float | None = Field(None, description="Closed P&L today.")
    as_of: datetime = Field(description=_PNL_AS_OF)


class PositionPnl(BaseModel):
    """Profit and loss of one position."""

    account: str
    model_code: str | None = None
    contract: ContractOut
    position: float | None = Field(None, description="Quantity held; 0 if closed today.")
    daily_pnl: float | None = Field(None, description="P&L since the start of today's session.")
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    market_value: float | None = Field(None, description="Current market value of the position.")
    as_of: datetime = Field(description=_PNL_AS_OF)


class ExecutionOut(BaseModel):
    """One fill (execution), with its commission when IBKR has reported it."""

    account: str
    exec_id: str = Field(description="IBKR execution id.")
    time: datetime | None = Field(None, description="When it executed (UTC).")
    contract: ContractOut
    side: Literal["BUY", "SELL"] | None = Field(None, description="BUY (bought) or SELL (sold).")
    shares: float = Field(description="Quantity filled in this execution.")
    price: float | None = Field(None, description="Fill price.")
    avg_price: float | None = Field(None, description="Average price of the order so far.")
    cum_qty: float | None = Field(None, description="Quantity of the order filled so far.")
    exchange: str | None = Field(None, description="Where it executed.")
    order_id: int | None = Field(None, description="Order id at the placing API client.")
    perm_id: int | None = Field(None, description="Permanent order id (unique across clients).")
    client_id: int | None = Field(None, description="API client that placed the order.")
    order_ref: str | None = None
    model_code: str | None = None
    liquidation: bool = Field(False, description="True when IBKR liquidated the position.")
    commission: float | None = Field(
        None, description="Commission charged; null until IBKR reports it."
    )
    commission_currency: str | None = None
    realized_pnl: float | None = Field(
        None, description="Realized P&L of this fill (0 when it opened a position)."
    )


class ExecutionList(Truncatable):
    """Executions for one account, newest first."""

    account: str
    executions: list[ExecutionOut]
    total: int = Field(description="How many executions matched before the limit.")


class OpenOrder(BaseModel):
    """A working order."""

    account: str
    order_id: int | None = Field(
        None, description="Order id at the placing API client (null for manual TWS orders)."
    )
    perm_id: int | None = Field(None, description="Permanent order id (unique across clients).")
    client_id: int | None = Field(None, description="API client that placed the order.")
    parent_id: int | None = Field(None, description="Parent order id (brackets).")
    modifiable: bool = Field(
        description=(
            "True when this server placed it (same client id), so it can modify or cancel "
            "it; orders of other clients and manual TWS orders cannot be changed from here."
        )
    )
    contract: ContractOut
    action: str = Field(description="BUY or SELL.")
    quantity: float = Field(description="Total order quantity.")
    order_type: str = Field(description="LMT, MKT, STP, STP LMT, TRAIL, ...")
    limit_price: float | None = None
    aux_price: float | None = Field(
        None, description="Stop price (STP, STP LMT) or trailing amount (TRAIL)."
    )
    trailing_percent: float | None = None
    trail_stop_price: float | None = None
    tif: str | None = Field(None, description="Time in force: DAY, GTC, IOC, GTD, OPG, FOK.")
    good_till_date: str | None = None
    outside_rth: bool = Field(False, description="May fill outside regular trading hours.")
    oca_group: str | None = None
    order_ref: str | None = None
    status: str = Field(description="PreSubmitted, Submitted, PendingSubmit, PendingCancel, ...")
    filled: float = Field(0.0, description="Quantity filled so far.")
    remaining: float = Field(0.0, description="Quantity still working.")
    avg_fill_price: float | None = None
    why_held: str | None = Field(None, description="Why IBKR holds the order, e.g. locate.")


class OpenOrderList(BaseModel):
    """Working orders in one account."""

    account: str
    include_other_clients: bool = Field(
        description="Whether orders of other API clients and manual TWS orders were included."
    )
    orders: list[OpenOrder]
    note: str | None = Field(
        None,
        description=(
            "Set when the orders were read another way than asked: on a read-only gateway "
            "API, this server's own orders come from every client's list, filtered by "
            "client id."
        ),
    )
    as_of: datetime = Field(description=_AS_OF)


class CompletedOrder(BaseModel):
    """A filled or cancelled order from recent sessions."""

    account: str
    perm_id: int | None = Field(None, description="Permanent order id (unique across clients).")
    parent_perm_id: int | None = None
    contract: ContractOut
    action: str = Field(description="BUY or SELL.")
    quantity: float = Field(description="Total order quantity.")
    filled: float | None = Field(None, description="Quantity filled.")
    order_type: str
    limit_price: float | None = None
    aux_price: float | None = None
    tif: str | None = None
    order_ref: str | None = None
    status: str = Field(description="Final state: Filled, Cancelled, Inactive, ...")
    completed_status: str | None = Field(
        None, description="IBKR's completion text, e.g. why it was cancelled."
    )
    completed_time: str | None = Field(
        None, description="Completion time exactly as IBKR reports it."
    )
    completed_at: datetime | None = Field(
        None, description="completed_time as UTC, when it could be parsed."
    )


class CompletedOrderList(Truncatable):
    """Completed orders in one account, newest first."""

    account: str
    orders: list[CompletedOrder]
    total: int = Field(description="How many orders matched before the limit.")
