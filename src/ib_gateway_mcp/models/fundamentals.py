"""Models for fundamental reports and Wall Street Horizon (WSH) calendar events."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ib_gateway_mcp.models.common import ContractOut, Truncatable

__all__ = [
    "FundamentalReport",
    "FundamentalReportType",
    "WshEventList",
    "WshEventQuery",
    "WshMetadata",
]

FundamentalReportType = Literal[
    "ReportSnapshot",
    "ReportsFinSummary",
    "ReportsFinStatements",
    "ReportsOwnership",
    "RESC",
    "CalendarReport",
]
"""Fundamental (Refinitiv) report types ``reqFundamentalData`` accepts.

ReportSnapshot: company overview, key ratios and a forecast summary. ReportsFinSummary:
per-period EPS, revenue and dividends. ReportsFinStatements: income statement, balance
sheet and cash flow (large). ReportsOwnership: institutional and insider holders (large).
RESC: analyst estimates. CalendarReport: company calendar (often not available).
"""


class FundamentalReport(Truncatable):
    """One fundamental report as XML, cut at ``max_chars`` when longer."""

    contract: ContractOut = Field(description="The stock the report is about.")
    report_type: FundamentalReportType = Field(description="The report type that was requested.")
    xml: str = Field(
        description=(
            "The report as XML (whitespace between tags removed). Not well-formed when "
            "truncated is true."
        )
    )
    total_chars: int = Field(description="Length of the whole report before truncation.")


class WshMetadata(Truncatable):
    """Wall Street Horizon metadata: the event types and filters WSH queries can use."""

    query: str | None = Field(None, description="The filter applied to the metadata, if any.")
    event_types: list[str] = Field(
        default_factory=list,
        description=(
            "Event type tags (wshe_...) found in the returned metadata; pass them as "
            "event_types to get_wsh_events."
        ),
    )
    metadata_json: str = Field(
        description=(
            "The metadata as compact JSON (only the parts mentioning query, when given). "
            "Not valid JSON when truncated is true."
        )
    )
    total_chars: int = Field(description="Length of the full (filtered) JSON before truncation.")
    cached: bool = Field(
        description="True when served from this server's cache instead of a new request."
    )
    fetched_at: datetime = Field(description="When the metadata was fetched from IBKR (UTC).")


class WshEventQuery(BaseModel):
    """The Wall Street Horizon event request that was sent to IBKR."""

    con_id: int | None = Field(None, description="Contract id the request named, if any.")
    filter: str | None = Field(None, description="The WSH filter JSON that was sent, if any.")
    start_date: date | None = Field(None, description="First day asked for, if any.")
    end_date: date | None = Field(None, description="Last day asked for, if any.")
    fill_watchlist: bool = Field(False, description="Whether the watchlist was added.")
    fill_portfolio: bool = Field(False, description="Whether the portfolio was added.")
    fill_competitors: bool = Field(False, description="Whether competitors were added.")
    total_limit: int = Field(
        description=(
            "Maximum number of events asked of IBKR; an answer that reaches it may be "
            "incomplete (truncated is then true)."
        )
    )


class WshEventList(Truncatable):
    """Corporate events from Wall Street Horizon, in the order IBKR returned them."""

    contract: ContractOut | None = Field(None, description="The instrument asked about, if any.")
    events: list[dict[str, Any]] = Field(
        description=(
            "One object per event, as WSH describes it (event type tag, dates, company "
            "and event-specific data). Missing numbers are null."
        )
    )
    total: int = Field(description="How many events IBKR returned, before the limit.")
    request: WshEventQuery = Field(description="What was asked of IBKR.")
    notes: list[str] = Field(
        default_factory=list, description="Remarks about how the arguments were used."
    )
