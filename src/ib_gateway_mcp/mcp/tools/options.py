"""Option tools: IBKR's option calculators and snapshot quotes for a slice of a chain.

Tools here follow :mod:`ib_gateway_mcp.mcp.tools.ops` and use the parameter types in
:mod:`ib_gateway_mcp.mcp.params`. The calculators run IBKR's option model; the quotes
need market data subscriptions (OPRA for US equity and index options).
"""

from typing import Annotated

from pydantic import Field

from ib_gateway_mcp.mcp.context import ToolContext, gateway_from
from ib_gateway_mcp.mcp.params import ContractArg, LimitArg
from ib_gateway_mcp.mcp.registry import Tier, ib_tool
from ib_gateway_mcp.models.common import ContractSpec
from ib_gateway_mcp.models.options import (
    ImpliedVolatilityOut,
    OptionPriceOut,
    OptionQuoteList,
    OptionRight,
)
from ib_gateway_mcp.services.options import MAX_VOLATILITY, QUOTES_LIMIT_MAX

UnderlyingPriceArg = Annotated[
    float,
    Field(gt=0, description="Underlying price to assume, e.g. the stock's current price."),
]


@ib_tool("options", Tier.READ, "Implied volatility calculator")
async def calculate_implied_volatility(
    ctx: ToolContext,
    contract: ContractArg,
    option_price: Annotated[
        float,
        Field(
            gt=0,
            description="Option price per share (not multiplied by 100), e.g. 5.20.",
        ),
    ],
    underlying_price: UnderlyingPriceArg,
) -> ImpliedVolatilityOut:
    """Compute an option's implied volatility from a given option price, with IBKR's model.

    `contract` must be one option (sec_type OPT or FOP with symbol, expiry, strike and
    right, or its con_id; futures options also need their exchange, e.g. CME). Returns
    `implied_vol` as a decimal (0.25 = 25%) and the greeks at that volatility (delta,
    gamma, vega, theta, dividend present value). Useful for what-if pricing: pass a
    hypothetical option or underlying price. Nothing is stored or streamed.

    Errors: invalid_request when the contract is not an option or no volatility fits the
    prices (e.g. an option price below intrinsic value); not_found or ambiguous_contract
    when the option cannot be resolved (get_option_chain lists expiries and strikes);
    request_timeout when IBKR does not answer within 4 seconds; ib_api_error if IBKR
    refuses (it may want market data permissions for the option and its underlying).
    """
    return await gateway_from(ctx).options.implied_volatility(
        contract, option_price=option_price, underlying_price=underlying_price
    )


@ib_tool("options", Tier.READ, "Option price calculator")
async def calculate_option_price(
    ctx: ToolContext,
    contract: ContractArg,
    volatility: Annotated[
        float,
        Field(
            gt=0,
            le=MAX_VOLATILITY,
            description="Annualized volatility as a decimal: 0.25 means 25% (not 25).",
        ),
    ],
    underlying_price: UnderlyingPriceArg,
) -> OptionPriceOut:
    """Compute an option's theoretical price and greeks at a given volatility, with IBKR's model.

    `contract` must be one option (sec_type OPT or FOP with symbol, expiry, strike and
    right, or its con_id; futures options also need their exchange, e.g. CME). Returns
    `option_price` per share (multiply by the contract multiplier for the premium) and
    the greeks (delta, gamma, vega, theta, dividend present value). Useful for
    scenarios: vary volatility or underlying_price.

    Errors: invalid_request when the contract is not an option, volatility looks like a
    percent (above 10), or IBKR computed no price; not_found or ambiguous_contract when
    the option cannot be resolved; request_timeout when IBKR does not answer within 4
    seconds; ib_api_error if IBKR refuses (e.g. missing market data permissions).
    """
    return await gateway_from(ctx).options.option_price(
        contract, volatility=volatility, underlying_price=underlying_price
    )


@ib_tool("options", Tier.READ, "Option chain quotes")
async def get_option_quotes(
    ctx: ToolContext,
    *,
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
    expiration: Annotated[
        str,
        Field(
            min_length=8,
            max_length=10,
            description="Expiration date as YYYYMMDD, e.g. 20261218 (from get_option_chain).",
        ),
    ],
    right: Annotated[
        OptionRight | None,
        Field(description="C for calls, P for puts; omit for both."),
    ] = None,
    strike_min: Annotated[
        float | None,
        Field(gt=0, description="Lowest strike to include; selects strikes by range."),
    ] = None,
    strike_max: Annotated[
        float | None,
        Field(gt=0, description="Highest strike to include; selects strikes by range."),
    ] = None,
    strikes_around_atm: Annotated[
        int | None,
        Field(
            ge=1,
            le=QUOTES_LIMIT_MAX,
            description=(
                "Number of strikes nearest the underlying's current price (default 5). "
                "Not together with strike_min/strike_max."
            ),
        ),
    ] = None,
    exchange: Annotated[
        str | None,
        Field(
            description=(
                "Exchange of the chain; default SMART, or the only exchange listed (futures "
                "options, e.g. CME)."
            )
        ),
    ] = None,
    trading_class: Annotated[
        str | None,
        Field(
            description=(
                "Trading class when several list the expiration, e.g. SPXW (PM-settled "
                "weeklies) vs SPX. Default: the class named like the underlying."
            )
        ),
    ] = None,
    limit: LimitArg = None,
) -> OptionQuoteList:
    """Snapshot quotes and greeks for a slice of one option expiration (a mini chain).

    Picks the chain for `expiration`, chooses strikes either in [strike_min, strike_max]
    or the `strikes_around_atm` strikes nearest the underlying's price (taken from a
    snapshot of the underlying), and returns for each option (both rights unless `right`
    is set): bid/ask/last with sizes, volume, close, and IBKR's model greeks (implied_vol,
    delta, gamma, vega, theta, und_price). Legs are sorted by strike, calls before puts.
    `limit` caps the legs (strike and right pairs): default 20, max 40; `total` and
    `truncated` say how many were selected (a range keeps the lowest strikes, ATM the
    nearest). Strikes the chain lists but this expiration lacks are reported in
    `skipped`, as are legs IBKR would not quote.

    Market data: one snapshot per leg, using the connection's market data type (see
    `market_data_type`; switch with set_market_data_type). Live quotes for US equity and
    index options need IBKR's OPRA subscription for API use (plus the underlying's
    exchange data; futures options need the futures exchange's data); without it, try
    delayed data. Snapshots take a few seconds and up to about 11. For expiries and
    strikes without quotes use get_option_chain; to stream one option use subscribe_quotes.

    Errors: not_found (unknown underlying, expiration or strikes not listed, or no
    underlying price: then pass strike_min/strike_max); invalid_request (bad arguments,
    or several trading classes: pass trading_class); ib_api_error when no leg could be
    quoted (the message names the subscription needed); subscription_limit when IBKR's
    market-data lines are used up.
    """
    return await gateway_from(ctx).options.option_quotes(
        underlying,
        expiration,
        right=right,
        strike_min=strike_min,
        strike_max=strike_max,
        strikes_around_atm=strikes_around_atm,
        exchange=exchange,
        trading_class=trading_class,
        limit=limit,
    )
