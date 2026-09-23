"""Historical data tools: bars, ticks, head timestamp, histogram, trading schedule.

Tools here follow :mod:`ib_gateway_mcp.mcp.tools.ops` and use the parameter types in
:mod:`ib_gateway_mcp.mcp.params`.
"""

from datetime import datetime
from typing import Annotated

from pydantic import Field

from ib_gateway_mcp.mcp.context import ToolContext, gateway_from
from ib_gateway_mcp.mcp.params import BarSizeArg, ContractArg, LimitArg, UseRthArg, WhatToShowArg
from ib_gateway_mcp.mcp.registry import Tier, ib_tool
from ib_gateway_mcp.models.history import (
    BarList,
    HeadTimestamp,
    Histogram,
    HistoricalTickList,
    HistoricalTickType,
    TradingSchedule,
)
from ib_gateway_mcp.services.history import SCHEDULE_DAYS_MAX, TICKS_MAX


@ib_tool("history", Tier.READ, "Historical bars")
async def get_historical_bars(
    ctx: ToolContext,
    contract: ContractArg,
    *,
    bar_size: BarSizeArg = "1 hour",
    duration: Annotated[
        str,
        Field(
            description=(
                "How far back from end: '<n> S|D|W|M|Y', e.g. '1800 S', '5 D', '2 W', "
                "'6 M', '1 Y' (M means months). Words like '30 mins' also work."
            )
        ),
    ] = "1 D",
    end: Annotated[
        datetime | None,
        Field(description="End of the range, ISO 8601 (no offset means UTC). Omit for now."),
    ] = None,
    what_to_show: WhatToShowArg = "TRADES",
    use_rth: UseRthArg = True,
    limit: LimitArg = None,
) -> BarList:
    """Return historical OHLCV bars for one instrument, oldest first.

    Bars cover `duration` back from `end` (now when omitted). Intraday bar times are UTC;
    daily, weekly and monthly bars carry the trading date. Default limit 1000 bars, at
    most 10000; when there are more, the NEWEST are kept and `truncated` is true (request
    a shorter duration or larger bars to see older ones).

    Limits (IBKR): bars of 30 seconds or less reach back about 6 months, allow short
    durations only (1 secs up to 1800 S, 5 secs up to 3600 S, 10/15 secs up to 14400 S,
    30 secs up to 28800 S), and are paced at about 60 requests per 10 minutes, 5 per
    contract and data type in 2 seconds, and no identical request within 15 s (this
    server answers identical requests from a 15-second cache). Larger bars are not paced
    that way; IBKR's guide for the longest duration: 1 min bars about 1 D, 3 mins 1 W,
    30 mins 1 M, daily bars years. Needs market-data permissions for the instrument (the
    same subscription as live quotes). IBKR keeps no data for expired options; expired
    futures need include_expired in the contract.

    Errors: not_found (no such contract, or no data in the range: check
    what_to_show, use_rth and get_head_timestamp), invalid_request (bad duration or
    bar size combination), rate_limit (pacing; retry after the stated time),
    ib_api_error 162 (pacing, permissions), request_timeout (shorten the request).
    """
    return await gateway_from(ctx).history.historical_bars(
        contract,
        bar_size=bar_size,
        duration=duration,
        end=end,
        what_to_show=what_to_show,
        use_rth=use_rth,
        limit=limit,
    )


@ib_tool("history", Tier.READ, "Historical ticks")
async def get_historical_ticks(
    ctx: ToolContext,
    contract: ContractArg,
    *,
    start: Annotated[
        datetime | None,
        Field(
            description=(
                "Return ticks from this time on (ISO 8601; no offset means UTC). Give "
                "exactly one of start and end."
            )
        ),
    ] = None,
    end: Annotated[
        datetime | None,
        Field(description="Return the ticks up to this time (ISO 8601; no offset means UTC)."),
    ] = None,
    count: Annotated[
        int,
        Field(ge=1, le=TICKS_MAX, description="How many ticks, 1-1000 (IBKR's maximum)."),
    ] = TICKS_MAX,
    what_to_show: Annotated[
        HistoricalTickType,
        Field(
            description=(
                "TRADES (price, size, exchange, conditions), BID_ASK (bid/ask and sizes; "
                "counts double for pacing) or MIDPOINT."
            )
        ),
    ] = "TRADES",
    use_rth: UseRthArg = True,
    ignore_size: Annotated[
        bool, Field(description="BID_ASK only: skip ticks where only the sizes changed.")
    ] = False,
) -> HistoricalTickList:
    """Return individual historical trades, quote changes or midpoints (time and sales).

    Give exactly one of `start` (ticks after it) or `end` (ticks before it). IBKR sends
    at most 1000 ticks per request, with one-second timestamps, and may add a few to
    finish the last second. When `truncated` is true there are probably more: page on
    with start set to the last tick's time (or end set to the first tick's time); ticks
    in that same second can repeat.

    Needs market-data permissions for the instrument. Paced like small bars (about 60
    requests per 10 minutes; BID_ASK counts double), so prefer get_historical_bars for
    anything longer than minutes of activity.

    Errors: not_found (no ticks in the range), invalid_request (both or neither
    of start/end), rate_limit, ib_api_error 162 (pacing, permissions).
    """
    return await gateway_from(ctx).history.historical_ticks(
        contract,
        start=start,
        end=end,
        count=count,
        what_to_show=what_to_show,
        use_rth=use_rth,
        ignore_size=ignore_size,
    )


@ib_tool("history", Tier.READ, "Earliest historical data")
async def get_head_timestamp(
    ctx: ToolContext,
    contract: ContractArg,
    *,
    what_to_show: WhatToShowArg = "TRADES",
    use_rth: UseRthArg = True,
) -> HeadTimestamp:
    """Return the earliest date and time IBKR has historical data for an instrument.

    Use it before long get_historical_bars requests, or when they come back empty, to
    learn how far back the data goes for this `what_to_show`. Needs market-data
    permissions for the instrument; counts toward IBKR's historical-data limits.

    Errors: not_found (no such contract, or no data of that type).
    """
    return await gateway_from(ctx).history.head_timestamp(
        contract, what_to_show=what_to_show, use_rth=use_rth
    )


@ib_tool("history", Tier.READ, "Price histogram")
async def get_histogram(
    ctx: ToolContext,
    contract: ContractArg,
    *,
    period: Annotated[
        str,
        Field(
            description=(
                "Look-back period: '<n> days|weeks|months|years', e.g. '3 days', '1 week', "
                "'1 month'."
            )
        ),
    ] = "1 week",
    use_rth: UseRthArg = True,
    limit: LimitArg = None,
) -> Histogram:
    """Return how trading volume was distributed over price levels during a period.

    Each entry is a price and IBKR's count (traded volume) at that price, sorted by
    price. Useful for volume-at-price, support/resistance and value-area questions.
    Default limit 200 price levels, at most 1000; when there are more, the busiest levels
    are kept and `truncated` is true. Needs market-data permissions for the instrument.

    Errors: not_found (no data for the period), invalid_request (bad period).
    """
    return await gateway_from(ctx).history.histogram(
        contract, period=period, use_rth=use_rth, limit=limit
    )


@ib_tool("history", Tier.READ, "Trading schedule")
async def get_trading_schedule(
    ctx: ToolContext,
    contract: ContractArg,
    *,
    num_days: Annotated[
        int,
        Field(
            ge=1,
            le=SCHEDULE_DAYS_MAX,
            description="How many days of sessions, ending at end (1-30).",
        ),
    ] = 5,
    end: Annotated[
        datetime | None,
        Field(description="Last day to cover, ISO 8601 (no offset means UTC). Omit for now."),
    ] = None,
    use_rth: UseRthArg = True,
) -> TradingSchedule:
    """Return an instrument's trading sessions (start, end, trading date) for recent days.

    Times are in the exchange's time zone with the offset included, so holidays, early
    closes and overnight sessions show up as they really were. For the upcoming
    sessions, get_contract_details also lists trading and liquid hours. Counts toward
    IBKR's historical-data limits.

    Errors: not_found (no such contract, or no sessions in the range).
    """
    return await gateway_from(ctx).history.trading_schedule(
        contract, num_days=num_days, end=end, use_rth=use_rth
    )
