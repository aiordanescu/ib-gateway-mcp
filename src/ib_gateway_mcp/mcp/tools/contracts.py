"""Contract tools: symbol search, contract details, qualification, option chains, market rules.

Tools here follow :mod:`ib_gateway_mcp.mcp.tools.ops` and use the parameter types in
:mod:`ib_gateway_mcp.mcp.params`. None of them needs a market data subscription.
"""

from typing import Annotated

from pydantic import Field

from ib_gateway_mcp.mcp.context import ToolContext, gateway_from
from ib_gateway_mcp.mcp.params import ContractArg, LimitArg
from ib_gateway_mcp.mcp.registry import Tier, ib_tool
from ib_gateway_mcp.models.common import ContractOut, ContractSpec
from ib_gateway_mcp.models.contracts import (
    ContractDetailsList,
    DepthExchangeList,
    MarketRuleList,
    OptionChainList,
    SmartComponentList,
    SymbolSearchResult,
)
from ib_gateway_mcp.services.contracts import MARKET_RULES_MAX


@ib_tool("contracts", Tier.READ, "Search symbols")
async def search_symbols(
    ctx: ToolContext,
    pattern: Annotated[
        str,
        Field(
            min_length=1,
            max_length=64,
            description=(
                "The first letters of a ticker (e.g. 'AAP') or a word from the company or "
                "instrument name (e.g. 'apple')."
            ),
        ),
    ],
    limit: LimitArg = None,
) -> SymbolSearchResult:
    """Find instruments by ticker prefix or company name (IBKR's symbol search).

    Returns up to `limit` matches (default 16, IBKR rarely sends more): each with its
    con_id, symbol, sec_type, primary exchange, currency, name (`description`) and the
    derivative types listed on it (OPT, FUT, WAR...). Use it to discover a symbol or its
    con_id, then call qualify_contract or get_contract_details for the exact contract.

    Limits: discovery only, not exhaustive (no options or futures months in the results;
    use get_option_chain or get_contract_details for those). IBKR allows about one search
    per second, so back-to-back searches are spaced out. Errors: not_found when nothing
    matches; request_timeout when IBKR did not answer within 4 seconds.
    """
    return await gateway_from(ctx).contracts.search_symbols(pattern, limit=limit)


@ib_tool("contracts", Tier.READ, "Contract details")
async def get_contract_details(
    ctx: ToolContext, contract: ContractArg, limit: LimitArg = None
) -> ContractDetailsList:
    """Return IBKR's full contract details for every instrument a spec matches.

    For each contract: identifiers (con_id, local symbol, trading class, multiplier),
    long name (`contract.description`), industry and category, stock type, the
    exchange time zone, trading and liquid (regular-hours) sessions for about the next
    week with closed days, min tick, size increments, valid exchanges with their market
    rule ids (same order; see get_market_rule), accepted order types, ISIN and other
    security ids, the underlying of a derivative, and bond terms for bonds.

    The contract may be partial: symbol + sec_type FUT + exchange lists every future expiry.
    Derivatives are sorted by expiry, strike and right; `limit` defaults to 20 (cap 200),
    `total` and `truncated` say how many matched. Broad option specs are slow and
    throttled by IBKR: use get_option_chain for expiries and strikes, then look up single
    options. Futures and indexes need their listing exchange (CME, CBOE...), not SMART;
    set include_expired for expired futures. Errors: not_found for an unknown
    instrument; invalid_request for combos (BAG).
    """
    return await gateway_from(ctx).contracts.contract_details(contract, limit=limit)


@ib_tool("contracts", Tier.READ, "Qualify contract")
async def qualify_contract(ctx: ToolContext, contract: ContractArg) -> ContractOut:
    """Resolve a contract spec to exactly one IBKR contract and return it with its con_id.

    Use it to check a spec before quoting or ordering, or to turn a symbol into a con_id;
    later calls can then pass just the con_id. The returned `description` is the long name
    (e.g. APPLE INC) to confirm it is the intended instrument.

    If several contracts match (e.g. a stock listed on two exchanges, or an option spec
    without trading class), the call fails with ambiguous_contract and the message lists
    up to 20 candidates with their con_ids: retry with the right con_id or more fields
    (primary_exchange, currency, trading_class...). An unknown spec fails with not_found;
    a combo (BAG) with invalid_request (qualify each leg by con_id instead).
    """
    return await gateway_from(ctx).contracts.qualify_contract(contract)


@ib_tool("contracts", Tier.READ, "Option chain")
async def get_option_chain(
    ctx: ToolContext,
    underlying: Annotated[
        ContractSpec,
        Field(
            description=(
                "The instrument the options are on: a stock (symbol, sec_type STK), an index "
                "(sec_type IND with its exchange, e.g. SPX on CBOE) or a future (sec_type FUT "
                "with exchange and contract month), or its con_id."
            )
        ),
    ],
    exchange: Annotated[
        str | None,
        Field(
            description=(
                "Only return chains listed on this exchange, e.g. SMART or CBOE. Omit for all "
                "exchanges (identical chains are merged anyway)."
            )
        ),
    ] = None,
    fut_fop_exchange: Annotated[
        str | None,
        Field(
            description=(
                "For futures options: the exchange they trade on, e.g. CME. Omit to use the "
                "future's own exchange (for stocks and indexes: all exchanges)."
            )
        ),
    ] = None,
) -> OptionChainList:
    """List the option expirations and strikes available on an underlying (no prices).

    Returns one entry per trading class (e.g. SPX monthly and SPXW weekly), with its
    multiplier, the exchanges listing it, all expirations (YYYYMMDD) and all strikes.
    Strikes are the union across expirations: not every strike exists for every expiry,
    so qualify_contract a specific option before quoting it. This is the cheap way to
    explore options; it needs no market data subscription and has no pacing concerns.
    For US stock and index options pass exchange SMART: IBKR lists a chain per options
    exchange, and chains that differ slightly are not merged, so the full answer can be
    long. Quotes and greeks come from the options and market_data tools.

    Errors: not_found when the underlying is unknown or has no listed options (or none on
    `exchange`); invalid_request when `underlying` is itself an option or combo (also when
    given by the con_id of one).
    """
    return await gateway_from(ctx).contracts.option_chain(
        underlying, exchange=exchange, fut_fop_exchange=fut_fop_exchange
    )


@ib_tool("contracts", Tier.READ, "Market rules")
async def get_market_rule(
    ctx: ToolContext,
    market_rule_ids: Annotated[
        list[Annotated[int, Field(ge=0)]],
        Field(
            min_length=1,
            max_length=MARKET_RULES_MAX,
            description=(
                "Market rule ids from get_contract_details (market_rule_ids), e.g. [26, 239]. "
                f"At most {MARKET_RULES_MAX}."
            ),
        ),
    ],
) -> MarketRuleList:
    """Return the price increments (tick ladder) of IBKR market rules.

    A contract's valid price steps can depend on the price level and exchange: each rule
    lists rows of (low_edge, increment), meaning prices from low_edge up to the next row's
    low_edge move in steps of increment. Get the ids from get_contract_details; its
    market_rule_ids line up with valid_exchanges. Use it to round limit prices correctly.

    IBKR answers each rule within a second or not at all: ids it did not answer (probably
    unknown) are listed in `missing_ids`; if none was answered the call fails with
    not_found.
    """
    return await gateway_from(ctx).contracts.market_rules(market_rule_ids)


@ib_tool("contracts", Tier.READ, "SMART components")
async def get_smart_components(
    ctx: ToolContext,
    bbo_exchange: Annotated[
        str,
        Field(
            min_length=1,
            max_length=32,
            description="The bbo_exchange code from a quote (get_quotes), e.g. '9c0001'.",
        ),
    ],
) -> SmartComponentList:
    """Expand a SMART BBO exchange code into the exchanges behind it.

    Quotes on SMART-routed contracts carry a `bbo_exchange` code; this lists the
    exchanges it stands for, each with IBKR's single-letter code, which tells you where the
    best bid and offer come from. The code only comes from market data (get_quotes needs
    a market data subscription or delayed data). When IBKR returns no exchanges (e.g.
    outside trading hours, or for a code not taken from a quote) the call fails with
    not_found.
    """
    return await gateway_from(ctx).contracts.smart_components(bbo_exchange)


@ib_tool("contracts", Tier.READ, "Market depth exchanges")
async def get_depth_exchanges(ctx: ToolContext) -> DepthExchangeList:
    """List the exchanges that offer market depth (level 2 order book), per security type.

    Each row gives the exchange, security type, listing exchange and IBKR's depth service
    type (Deep or Deep2). Check it before subscribing to market depth. Depth itself needs
    a separate depth data subscription at IBKR; this list does not. The list is static
    and cached for the gateway session.
    """
    return await gateway_from(ctx).contracts.depth_exchanges()
