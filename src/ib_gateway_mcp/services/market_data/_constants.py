"""Limits of the market data service, and what IBKR's market data errors mean."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

MAX_QUOTE_CONTRACTS = 25
"""Most contracts one ``quotes`` call takes (each snapshot holds a market data line)."""
DEPTH_ROWS_MAX = 50
"""Most order book levels per side a depth stream may request."""
TICK_BUFFER_MAX = 5000
"""Largest tick-by-tick ring buffer."""
REALTIME_BUFFER_MAX = 17280
"""Largest 5-second bar ring buffer (one day)."""
BARS_KEPT_MAX = 5000
"""Most bars a live bar series keeps (older ones are dropped as new bars arrive)."""
DATA_LIMIT_DEFAULT = 100
"""Default number of ticks or bars ``subscription_data`` returns."""
DATA_LIMIT_MAX = 5000
"""Cap on ``subscription_data``'s ``limit``."""
NOTICES_KEPT = 10
"""IBKR messages each stream remembers."""

_REALTIME_BAR_SECONDS = 5
_DURATION = re.compile(r"^[1-9]\d* [SDWMY]$")
_DELAYED_TYPES = frozenset({3, 4})
_TIME_KEYS = ("time", "date", "received_at")
"""Keys that mark a snapshot list as a time series ``subscription_data`` may window."""

_NOTICE_CODES = frozenset({165, 317, 10090, 10091, 10167, 10225})
"""Codes about a stream that do not stop it (besides the 2100-2199 informational range)."""
_LIMIT_CODES = frozenset({101, 309, 10190})
"""Codes that mean an IBKR-side concurrency limit was hit."""

_HINTS: Mapping[int, str] = MappingProxyType(
    {
        101: (
            "IBKR's limit on simultaneous market data lines is reached (100 by default, "
            "shared by every API client and TWS session on this login). Unsubscribe from "
            "some streams; list_subscriptions shows them."
        ),
        162: (
            "IBKR's historical data service refused the request: a pacing violation (wait a "
            "minute and retry), missing market data permissions, or no data for the period."
        ),
        200: "IBKR does not know this contract for market data; check it with qualify_contract.",
        309: (
            "IBKR allows 3 market depth streams at a time by default (more with Quote "
            "Booster packs). Unsubscribe from one first."
        ),
        310: "IBKR has no such market depth stream (it already ended).",
        317: "IBKR reset the order book; it is rebuilt as updates arrive.",
        321: (
            "IBKR rejected the request as invalid for this instrument (for example a "
            "generic tick, bar size, duration or what_to_show it does not offer for this "
            "security type); the message names the field."
        ),
        354: (
            "The login has no market data subscription for this instrument. Call "
            "set_market_data_type with data_type 'delayed' for free 15-20 minute delayed "
            "data, or subscribe to the exchange's data in IBKR's Client Portal."
        ),
        420: (
            "IBKR refused the real-time bars request: not available for this instrument, "
            "or a pacing violation."
        ),
        # IB Gateway 10.45 also answers 10089, and sends nothing, with delayed selected.
        10089: (
            "This instrument needs an additional market data subscription for API use. "
            "With live data selected, call set_market_data_type with data_type 'delayed' "
            "and retry; if delayed data is already selected, IBKR offers this login no "
            "delayed data for it either."
        ),
        10090: "Part of the requested market data is not subscribed; some fields stay empty.",
        10091: (
            "Part of the requested market data needs an additional subscription for API "
            "use; those fields stay empty (set_market_data_type 'delayed' fills them with "
            "delayed data)."
        ),
        10092: (
            "IBKR offers no market depth for this security on this exchange; try "
            "smart_depth=true, or an exchange listed by get_depth_exchanges."
        ),
        10167: "No live subscription: IBKR sends delayed data (15-20 minutes old) instead.",
        10168: (
            "No live subscription, and delayed data is not enabled: call "
            "set_market_data_type with data_type 'delayed' and retry."
        ),
        10189: (
            "IBKR refused the tick-by-tick request; it needs a live market data "
            "subscription for the instrument."
        ),
        10190: (
            "IBKR's limit on simultaneous tick-by-tick streams is reached (usually 3). "
            "Unsubscribe from one first."
        ),
        10197: (
            "No market data during a competing live session: the same IBKR username is "
            "using market data in TWS, the mobile app or the web portal. Close it there, "
            "or use delayed data."
        ),
        10225: "IBKR interrupted the bar stream (bust event); it was re-requested.",
    }
)
"""What common market data errors mean, and what to do about them."""

_SNAPSHOT_HINTS: Mapping[int, str] = MappingProxyType(
    {
        **_HINTS,
        10090: (
            "Part of this snapshot's data is not subscribed, and a snapshot ends on that "
            "error (ib_async fails it). subscribe_quotes keeps streaming the subscribed "
            "part; set_market_data_type with 'delayed' may fill the rest with delayed data."
        ),
    }
)
"""Hints for snapshot errors, where some stream notices end the request."""
