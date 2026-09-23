"""Models for market scanner parameters, runs and subscriptions."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ib_gateway_mcp.models.common import ContractOut, Truncatable

__all__ = [
    "MAX_SCANNER_ROWS",
    "ScanCodeInfo",
    "ScanResult",
    "ScannerFilterChoice",
    "ScannerFilterInfo",
    "ScannerInstrumentInfo",
    "ScannerLocationInfo",
    "ScannerParameterSection",
    "ScannerParameters",
    "ScannerRow",
    "ScannerSnapshot",
    "ScannerSpec",
]

MAX_SCANNER_ROWS = 50
"""IBKR returns at most 50 rows per scan."""

ScannerParameterSection = Literal["scan_codes", "instruments", "locations", "filters"]
"""Parts of the scanner catalogue ``get_scanner_parameters`` can browse."""

FilterValue = str | int | float


class ScannerSpec(BaseModel):
    """What to scan for: a scan code, an instrument type, a location and optional filters.

    Codes come from ``get_scanner_parameters``. The price, volume and market-cap fields
    are IBKR's basic filters; ``filters`` takes any further filter tag from the
    catalogue's ``filters`` section (e.g. ``{"avgVolumeAbove": "100000"}``).
    """

    model_config = ConfigDict(extra="forbid")

    scan_code: str = Field(min_length=1, description="Scan code, e.g. TOP_PERC_GAIN.")
    instrument: str = Field("STK", min_length=1, description="Instrument type, e.g. STK.")
    location_code: str = Field(
        "STK.US.MAJOR", min_length=1, description="Location code, e.g. STK.US.MAJOR."
    )
    above_price: float | None = Field(None, ge=0, description="Only prices above this.")
    below_price: float | None = Field(None, gt=0, description="Only prices below this.")
    above_volume: int | None = Field(None, ge=0, description="Only volume above this.")
    market_cap_above: float | None = Field(
        None, ge=0, description="Only market capitalization above this."
    )
    market_cap_below: float | None = Field(
        None, gt=0, description="Only market capitalization below this."
    )
    filters: dict[str, FilterValue] = Field(
        default_factory=dict, description="Further filter tags and their values."
    )
    rows: int = Field(25, ge=1, le=MAX_SCANNER_ROWS, description="Rows to return (1-50).")

    @field_validator("scan_code", "instrument", "location_code", mode="before")
    @classmethod
    def _strip_codes(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


# --- scanner parameters ---------------------------------------------------------------


class ScanCodeInfo(BaseModel):
    """One scan (what the scanner ranks by)."""

    code: str = Field(description="Pass as scan_code to run_scanner or subscribe_scanner.")
    name: str | None = Field(None, description="Display name.")
    instruments: list[str] = Field(
        default_factory=list, description="Instrument types this scan supports."
    )


class ScannerInstrumentInfo(BaseModel):
    """One instrument type a scan can cover."""

    type: str = Field(description="Pass as instrument to run_scanner, e.g. STK or FUT.US.")
    name: str | None = Field(None, description="Display name, e.g. US Stocks.")
    filter_count: int = Field(0, description="How many filter groups apply to it.")


class ScannerLocationInfo(BaseModel):
    """One market or exchange a scan can run over."""

    code: str = Field(description="Pass as location_code to run_scanner, e.g. STK.US.MAJOR.")
    name: str | None = Field(None, description="Display name.")
    instruments: list[str] = Field(
        default_factory=list, description="Instrument types available at this location."
    )
    parent: str | None = Field(None, description="Code of the enclosing location, if any.")


class ScannerFilterChoice(BaseModel):
    """One allowed value of a choice filter."""

    value: str
    name: str | None = None


class ScannerFilterInfo(BaseModel):
    """One filter tag, usable as a key of run_scanner's ``filters``."""

    tag: str = Field(description="Filter tag, e.g. priceAbove or avgVolumeAbove.")
    name: str | None = Field(None, description="Display name.")
    filter_id: str = Field(description="The filter group it belongs to, e.g. PRICE.")
    category: str | None = Field(None, description="Catalogue category.")
    value_type: str | None = Field(
        None, description="Kind of value: number, integer, text, choice, date or boolean."
    )
    choices: list[ScannerFilterChoice] = Field(
        default_factory=list,
        description="Allowed values of a choice filter (first 50 at most).",
    )


class ScannerParameters(Truncatable):
    """A slice of IBKR's scanner catalogue; only the list for ``section`` is filled."""

    section: ScannerParameterSection
    query: str | None = Field(None, description="The substring filter applied, if any.")
    instrument: str | None = Field(None, description="The instrument filter applied, if any.")
    total: int = Field(description="How many entries matched, before the limit.")
    scan_codes: list[ScanCodeInfo] = Field(default_factory=list)
    instruments: list[ScannerInstrumentInfo] = Field(default_factory=list)
    locations: list[ScannerLocationInfo] = Field(default_factory=list)
    filters: list[ScannerFilterInfo] = Field(default_factory=list)
    fetched_at: datetime = Field(description="When the catalogue was fetched from IBKR (UTC).")


# --- scan results ---------------------------------------------------------------------


class ScannerRow(BaseModel):
    """One ranked instrument in a scan."""

    rank: int = Field(description="Position in the scan, 1 = top.")
    contract: ContractOut
    market_name: str | None = Field(None, description="IBKR market name.")
    distance: str | None = Field(None, description="Scan-specific value, when IBKR sends one.")
    benchmark: str | None = Field(None, description="Scan-specific value, when IBKR sends one.")
    projection: str | None = Field(None, description="Scan-specific value, when IBKR sends one.")
    legs: str | None = Field(None, description="Combo legs, for combo scans.")


class ScanResult(Truncatable):
    """The result of a one-shot scan, best first."""

    scan_code: str
    instrument: str
    location_code: str
    rows: list[ScannerRow]
    as_of: datetime = Field(description="When the rows arrived (UTC).")


class ScannerSnapshot(BaseModel):
    """The current rows of a live scanner subscription, best first."""

    scan_code: str
    instrument: str
    location_code: str
    rows: list[ScannerRow] = Field(default_factory=list)
    updated_at: datetime | None = Field(None, description="When IBKR last sent rows (UTC).")
    updates: int = Field(0, description="How many result sets have arrived so far.")
    no_matches: bool = Field(
        False, description="True when IBKR reported that nothing matches right now."
    )
    error: str | None = Field(None, description="The last error IBKR reported for this scan.")
