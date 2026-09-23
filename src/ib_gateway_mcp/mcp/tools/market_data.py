"""Market data tools: quote snapshots, market data type, and streaming subscriptions.

Tools here follow :mod:`ib_gateway_mcp.mcp.tools.ops` and use the parameter types in
:mod:`ib_gateway_mcp.mcp.params`. The subscription tools (``list_subscriptions``,
``get_subscription_data``, ``unsubscribe``) serve every kind of stream, including the
ones other toolsets open (scanner, news, display groups).
"""

from datetime import datetime
from typing import Annotated

from pydantic import Field

from ib_gateway_mcp.mcp.context import ToolContext, gateway_from
from ib_gateway_mcp.mcp.params import (
    SUBSCRIPTION_ID_HELP,
    ContractArg,
    LimitArg,
    LiveBarSizeArg,
    SubscriptionIdArg,
    UseRthArg,
    WhatToShowArg,
)
from ib_gateway_mcp.mcp.registry import Tier, ib_tool
from ib_gateway_mcp.models.common import (
    ContractSpec,
    MarketDataTypeName,
    RealtimeWhatToShow,
    SubscriptionDataOut,
    SubscriptionOut,
)
from ib_gateway_mcp.models.market_data import (
    GenericTick,
    MarketDataTypeOut,
    QuoteList,
    SubscriptionList,
    TickByTickType,
    UnsubscribeResult,
)
from ib_gateway_mcp.services.market_data import (
    DEPTH_ROWS_MAX,
    MAX_QUOTE_CONTRACTS,
    REALTIME_BUFFER_MAX,
    TICK_BUFFER_MAX,
)


@ib_tool("market_data", Tier.READ, "Quote snapshots")
async def get_quotes(
    ctx: ToolContext,
    contracts: Annotated[
        list[ContractSpec],
        Field(
            min_length=1,
            max_length=MAX_QUOTE_CONTRACTS,
            description=(
                f"1 to {MAX_QUOTE_CONTRACTS} instruments. A con_id alone is unambiguous; "
                "otherwise symbol and sec_type, plus expiry, strike and right for options."
            ),
        ),
    ],
    regulatory_snapshot: Annotated[
        bool,
        Field(
            description=(
                "Request a regulatory (NBBO) snapshot. COSTS MONEY: IBKR bills about USD "
                "0.01 per request for US stocks and ETFs without a live subscription. "
                "Refused (configuration_error) unless the server's operator allowed it. "
                "Leave false unless the user asked for it."
            )
        ),
    ] = False,
) -> QuoteList:
    """Get a one-time quote for up to 25 contracts: bid, ask, last, sizes, OHLC, volume.

    Each quote also has `halted`, `market_data_type` (live, frozen, delayed or
    delayed_frozen), `bbo_exchange` (expand it with get_smart_components) and, for
    options, IBKR model greeks and implied volatility. Snapshots take up to about 11
    seconds for quiet contracts. A contract with an open quote stream (subscribe_quotes)
    is answered from the stream at no extra cost. Generic ticks (shortable shares,
    fundamental ratios...) are not available as snapshots; use subscribe_quotes.

    Contracts that fail are listed in `errors` (unknown or ambiguous contract, no market
    data permission) while the others still get quotes; if all fail, the call fails.
    Needs market data permissions for each exchange. Without them IBKR answers with
    error 354, 10089 or 10168: call set_market_data_type with 'delayed' for free
    15-20 minute delayed data (10089 with delayed already selected: IBKR has no delayed
    data for that instrument on this login). Null prices mean IBKR sent no value.
    """
    return await gateway_from(ctx).market_data.quotes(
        contracts, regulatory_snapshot=regulatory_snapshot
    )


@ib_tool("market_data", Tier.READ, "Set market data type", idempotent=True, read_only=False)
async def set_market_data_type(
    ctx: ToolContext,
    data_type: Annotated[
        MarketDataTypeName,
        Field(
            description=(
                "live (real-time, needs a subscription), frozen (last live values after "
                "the close), delayed (15-20 minutes old, free for most exchanges) or "
                "delayed_frozen (last delayed values)."
            )
        ),
    ],
) -> MarketDataTypeOut:
    """Switch the whole gateway connection between live, frozen, delayed and delayed-frozen data.

    Applies to every market data request made afterwards (get_quotes and new streams),
    for every tool, and is kept across reconnects. Use 'delayed' when quotes fail with
    error 354, 10089 or 10168 (no live market data subscription). With live selected,
    IBKR still falls back to delayed data where it can (each quote says which it got).
    Delayed data has no market depth and no tick-by-tick data. Open streams keep what
    they had until you unsubscribe and subscribe again.
    """
    return gateway_from(ctx).market_data.set_market_data_type(data_type)


@ib_tool("market_data", Tier.READ, "Stream quotes")
async def subscribe_quotes(
    ctx: ToolContext,
    contract: ContractArg,
    generic_ticks: Annotated[
        list[GenericTick] | None,
        Field(
            description=(
                "Extra fields to stream: option_volume, option_open_interest, "
                "historical_volatility, avg_option_volume, implied_volatility (these five "
                "for stocks: the underlying's option statistics), index_future_premium, "
                "misc_stats (13/26/52-week high/low, average volume), mark_price, auction, "
                "rt_volume (time & sales, VWAP), shortable (short availability and shares), "
                "fundamental_ratios (needs a Refinitiv subscription), trade_count, "
                "trade_rate, volume_rate, rt_trade_volume, rt_historical_volatility, "
                "dividends, futures_open_interest, last_rth_trade, bond_factor_multiplier, "
                "short_term_volume (3/5/10-minute volume), ipo_prices, and for ETFs "
                "etf_nav_bid_ask, etf_nav_last, etf_nav_close, etf_nav_high_low, "
                "etf_nav_frozen_last (IBKR's intraday NAV of the fund)."
            )
        ),
    ] = None,
) -> SubscriptionOut:
    """Start streaming live top-of-book quotes for one contract (read with get_subscription_data).

    Returns a subscription handle. get_subscription_data(subscription_id) then returns
    the current quote (bid/ask/last, sizes, OHLC, volume, greeks for options), the
    requested generic tick values under `extras`, and IBKR notices. There is one quote
    stream per contract: subscribing again returns the same handle (`deduplicated`
    true), adding any new generic ticks to it. Each stream uses one of the login's
    market data lines (100 by default); streams nobody reads for `idle_ttl_s` seconds
    are cancelled, and unsubscribe frees the line at once.

    Needs market data permissions (see set_market_data_type for delayed data). If IBKR
    refuses the stream right away (354, 10089, 10168, 10197), the call fails with the
    reason; later problems show in the data as `active: false` and `error`.
    """
    return await gateway_from(ctx).market_data.subscribe_quotes(contract, generic_ticks or ())


@ib_tool("market_data", Tier.READ, "Stream market depth")
async def subscribe_market_depth(
    ctx: ToolContext,
    contract: ContractArg,
    rows: Annotated[
        int, Field(ge=1, le=DEPTH_ROWS_MAX, description="Price levels per side of the book.")
    ] = 10,
    smart_depth: Annotated[
        bool,
        Field(
            description=(
                "Aggregate the book across all exchanges (each level names its venue). "
                "False shows the book of the contract's exchange only."
            )
        ),
    ] = False,
) -> SubscriptionOut:
    """Start streaming the order book (Level II market depth) of one contract.

    get_subscription_data returns `bids` and `asks` as levels (position 0 is the best
    price) with price, size and market maker or venue. IBKR allows only 3 depth
    streams at a time by default; this server refuses a 4th (unsubscribe one first).
    One depth stream per contract: subscribing again returns the same handle with its
    original rows and smart_depth. Needs live data (not available when
    set_market_data_type chose delayed) and a Level II (depth of book) subscription
    for the exchange; get_depth_exchanges lists exchanges that offer depth. Errors:
    309 (depth limit), 10092 (no depth for this contract and exchange), 354 (no
    subscription).
    """
    return await gateway_from(ctx).market_data.subscribe_market_depth(
        contract, rows=rows, smart_depth=smart_depth
    )


@ib_tool("market_data", Tier.READ, "Stream tick-by-tick data")
async def subscribe_tick_by_tick(
    ctx: ToolContext,
    contract: ContractArg,
    tick_type: Annotated[
        TickByTickType,
        Field(
            description=(
                "Last: trades reported to the consolidated tape. AllLast: every trade "
                "including odd lots and off-exchange prints. BidAsk: every change of the "
                "best bid or ask. MidPoint: every change of the midpoint."
            )
        ),
    ],
    ignore_size: Annotated[
        bool, Field(description="BidAsk only: skip updates that change only a size.")
    ] = False,
    buffer_size: Annotated[
        int,
        Field(
            ge=1,
            le=TICK_BUFFER_MAX,
            description="How many of the newest ticks the server keeps for you to read.",
        ),
    ] = 500,
) -> SubscriptionOut:
    """Start recording every trade, quote change or midpoint of one contract into a ring buffer.

    get_subscription_data returns the buffered ticks oldest first (use its `since` and
    `limit` to page). Times are when the tick reached this server (UTC). IBKR allows
    only about 3 tick-by-tick streams at a time; this server refuses more. One stream
    per contract and tick_type: subscribing again returns the same handle. Needs live
    data and a market data subscription for the instrument (not available with delayed
    data); errors 10189 and 10190 mean IBKR refused it or its limit is reached. Stop it
    with unsubscribe.
    """
    return await gateway_from(ctx).market_data.subscribe_tick_by_tick(
        contract, tick_type, ignore_size=ignore_size, buffer_size=buffer_size
    )


@ib_tool("market_data", Tier.READ, "Stream 5-second bars")
async def subscribe_realtime_bars(
    ctx: ToolContext,
    contract: ContractArg,
    what_to_show: Annotated[
        RealtimeWhatToShow,
        Field(description="Build bars from TRADES, MIDPOINT, BID or ASK prices."),
    ] = "TRADES",
    use_rth: UseRthArg = False,
    buffer_size: Annotated[
        int,
        Field(
            ge=1,
            le=REALTIME_BUFFER_MAX,
            description="How many of the newest bars the server keeps (720 = one hour).",
        ),
    ] = 720,
) -> SubscriptionOut:
    """Start streaming 5-second OHLCV bars for one contract into a ring buffer.

    A new bar arrives every 5 seconds; get_subscription_data returns them oldest first
    with open, high, low, close, volume, VWAP and trade count. Only 5-second bars exist;
    for other sizes use subscribe_bars. One stream per contract, what_to_show and
    use_rth: subscribing again returns the same handle. Uses a market data line and
    counts against IBKR's historical data pacing. Needs market data permissions; TRADES
    is not available for forex (use MIDPOINT). Errors 420 and 162 mean IBKR refused the
    request (invalid for the contract, or pacing). Stop it with unsubscribe.
    """
    return await gateway_from(ctx).market_data.subscribe_realtime_bars(
        contract, what_to_show=what_to_show, use_rth=use_rth, buffer_size=buffer_size
    )


@ib_tool("market_data", Tier.READ, "Stream live bars")
async def subscribe_bars(
    ctx: ToolContext,
    contract: ContractArg,
    bar_size: LiveBarSizeArg,
    *,
    duration: Annotated[
        str,
        Field(
            description=(
                "How much history to load first: a number and a unit, S (seconds), D, W, M "
                "or Y, e.g. '3600 S', '1 D', '2 W'."
            )
        ),
    ] = "1 D",
    what_to_show: WhatToShowArg = "TRADES",
    use_rth: UseRthArg = True,
) -> SubscriptionOut:
    """Load recent bars of any size and keep the newest bar updating live.

    First loads `duration` of history (like get_historical_bars), then IBKR updates the
    last bar and appends new ones as time passes. get_subscription_data returns the
    bars oldest first (the newest 100 by default; the server keeps up to 5000). One
    stream per contract and parameter set: subscribing again returns the same handle.
    Counts against IBKR's historical data pacing (about 60 requests per 10 minutes;
    error 162 on a violation) and needs market data permissions. Stop it with
    unsubscribe.

    Errors: not_found if IBKR has no bars for the period (try a longer duration or
    use_rth=false), ib_api_error 321 if IBKR rejects the combination of bar size,
    duration and what_to_show (the message names the field).
    """
    return await gateway_from(ctx).market_data.subscribe_bars(
        contract, bar_size, duration=duration, what_to_show=what_to_show, use_rth=use_rth
    )


@ib_tool("market_data", Tier.READ, "List subscriptions")
async def list_subscriptions(ctx: ToolContext) -> SubscriptionList:
    """List every open subscription of every kind: quotes, depth, ticks, bars, scans, news...

    For each: its id, kind, key, contract, parameters, when it was created and last
    read, when it will be cancelled for being idle (`idle_expires_at`), and `stale`
    (true while the gateway connection is down). Also shows capacity: subscriptions
    used out of the server maximum, and market depth and tick-by-tick streams used out
    of IBKR's limits, plus the connection's current market data type.
    """
    return gateway_from(ctx).market_data.list_subscriptions()


@ib_tool("market_data", Tier.READ, "Read subscription")
async def get_subscription_data(
    ctx: ToolContext,
    subscription_id: SubscriptionIdArg,
    limit: LimitArg = None,
    since: Annotated[
        datetime | None,
        Field(
            description=(
                "Only items (ticks, bars, headlines, bulletins, display group updates) "
                "after this time (ISO 8601, UTC if no zone). Pass the time of the last item "
                "you read to get only new ones."
            )
        ),
    ] = None,
) -> SubscriptionDataOut:
    """Read the latest state of any subscription: quote, order book, ticks, bars, rows, news...

    `data` depends on `kind`: quotes → `quote` (+ `extras`); depth → `bids`/`asks`;
    tick_by_tick → `ticks`; realtime_bars and bars → `bars`; scanner → `rows`; news →
    `headlines`; news_bulletins → `bulletins`; display_group → `current` and `updates`.
    Time series (ticks, bars, headlines, bulletins, display group updates) come oldest
    first, cut to the newest `limit` (default 100, max 5000) after `since`;
    `data.truncated` is true when older ones were left out.
    Stream snapshots also carry `active`, `error` and `notices` (IBKR messages such as
    delayed data or a lost subscription). `stale` true means the gateway connection
    dropped and values may be old. Each read keeps the subscription alive; one not read
    for its idle time is cancelled and this tool then reports subscription_not_found.
    """
    return gateway_from(ctx).market_data.subscription_data(
        subscription_id, limit=limit, since=since
    )


@ib_tool("market_data", Tier.READ, "Unsubscribe", idempotent=True, read_only=False)
async def unsubscribe(
    ctx: ToolContext,
    subscription_id: Annotated[str | None, Field(description=SUBSCRIPTION_ID_HELP)] = None,
    all: Annotated[bool, Field(description="Cancel every subscription of every kind.")] = False,
) -> UnsubscribeResult:
    """Cancel one subscription, or all of them, and free their IBKR market data lines.

    Pass either `subscription_id` or `all=true`. Works for every kind of subscription
    (quotes, depth, tick-by-tick, bars, scanners, news, bulletins, display groups).
    Subscriptions belong to the server, not to one client: `all=true` also stops streams
    that other clients of this server opened.
    """
    return await gateway_from(ctx).market_data.unsubscribe(subscription_id, all_subscriptions=all)
