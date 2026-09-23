"""IBKR's pacing of historical data, shared by every request that counts against it.

IBKR allows about 60 requests for bars of 30 seconds or less (and ticks) per 10
minutes, at most 5 for the same contract and data type within 2 seconds, and no
identical request within 15 seconds; BID_ASK counts twice. At most 50 historical
requests may be open at once. Real-time bars and the backfill of live-updating bars
count against the same limits, and a violation (162, 420) blocks the history tools
too, so one :class:`HistoricalPacing` per gateway (``Gateway.pacing``) serves the
history and market data services alike.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Hashable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from ib_gateway_mcp.errors import RateLimitError

if TYPE_CHECKING:
    from ib_async import Contract

__all__ = [
    "BAR_SECONDS",
    "IDENTICAL_REQUEST_TTL",
    "MAX_OPEN_REQUESTS",
    "PACING_BURST_MAX",
    "PACING_BURST_WINDOW",
    "PACING_MAX_REQUESTS",
    "PACING_WINDOW",
    "SMALL_BAR_SECONDS",
    "HistoricalPacing",
    "RecentResults",
]

PACING_WINDOW = 600.0
"""Seconds in IBKR's long pacing window for small bars and ticks."""
PACING_MAX_REQUESTS = 60
"""Paced requests IBKR allows per :data:`PACING_WINDOW`."""
PACING_BURST_WINDOW = 2.0
"""Seconds in IBKR's burst window (same contract, exchange and data type)."""
PACING_BURST_MAX = 5
"""Paced requests allowed per burst window; IBKR rejects the sixth."""
IDENTICAL_REQUEST_TTL = 15.0
"""IBKR rejects an identical historical request within this many seconds; the history
service answers it from its cache instead."""
MAX_OPEN_REQUESTS = 50
"""IBKR's limit on simultaneously open historical requests."""
SMALL_BAR_SECONDS = 30
"""Bars this size or smaller fall under IBKR's strict pacing and duration limits."""

BAR_SECONDS: Mapping[str, int] = MappingProxyType(
    {
        "1 secs": 1,
        "5 secs": 5,
        "10 secs": 10,
        "15 secs": 15,
        "30 secs": 30,
        "1 min": 60,
        "2 mins": 120,
        "3 mins": 180,
        "5 mins": 300,
        "10 mins": 600,
        "15 mins": 900,
        "20 mins": 1200,
        "30 mins": 1800,
        "1 hour": 3600,
        "2 hours": 7200,
        "3 hours": 10800,
        "4 hours": 14400,
        "8 hours": 28800,
        "1 day": 86400,
        "1 week": 7 * 86400,
        "1 month": 30 * 86400,
    }
)
"""Length of each :data:`~ib_gateway_mcp.models.common.BarSize` in seconds (a month
counts 30 days)."""


class _Pacer:
    """Client-side copy of IBKR's pacing rules for small bars and ticks."""

    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._now = monotonic
        self._window: deque[float] = deque()
        self._bursts: dict[Hashable, deque[float]] = {}

    def admit(self, burst_key: Hashable, weight: int, subject: str) -> None:
        """Record a paced request, or refuse it with the time until it would be allowed.

        Raises:
            RateLimitError: Sending it now would break IBKR's pacing.
        """
        now = self._now()
        while self._window and now - self._window[0] >= PACING_WINDOW:
            self._window.popleft()
        for key in list(self._bursts):
            stamps = self._bursts[key]
            while stamps and now - stamps[0] >= PACING_BURST_WINDOW:
                stamps.popleft()
            if not stamps:
                del self._bursts[key]
        burst = self._bursts.get(burst_key, deque())
        overflow = len(self._window) + weight - PACING_MAX_REQUESTS
        if overflow > 0:
            wait = PACING_WINDOW - (now - self._window[overflow - 1])
            raise RateLimitError(
                f"Not requesting {subject}: IBKR allows {PACING_MAX_REQUESTS} requests for bars "
                f"of 30 seconds or less and ticks per {PACING_WINDOW / 60:g} minutes (BID_ASK "
                f"counts twice), and this server has sent that many. Retry in {wait:.0f} s, "
                "or use bars of 1 minute or more, which IBKR does not pace this way."
            )
        burst_overflow = len(burst) + weight - PACING_BURST_MAX
        if burst_overflow > 0:
            wait = PACING_BURST_WINDOW - (now - burst[burst_overflow - 1])
            raise RateLimitError(
                f"Not requesting {subject}: IBKR rejects a sixth request for the same contract "
                f"and data type within {PACING_BURST_WINDOW:g} seconds. Retry in {wait:.1f} s."
            )
        self._window.extend([now] * weight)
        burst.extend([now] * weight)
        self._bursts[burst_key] = burst


class HistoricalPacing:
    """IBKR's pacing limits and open-request cap for historical data, for one gateway.

    Args:
        monotonic: The clock the pacing windows are measured with (tests pass a fake).
    """

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._pacer = _Pacer(monotonic)
        self._open_requests = asyncio.Semaphore(MAX_OPEN_REQUESTS)

    def admit(self, contract: Contract, kind: str, what_to_show: str, subject: str) -> None:
        """Count a paced request (small bars, ticks) against IBKR's limits.

        ``kind`` (``"bars"`` or ``"ticks"``) and ``what_to_show`` pick the burst window
        (with the contract and exchange); BID_ASK counts twice, as at IBKR.

        Raises:
            RateLimitError: Sending the request now would break IBKR's pacing.
        """
        weight = 2 if what_to_show == "BID_ASK" else 1
        key = (contract.conId, contract.exchange, kind, what_to_show)
        self._pacer.admit(key, weight, subject)

    def admit_bars(
        self, contract: Contract, *, what_to_show: str, subject: str, bar_size: str | None = None
    ) -> None:
        """Count a streaming bar request against the historical pacing budget.

        IBKR paces real-time bars (``bar_size`` None: always 5-second bars) and the
        backfill of live-updating bars of 30 seconds or less like historical requests.
        Larger bar sizes are not counted.

        Raises:
            RateLimitError: Sending the request now would break IBKR's pacing.
        """
        if bar_size is not None and BAR_SECONDS.get(bar_size, 0) > SMALL_BAR_SECONDS:
            return
        self.admit(contract, "bars", what_to_show, subject)

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one of IBKR's :data:`MAX_OPEN_REQUESTS` historical request slots."""
        async with self._open_requests:
            yield


@dataclass(frozen=True)
class _Cached:
    at: float
    value: Any


class RecentResults:
    """Answers identical requests made within :data:`IDENTICAL_REQUEST_TTL` seconds."""

    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._now = monotonic
        self._entries: dict[Hashable, _Cached] = {}

    def get(self, key: Hashable) -> Any | None:
        self._purge()
        entry = self._entries.get(key)
        return entry.value if entry is not None else None

    def put(self, key: Hashable, value: Any) -> None:
        self._purge()
        self._entries[key] = _Cached(self._now(), value)

    def _purge(self) -> None:
        """Drop entries older than the TTL, so large bar lists are not kept around."""
        now = self._now()
        for stale in [k for k, v in self._entries.items() if now - v.at >= IDENTICAL_REQUEST_TTL]:
            del self._entries[stale]
