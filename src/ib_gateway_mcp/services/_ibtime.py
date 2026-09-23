"""IBKR's time formats: zone names, timestamps it sends, and the UTC stamp it takes."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from types import MappingProxyType
from zoneinfo import ZoneInfo

from ib_gateway_mcp._util import clean_str, ensure_utc

__all__ = ["TZ_ABBREVIATIONS", "ib_utc_stamp", "parse_ib_time", "zone_or_none"]

logger = logging.getLogger(__name__)

TZ_ABBREVIATIONS: Mapping[str, int] = MappingProxyType(
    {
        "UTC": 0,
        "GMT": 0,
        "EST": -5,
        "EDT": -4,
        "CST": -6,
        "CDT": -5,
        "MST": -7,
        "MDT": -6,
        "PST": -8,
        "PDT": -7,
        "BST": 1,
        "CET": 1,
        "CEST": 2,
        "MET": 1,
        "MEST": 2,
        "HKT": 8,
        "JST": 9,
    }
)
"""UTC offsets (hours) of zone abbreviations IBKR uses in timestamps that are not IANA
names."""

_IB_TIME = re.compile(r"^(\d{8})[ -]+(\d{1,2}:\d{2}:\d{2})(?:\s+(\S+))?$")


def zone_or_none(name: str | None, *, what: str) -> tzinfo | None:
    """The IANA time zone ``name`` (e.g. ``US/Eastern``), or None when blank or unknown.

    ``what`` says where the name came from, for the log line about an unknown one.
    """
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        logger.info("Unknown time zone %r in %s", name, what)
        return None


def parse_ib_time(text: str | None) -> datetime | None:
    """Parse an IBKR timestamp such as ``20260102 10:30:00 America/New_York`` into UTC.

    ``yyyymmdd-hh:mm:ss`` (dash, no zone) is UTC by IBKR's convention. Returns None for
    anything else it cannot place in time (e.g. no zone at all).
    """
    raw = clean_str(text)
    if raw is None:
        return None
    match = _IB_TIME.match(raw)
    if match is None:
        return None
    day, clock, zone = match.groups()
    try:
        naive = datetime.strptime(f"{day} {clock}", "%Y%m%d %H:%M:%S")
    except ValueError:
        return None
    tz: tzinfo | None
    if zone is None:
        tz = UTC if "-" in raw else None
    elif zone.upper() in TZ_ABBREVIATIONS:
        tz = timezone(timedelta(hours=TZ_ABBREVIATIONS[zone.upper()]))
    else:
        tz = zone_or_none(zone, what="an IBKR timestamp")
    if tz is None:
        return None
    return naive.replace(tzinfo=tz).astimezone(UTC)


def ib_utc_stamp(value: datetime) -> str:
    """IBKR's UTC timestamp format for order times, e.g. ``20261218-21:00:00``."""
    return ensure_utc(value).astimezone(UTC).strftime("%Y%m%d-%H:%M:%S")
