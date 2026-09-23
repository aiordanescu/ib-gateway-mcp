"""Account tools: summary, values, positions, portfolio, P&L, executions and orders on record.

Tools here follow :mod:`ib_gateway_mcp.mcp.tools.ops` and use the parameter types in
:mod:`ib_gateway_mcp.mcp.params`. Each one calls a single
:class:`~ib_gateway_mcp.services.account.AccountService` method.
"""

from datetime import datetime
from typing import Annotated

from pydantic import Field

from ib_gateway_mcp.mcp.context import ToolContext, gateway_from
from ib_gateway_mcp.mcp.params import AccountArg, ContractArg, LimitArg
from ib_gateway_mcp.mcp.registry import Tier, ib_tool
from ib_gateway_mcp.models.account import (
    AccountPnl,
    AccountSummary,
    AccountValueList,
    CompletedOrderList,
    ExecutionList,
    OpenOrderList,
    Portfolio,
    PositionList,
    PositionPnl,
)
from ib_gateway_mcp.models.common import Action, SecType

ModelCodeArg = Annotated[
    str | None,
    Field(
        description=(
            "Financial-advisor model code, to scope the result to one model portfolio. "
            "Omit for the whole account (normal for non-advisor accounts)."
        )
    ),
]


@ib_tool("account", Tier.READ, "Account summary")
async def get_account_summary(
    ctx: ToolContext,
    account: AccountArg = None,
    tags: Annotated[
        list[str] | None,
        Field(
            description=(
                "Only return these tags in `values`, e.g. ['NetLiquidation', 'BuyingPower']. "
                "Omit for every row. The headline fields are always filled."
            )
        ),
    ] = None,
) -> AccountSummary:
    """Return an account's headline balances: net liquidation, cash, buying power, margin.

    Headline fields (in `base_currency`): net_liquidation, total_cash_value, settled_cash,
    buying_power, available_funds, excess_liquidity, equity_with_loan_value,
    gross_position_value, init/maint margin requirement, sma, cushion (fraction), leverage
    and day_trades_remaining (-1 = unlimited). `values` holds every summary row,
    including per-currency ledger rows (CashBalance, UnrealizedPnL... with currency BASE
    for converted totals). IBKR refreshes the summary about every 3 minutes, and right
    after trades. No market-data subscription needed.
    Errors: invalid_request lists the valid tags when a tag is unknown.
    """
    return await gateway_from(ctx).account.account_summary(account, tags=tags)


@ib_tool("account", Tier.READ, "Account values")
async def get_account_values(
    ctx: ToolContext,
    *,
    account: AccountArg = None,
    model_code: ModelCodeArg = None,
    tags: Annotated[
        list[str] | None,
        Field(description="Only these tags (case-insensitive), e.g. ['CashBalance']."),
    ] = None,
    currency: Annotated[
        str | None,
        Field(description="Only values in this currency, e.g. USD; BASE for converted totals."),
    ] = None,
    limit: LimitArg = None,
) -> AccountValueList:
    """Return the full key/value account data: every tag IBKR reports, per currency.

    Use it for details the summary lacks (per-currency cash, accrued interest, segment
    values with -C/-S suffixes, currency exchange rates...). Each row has the raw `value`
    and, when numeric, `amount`. Values update about every 3 minutes or on change.
    With `model_code` (financial advisors) the values of that model are fetched once.
    Default limit 200, maximum 1000; `truncated` says whether more matched.
    Errors: invalid_request lists the valid tags when a tag is unknown.
    """
    return await gateway_from(ctx).account.account_values(
        account, model_code=model_code, tags=tags, currency=currency, limit=limit
    )


@ib_tool("account", Tier.READ, "Positions")
async def get_positions(
    ctx: ToolContext, account: AccountArg = None, model_code: ModelCodeArg = None
) -> PositionList:
    """List an account's positions: contract, quantity (negative = short) and average cost.

    Fast and always current (IBKR streams position changes). avg_cost includes
    commissions and, for options and futures, the multiplier. For market value and
    unrealized P&L use get_portfolio; for today's P&L of one position use
    get_position_pnl. `contract.exchange` is not reported for positions; use the con_id
    for follow-up calls. `model_code` (financial advisors) lists one model's positions.
    """
    return await gateway_from(ctx).account.positions(account, model_code=model_code)


@ib_tool("account", Tier.READ, "Portfolio")
async def get_portfolio(ctx: ToolContext, account: AccountArg = None) -> Portfolio:
    """List an account's positions with market price, market value and unrealized/realized P&L.

    Values are in each position's currency and use IBKR's own valuation (no market-data
    subscription needed); IBKR refreshes them about every 3 minutes. For an account other
    than the default the call takes a moment longer, because IBKR streams portfolio data
    for one account at a time.
    """
    return await gateway_from(ctx).account.portfolio(account)


@ib_tool("account", Tier.READ, "Account P&L")
async def get_pnl(
    ctx: ToolContext, account: AccountArg = None, model_code: ModelCodeArg = None
) -> AccountPnl:
    """Return the account's live P&L: today's (daily), unrealized and realized, in base currency.

    One-shot: subscribes to IBKR's P&L feed, waits a few seconds at most for the first
    update (usually about a second), and cancels, so every call is a fresh reading and
    nothing needs unsubscribing. Errors: request_timeout when IBKR sends nothing (right
    after login, or an account without data); retry once before giving up.
    """
    return await gateway_from(ctx).account.pnl(account, model_code=model_code)


@ib_tool("account", Tier.READ, "Position P&L")
async def get_position_pnl(
    ctx: ToolContext,
    contract: ContractArg,
    account: AccountArg = None,
    model_code: ModelCodeArg = None,
) -> PositionPnl:
    """Return the live P&L of one position: daily, unrealized, realized and market value.

    Pass the position's con_id (from get_positions) for an exact match. Waits a few
    seconds at most for IBKR's first update. Errors: not_found when the account has no
    position and no P&L today in that contract; ambiguous_contract lists candidates;
    invalid_request for a combo (BAG: ask per leg); request_timeout when IBKR sends nothing.
    """
    return await gateway_from(ctx).account.position_pnl(contract, account, model_code=model_code)


@ib_tool("account", Tier.READ, "Executions")
async def get_executions(
    ctx: ToolContext,
    *,
    account: AccountArg = None,
    symbol: Annotated[str | None, Field(description="Only this symbol, e.g. AAPL.")] = None,
    sec_type: Annotated[
        SecType | None, Field(description="Only this security type, e.g. STK or OPT.")
    ] = None,
    side: Annotated[Action | None, Field(description="Only buys (BUY) or sells (SELL).")] = None,
    since: Annotated[
        datetime | None,
        Field(description="Only executions at or after this time (ISO 8601; no zone = UTC)."),
    ] = None,
    limit: LimitArg = None,
) -> ExecutionList:
    """List the account's executions (fills), newest first, with commission and realized P&L.

    Covers the current trading day only (up to 7 days if the gateway's trade-log setting
    allows); older trades are not available through the API. `commission` is null until
    IBKR reports it (usually within seconds of the fill); realized_pnl is 0 for fills
    that opened a position. Default limit 100, maximum 1000.
    """
    return await gateway_from(ctx).account.executions(
        account, symbol=symbol, sec_type=sec_type, side=side, since=since, limit=limit
    )


@ib_tool("account", Tier.READ, "Open orders")
async def get_open_orders(
    ctx: ToolContext,
    account: AccountArg = None,
    include_other_clients: Annotated[
        bool,
        Field(
            description=(
                "Also list orders placed by other API clients and manually in TWS "
                "(default). False lists only the orders this server placed."
            )
        ),
    ] = True,
) -> OpenOrderList:
    """List the account's working orders: status, filled/remaining quantity and prices.

    `modifiable` is true only for orders this server placed (same API client id): only
    those can be modified or cancelled with the order tools. Orders of other API clients
    and manual TWS orders are shown for information. Filled and cancelled orders are
    left out; see get_completed_orders. When the gateway's API is read-only (get_health:
    api_read_only), IBKR refuses the request for this server's orders alone, so
    include_other_clients=false reads every client's orders and keeps this server's;
    `note` says so. Errors: ib_api_error 321, at once, if IBKR refuses open orders
    altogether on a read-only API.
    """
    return await gateway_from(ctx).account.open_orders(
        account, include_other_clients=include_other_clients
    )


@ib_tool("account", Tier.READ, "Completed orders")
async def get_completed_orders(
    ctx: ToolContext,
    account: AccountArg = None,
    api_only: Annotated[
        bool, Field(description="Only orders placed through the API (leave out manual TWS ones).")
    ] = False,
    limit: LimitArg = None,
) -> CompletedOrderList:
    """List recently filled or cancelled orders, newest first, with IBKR's completion status.

    IBKR decides how far back this reaches (the current and recent sessions). Use
    get_executions for fill prices and commissions. Default limit 100, maximum 1000.
    Errors: ib_api_error 321, at once, when the gateway's API is read-only (get_health:
    api_read_only), which refuses this request; get_executions still lists recent fills.
    """
    return await gateway_from(ctx).account.completed_orders(account, api_only=api_only, limit=limit)
