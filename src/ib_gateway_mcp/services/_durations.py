"""Durations, periods and bar sizes as IBKR's historical data service accepts them.

IBKR takes durations as ``"<n> <S|D|W|M|Y>"`` (seconds only up to a day) and rejects
small bars over long durations. The history tools accept friendlier words (``"30 mins"``,
``"2 weeks"``), normalize them here, and refuse up front the combinations IBKR is known
to reject.
"""

from __future__ import annotations

import re
from datetime import datetime

from ib_gateway_mcp.errors import InvalidRequestError
from ib_gateway_mcp.services._pacing import BAR_SECONDS, SMALL_BAR_SECONDS

__all__ = ["DURATION_GUIDE", "check_bar_request", "normalize_duration", "normalize_period"]

_DAY = 86400
_UNIT_SECONDS = {"S": 1, "D": _DAY, "W": 7 * _DAY, "M": 30 * _DAY, "Y": 365 * _DAY}
_MAX_SECONDS_DURATION = _DAY
"""IBKR wants durations over a day in D, W, M or Y."""

_DURATION_UNITS = {
    "s": "S",
    "sec": "S",
    "secs": "S",
    "second": "S",
    "seconds": "S",
    "min": "min",
    "mins": "min",
    "minute": "min",
    "minutes": "min",
    "h": "h",
    "hr": "h",
    "hrs": "h",
    "hour": "h",
    "hours": "h",
    "d": "D",
    "day": "D",
    "days": "D",
    "w": "W",
    "wk": "W",
    "wks": "W",
    "week": "W",
    "weeks": "W",
    "m": "M",
    "mo": "M",
    "mon": "M",
    "month": "M",
    "months": "M",
    "y": "Y",
    "yr": "Y",
    "yrs": "Y",
    "year": "Y",
    "years": "Y",
}
_AMOUNT_AND_UNIT = re.compile(r"^\s*(\d+)\s*([A-Za-z]+)\s*$")

_SMALL_BAR_MIN_BAR = (
    (3600, 1),
    (14400, 5),
    (28800, 10),
    (_DAY, 30),
)
"""IBKR's duration table for small bars as (duration below, smallest bar allowed):
up to 3600 S any bar; from 3600 S at least 5 secs; from 14400 S at least 10 secs; from
28800 S at least 30 secs; from 1 D at least 1 min."""

DURATION_GUIDE = (
    "IBKR's guideline for the smallest bar per duration: 1800 S: 1 secs; 3600 S: 5 secs; "
    "14400 S: 10 secs; 28800 S: 30 secs; 1 D: 1 min; 2 D: 2 mins; 1 W: 3 mins; "
    "1 M: 30 mins; 1 Y: 1 day."
)


def normalize_duration(duration: str) -> tuple[str, int]:
    """Parse a duration into IBKR's ``"<n> <S|D|W|M|Y>"`` form and its length in seconds.

    Accepts IBKR's own form (``"5 D"``, ``"1800 S"``) and words (``"30 mins"``,
    ``"2 hours"``, ``"3 days"``, ``"6 months"``). Minutes and hours become seconds. A
    single ``M`` means months, as in IBKR.

    Raises:
        InvalidRequestError: Not a duration, zero, or seconds beyond one day.
    """
    match = _AMOUNT_AND_UNIT.match(duration)
    unit = _DURATION_UNITS.get(match.group(2).lower()) if match else None
    if match is None or unit is None:
        raise InvalidRequestError(
            f"duration {duration!r} is not valid: use '<number> <unit>' with unit S "
            "(seconds, up to 86400), D (days), W (weeks), M (months) or Y (years), "
            "e.g. '1800 S', '5 D', '6 M'."
        )
    amount = int(match.group(1))
    if amount < 1:
        raise InvalidRequestError(f"duration {duration!r} must be at least 1.")
    if unit == "min":
        amount, unit = amount * 60, "S"
    elif unit == "h":
        amount, unit = amount * 3600, "S"
    if unit == "S" and amount > _MAX_SECONDS_DURATION:
        if amount % _DAY == 0:
            amount, unit = amount // _DAY, "D"
        else:
            raise InvalidRequestError(
                f"duration {duration!r} is longer than a day: IBKR wants durations over "
                "86400 S in days, weeks, months or years (e.g. '2 D')."
            )
    return f"{amount} {unit}", amount * _UNIT_SECONDS[unit]


def normalize_period(period: str) -> str:
    """Parse a histogram period into IBKR's form, e.g. ``"3 days"`` or ``"1 week"``.

    Accepts words (``"2 weeks"``) and IBKR's duration letters (``"3 D"``, ``"1 W"``,
    ``"1 M"``, ``"1 Y"``).

    Raises:
        InvalidRequestError: Not a period of days, weeks, months or years.
    """
    names = {"D": "day", "W": "week", "M": "month", "Y": "year"}
    match = _AMOUNT_AND_UNIT.match(period)
    unit = _DURATION_UNITS.get(match.group(2).lower()) if match else None
    if match is None or unit not in names:
        raise InvalidRequestError(
            f"period {period!r} is not valid: use '<number> days|weeks|months|years', "
            "e.g. '3 days' or '1 week'."
        )
    amount = int(match.group(1))
    if amount < 1:
        raise InvalidRequestError(f"period {period!r} must be at least 1.")
    name = names[unit]
    return f"{amount} {name}" if amount == 1 else f"{amount} {name}s"


def check_bar_request(
    bar_size: str, duration: str, duration_seconds: int, what_to_show: str, end: datetime | None
) -> None:
    """Refuse bar requests IBKR is known to reject (cheap, documented rules only)."""
    bar_seconds = BAR_SECONDS.get(bar_size)
    if bar_seconds is None:
        raise InvalidRequestError(
            f"bar_size {bar_size!r} is not valid; use one of: {', '.join(BAR_SECONDS)}."
        )
    if bar_seconds > duration_seconds:
        raise InvalidRequestError(
            f"bar_size {bar_size} is longer than the duration {duration}; request a duration "
            "of at least one bar."
        )
    if bar_seconds <= SMALL_BAR_SECONDS:
        smallest = 60
        for below, minimum in _SMALL_BAR_MIN_BAR:
            if duration_seconds < below:
                smallest = minimum
                break
        if bar_seconds < smallest:
            raise InvalidRequestError(
                f"IBKR does not serve {bar_size} bars over {duration}: request a shorter "
                f"duration or larger bars. {DURATION_GUIDE}"
            )
    if what_to_show == "ADJUSTED_LAST" and end is not None:
        raise InvalidRequestError(
            "what_to_show ADJUSTED_LAST only works up to now: leave end empty."
        )
