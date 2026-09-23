"""Scanner tools: scanner parameters, one-off scans and scanner subscriptions.

Tools here follow :mod:`ib_gateway_mcp.mcp.tools.ops` and use the parameter types in
:mod:`ib_gateway_mcp.mcp.params`.
"""

from typing import Annotated

from pydantic import Field

from ib_gateway_mcp.mcp.context import ToolContext, gateway_from
from ib_gateway_mcp.mcp.params import LimitArg
from ib_gateway_mcp.mcp.registry import Tier, ib_tool
from ib_gateway_mcp.models.common import SubscriptionOut
from ib_gateway_mcp.models.scanners import (
    MAX_SCANNER_ROWS,
    ScannerParameters,
    ScannerParameterSection,
    ScannerSpec,
    ScanResult,
)

SectionArg = Annotated[
    ScannerParameterSection,
    Field(
        description=(
            "What to list: scan_codes (what a scan ranks by), instruments (instrument "
            "types), locations (markets and exchanges) or filters (filter tags)."
        )
    ),
]
QueryArg = Annotated[
    str | None,
    Field(description="Case-insensitive text to look for in codes and names, e.g. gain."),
]
InstrumentFilterArg = Annotated[
    str | None,
    Field(description="Only entries that work with this instrument type, e.g. STK."),
]
_NOT_BLANK = r"\S"
"""At least one non-space character: ScannerSpec strips codes, so blanks fail there."""

ScanCodeArg = Annotated[
    str,
    Field(
        pattern=_NOT_BLANK,
        description="What to rank by, e.g. TOP_PERC_GAIN or MOST_ACTIVE.",
    ),
]
InstrumentArg = Annotated[
    str,
    Field(pattern=_NOT_BLANK, description="Instrument type, e.g. STK (US stocks) or FUT.US."),
]
LocationArg = Annotated[
    str,
    Field(
        pattern=_NOT_BLANK,
        description="Market or exchange, e.g. STK.US.MAJOR or STK.NASDAQ.",
    ),
]
AbovePriceArg = Annotated[float | None, Field(ge=0, description="Only prices above this.")]
BelowPriceArg = Annotated[float | None, Field(gt=0, description="Only prices below this.")]
AboveVolumeArg = Annotated[int | None, Field(ge=0, description="Only volume above this.")]
MarketCapAboveArg = Annotated[
    float | None, Field(ge=0, description="Only market capitalization above this.")
]
MarketCapBelowArg = Annotated[
    float | None, Field(gt=0, description="Only market capitalization below this.")
]
FiltersArg = Annotated[
    dict[str, str | int | float] | None,
    Field(
        description=(
            "More filters as tag: value, with tags from get_scanner_parameters "
            'section="filters", e.g. {"avgVolumeAbove": 100000}.'
        )
    ),
]
RowsArg = Annotated[
    int,
    Field(ge=1, le=MAX_SCANNER_ROWS, description="How many rows to return (1-50)."),
]


@ib_tool("scanners", Tier.READ, "Scanner parameters")
async def get_scanner_parameters(
    ctx: ToolContext,
    section: SectionArg,
    query: QueryArg = None,
    instrument: InstrumentFilterArg = None,
    limit: LimitArg = None,
) -> ScannerParameters:
    """Browse IBKR's market scanner catalogue to find valid inputs for run_scanner.

    Sections:
    - scan_codes: what a scan ranks by (e.g. TOP_PERC_GAIN, MOST_ACTIVE, HOT_BY_VOLUME),
      with the instrument types each supports.
    - instruments: instrument types (e.g. STK for US stocks, IND.US, FUT.US).
    - locations: markets and exchanges (e.g. STK.US.MAJOR, STK.NASDAQ), nested via parent.
    - filters: filter tags for run_scanner's `filters` (e.g. avgVolumeAbove), with
      their value type and, for choice filters, the allowed values.
    `query` matches codes and names (case-insensitive substring; for filters also the
    filter group and category); `instrument` keeps only entries for that instrument
    type. Default limit 50, at most 500; `total` says how many matched. The catalogue
    is fetched once per gateway session, so the first call can take a few seconds.
    Errors: not_found when nothing matches or the instrument type is unknown.
    """
    return await gateway_from(ctx).scanners.parameters(
        section, query=query, instrument=instrument, limit=limit
    )


@ib_tool("scanners", Tier.READ, "Run a market scan")
async def run_scanner(
    ctx: ToolContext,
    *,
    scan_code: ScanCodeArg,
    instrument: InstrumentArg = "STK",
    location_code: LocationArg = "STK.US.MAJOR",
    above_price: AbovePriceArg = None,
    below_price: BelowPriceArg = None,
    above_volume: AboveVolumeArg = None,
    market_cap_above: MarketCapAboveArg = None,
    market_cap_below: MarketCapBelowArg = None,
    filters: FiltersArg = None,
    rows: RowsArg = 25,
) -> ScanResult:
    """Run an IBKR market scan once and return the ranked instruments, best first.

    Example: top % gainers among US listed stocks above $5 is scan_code=TOP_PERC_GAIN,
    instrument=STK, location_code=STK.US.MAJOR, above_price=5. Find other scan codes,
    locations and filter tags with get_scanner_parameters. Each row has the rank
    (1 = top) and the contract (with con_id, for quotes or orders).
    Limits: at most 50 rows. IBKR allows 10 scanner subscriptions at a time; a run
    uses one for a moment and always releases it. Scans need market data permissions
    for the exchanges scanned.
    Errors: not_found when nothing matches right now (loosen the filters, or the
    market may be closed); ib_api_error with IBKR's message for an unknown scan code,
    location or filter; subscription_limit when 10 scanner subscriptions are open.
    """
    spec = ScannerSpec(
        scan_code=scan_code,
        instrument=instrument,
        location_code=location_code,
        above_price=above_price,
        below_price=below_price,
        above_volume=above_volume,
        market_cap_above=market_cap_above,
        market_cap_below=market_cap_below,
        filters=filters or {},
        rows=rows,
    )
    return await gateway_from(ctx).scanners.run_scanner(spec)


@ib_tool("scanners", Tier.READ, "Stream a market scan")
async def subscribe_scanner(
    ctx: ToolContext,
    *,
    scan_code: ScanCodeArg,
    instrument: InstrumentArg = "STK",
    location_code: LocationArg = "STK.US.MAJOR",
    above_price: AbovePriceArg = None,
    below_price: BelowPriceArg = None,
    above_volume: AboveVolumeArg = None,
    market_cap_above: MarketCapAboveArg = None,
    market_cap_below: MarketCapBelowArg = None,
    filters: FiltersArg = None,
    rows: RowsArg = 25,
) -> SubscriptionOut:
    """Keep a market scan running so its ranking follows the market; returns a handle.

    Takes the same arguments as run_scanner. Read the current rows with
    get_subscription_data(subscription_id): rows (best first), updated_at, no_matches
    (nothing qualifies right now) and error. Stop it with unsubscribe; it is also
    cancelled after the idle time in idle_ttl_s without a read. The same scan asked
    for twice returns the existing handle (deduplicated=true).
    Limits: at most 50 rows; IBKR allows 10 scanner subscriptions at a time.
    Errors: ib_api_error when IBKR rejects the scan (check the codes with
    get_scanner_parameters); subscription_limit when no slot is free.
    """
    spec = ScannerSpec(
        scan_code=scan_code,
        instrument=instrument,
        location_code=location_code,
        above_price=above_price,
        below_price=below_price,
        above_volume=above_volume,
        market_cap_above=market_cap_above,
        market_cap_below=market_cap_below,
        filters=filters or {},
        rows=rows,
    )
    return await gateway_from(ctx).scanners.subscribe_scanner(spec)
