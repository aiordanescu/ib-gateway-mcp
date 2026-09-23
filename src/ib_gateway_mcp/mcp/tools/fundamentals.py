"""Fundamentals tools: fundamental reports and Wall Street Horizon (WSH) events.

Tools here follow :mod:`ib_gateway_mcp.mcp.tools.ops` and use the parameter types in
:mod:`ib_gateway_mcp.mcp.params`.
"""

from datetime import date
from typing import Annotated, Any

from pydantic import Field

from ib_gateway_mcp.mcp.context import ToolContext, gateway_from
from ib_gateway_mcp.mcp.params import ContractArg, LimitArg
from ib_gateway_mcp.mcp.registry import Tier, ib_tool
from ib_gateway_mcp.models.common import ContractSpec
from ib_gateway_mcp.models.fundamentals import (
    FundamentalReport,
    FundamentalReportType,
    WshEventList,
    WshMetadata,
)
from ib_gateway_mcp.services.fundamentals import (
    DEFAULT_REPORT_CHARS,
    DEFAULT_WSH_METADATA_CHARS,
    MAX_REPORT_CHARS,
    MAX_WSH_METADATA_CHARS,
)


@ib_tool("fundamentals", Tier.READ, "Fundamental report")
async def get_fundamental_data(
    ctx: ToolContext,
    contract: ContractArg,
    report_type: Annotated[
        FundamentalReportType,
        Field(
            description=(
                "ReportSnapshot: company overview, key ratios, forecast summary. "
                "ReportsFinSummary: per-period EPS, revenue, dividends. ReportsFinStatements: "
                "income statement, balance sheet, cash flow (large). ReportsOwnership: "
                "holders (large). RESC: analyst estimates. CalendarReport: company calendar "
                "(often unavailable)."
            )
        ),
    ],
    max_chars: Annotated[
        int,
        Field(
            ge=1000,
            le=MAX_REPORT_CHARS,
            description=(
                f"Longest XML report to return, in characters (at most {MAX_REPORT_CHARS:,}); "
                "a longer report is cut and truncated is true."
            ),
        ),
    ] = DEFAULT_REPORT_CHARS,
) -> FundamentalReport:
    """Fetch a Refinitiv fundamentals report for a stock, as XML.

    Deprecated by IBKR: reqFundamentalData was removed in TWS API 10.50. It still works on
    IB Gateway stable (10.45); newer gateways may refuse it. Stocks only (sec_type STK).
    Needs the Refinitiv (Reuters) fundamentals data subscription on the IBKR login.
    Whitespace between XML tags is removed; long reports are cut at max_chars (default
    50000, max 200000) with truncated=true, so prefer ReportSnapshot or ReportsFinSummary
    over the large statement and ownership reports.
    Errors: not_found (unknown stock, or IBKR has no such report for it, error
    430), ambiguous_contract (give primary_exchange or con_id), ib_api_error 10358 (no
    fundamentals subscription), invalid_request (not a stock), request_timeout (a newer
    gateway may not answer at all). For key ratios without this report, use
    subscribe_quotes with the fundamental_ratios generic tick.
    """
    return await gateway_from(ctx).fundamentals.fundamental_data(
        contract, report_type, max_chars=max_chars
    )


@ib_tool("fundamentals", Tier.READ, "WSH metadata")
async def get_wsh_metadata(
    ctx: ToolContext,
    query: Annotated[
        str | None,
        Field(
            description=(
                "Only return the parts of the metadata mentioning this text "
                "(case-insensitive), e.g. 'earnings' or 'wshe_ed'. Omit for everything."
            )
        ),
    ] = None,
    max_chars: Annotated[
        int,
        Field(
            ge=1000,
            le=MAX_WSH_METADATA_CHARS,
            description=(
                "Longest JSON to return, in characters (at most "
                f"{MAX_WSH_METADATA_CHARS:,}); longer JSON is cut and truncated is true."
            ),
        ),
    ] = DEFAULT_WSH_METADATA_CHARS,
) -> WshMetadata:
    """Describe the Wall Street Horizon (WSH) event calendar: event types and filter fields.

    Use it to build get_wsh_events queries: `event_types` lists the event type tags
    (wshe_ed is the earnings date, for example) and `metadata_json` holds the full
    description, filtered by `query` and cut at max_chars (default 30000, max 200000).
    The metadata is fetched once and then served from this server's cache (`cached`).
    Needs a Wall Street Horizon corporate event data subscription (paid) on the IBKR
    login; without it IBKR answers with an ib_api_error. not_found means nothing matched
    `query`.
    """
    return await gateway_from(ctx).fundamentals.wsh_metadata(query=query, max_chars=max_chars)


@ib_tool("fundamentals", Tier.READ, "WSH corporate events")
async def get_wsh_events(
    ctx: ToolContext,
    *,
    contract: Annotated[
        ContractSpec | None,
        Field(
            description=(
                "The company (usually a stock). Alone it returns all its event types; add "
                "event_types to narrow them."
            )
        ),
    ] = None,
    filter_json: Annotated[
        str | dict[str, Any] | None,
        Field(
            description=(
                "A raw WSH filter: a JSON object (or its text), e.g. "
                '{"watchlist": ["8314"], "wshe_ed": "true"}. Overrides contract and '
                "event_types; dates, fill flags and limit still apply."
            )
        ),
    ] = None,
    event_types: Annotated[
        list[str] | None,
        Field(
            description=(
                "WSH event type tags from get_wsh_metadata, e.g. wshe_ed (earnings date), "
                "wshe_bod (board meeting)."
            )
        ),
    ] = None,
    start_date: Annotated[
        date | None, Field(description="First day to include (YYYY-MM-DD).")
    ] = None,
    end_date: Annotated[date | None, Field(description="Last day to include (YYYY-MM-DD).")] = None,
    fill_watchlist: Annotated[
        bool,
        Field(description="Also include the login's watchlist instruments (WSH fillWatchlist)."),
    ] = False,
    fill_portfolio: Annotated[
        bool,
        Field(
            description=(
                "Also include the instruments held in the login's portfolio (WSH "
                "fillPortfolio). Refused when the login has accounts outside this "
                "server's allowlist."
            )
        ),
    ] = False,
    fill_competitors: Annotated[
        bool,
        Field(
            description=(
                "Also include the competitors of the selected companies (WSH "
                "fillCompetitors); not enough on its own."
            )
        ),
    ] = False,
    limit: LimitArg = None,
) -> WshEventList:
    """List Wall Street Horizon corporate events: earnings dates, dividends, splits, meetings.

    Also shareholder and board meetings, conferences and more (get_wsh_metadata lists
    the event types). Give a contract, event_types, a raw filter_json, or
    fill_portfolio/fill_watchlist; narrow by start_date/end_date. Returns at most `limit`
    events (default 50, max 100); each event is the JSON object WSH sends (event type
    tag, dates, company, details), and `request` shows what was asked of IBKR.
    truncated=true means more events may exist: narrow the dates to page through them.
    The WSH metadata is requested automatically first, as IBKR requires. Needs a Wall
    Street Horizon corporate event data subscription (paid) on the IBKR login; without
    it IBKR answers with an ib_api_error.

    Errors: not_found (no events match: widen the dates or check event_types),
    invalid_request (unknown event type, bad filter_json, dates in the wrong order,
    nothing to ask for), account_not_allowed (fill_portfolio on a login with accounts
    outside the allowlist).
    """
    return await gateway_from(ctx).fundamentals.wsh_events(
        contract=contract,
        filter_json=filter_json,
        event_types=event_types,
        start_date=start_date,
        end_date=end_date,
        fill_watchlist=fill_watchlist,
        fill_portfolio=fill_portfolio,
        fill_competitors=fill_competitors,
        limit=limit,
    )
