"""Historical bars and ticks, head timestamps, histograms and trading schedules.

IBKR paces historical data (the "HMDS" farm) strictly, and ib_async 2.1.0 has a few
habits the service works around:

* ``reqHistoricalDataAsync`` returns an **empty list** when its own timeout expires (it
  cancels the request at IBKR first). The service gives ib_async a timeout just below
  its own and tells a timeout (``RequestTimeoutError``) from "no data"
  (``NotFoundError``) by the time the request took.
* ``reqHistoricalTicksAsync``, ``reqHeadTimeStampAsync``, ``reqHistogramDataAsync`` and
  ``reqHistoricalScheduleAsync`` have no timeout of their own and do not cancel when
  abandoned. On a timeout the service looks the request id up by contract (ib_async's
  ``wrapper._reqId2Contract``) and cancels it, so it does not keep one of IBKR's 50
  open historical requests. (Historical ticks cannot be cancelled below API 215; for
  them only ib_async's bookkeeping is dropped.)
* Error 321 ("error validating request") is a warning to ib_async and never ends the
  request. The service watches ``errorEvent`` for 321 on its own contract and fails at
  once instead of waiting for the timeout.

Pacing: IBKR allows about 60 requests for bars of 30 seconds or less (and ticks) per
10 minutes, at most 5 for the same contract and data type within 2 seconds, and no
identical request within 15 seconds; BID_ASK counts twice. The service answers
identical requests from a 15-second cache, refuses paced requests over the limits
before sending them (``RateLimitError`` with a retry time), keeps at most 50 requests
open, and turns IBKR's pacing errors into messages that say what to do. The limits
are kept by the gateway's :class:`~ib_gateway_mcp.services._pacing.HistoricalPacing`,
which real-time bars and live bar backfills count against too.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, tzinfo
from typing import TYPE_CHECKING, Any

from ib_async import (
    BarData,
    Contract,
    HistogramData,
    HistoricalSchedule,
    HistoricalSession,
    HistoricalTick,
    HistoricalTickBidAsk,
    HistoricalTickLast,
)
from ib_async.wrapper import RequestError

from ib_gateway_mcp._ib_compat import end_request, requests_for_contract
from ib_gateway_mcp._util import (
    clamp_limit,
    clean_float,
    clean_int,
    clean_str,
    contract_to_out,
    ensure_utc,
)
from ib_gateway_mcp.errors import (
    IbApiError,
    IbGatewayMcpError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RequestTimeoutError,
)
from ib_gateway_mcp.models.common import BarSize, ContractOut, ContractSpec, WhatToShow
from ib_gateway_mcp.models.history import (
    Bar,
    BarList,
    HeadTimestamp,
    Histogram,
    HistogramEntry,
    HistoricalTickList,
    HistoricalTickOut,
    HistoricalTickType,
    ScheduleSession,
    TradingSchedule,
)
from ib_gateway_mcp.services._durations import (
    DURATION_GUIDE,
    check_bar_request,
    normalize_duration,
    normalize_period,
)
from ib_gateway_mcp.services._ibtime import zone_or_none
from ib_gateway_mcp.services._pacing import (
    BAR_SECONDS,
    IDENTICAL_REQUEST_TTL,
    MAX_OPEN_REQUESTS,
    PACING_BURST_MAX,
    PACING_BURST_WINDOW,
    PACING_MAX_REQUESTS,
    PACING_WINDOW,
    SMALL_BAR_SECONDS,
    RecentResults,
)
from ib_gateway_mcp.services.base import REQUEST_ENDING_WARNINGS, BaseService, describe_spec

if TYPE_CHECKING:
    from ib_gateway_mcp.gateway import Gateway

__all__ = [
    "BARS_LIMIT_DEFAULT",
    "BARS_LIMIT_MAX",
    "BAR_SECONDS",
    "HISTOGRAM_LIMIT_DEFAULT",
    "HISTOGRAM_LIMIT_MAX",
    "IDENTICAL_REQUEST_TTL",
    "MAX_OPEN_REQUESTS",
    "PACING_BURST_MAX",
    "PACING_BURST_WINDOW",
    "PACING_MAX_REQUESTS",
    "PACING_WINDOW",
    "SCHEDULE_DAYS_MAX",
    "SMALL_BAR_SECONDS",
    "TICKS_MAX",
    "HistoryService",
    "normalize_duration",
    "normalize_period",
]

logger = logging.getLogger(__name__)


BARS_LIMIT_DEFAULT = 1000
"""Bars ``historical_bars`` returns by default (the newest)."""
BARS_LIMIT_MAX = 10_000
TICKS_MAX = 1000
"""IBKR's maximum ticks per historical-ticks request."""
HISTOGRAM_LIMIT_DEFAULT = 200
"""Price levels ``histogram`` returns by default (the busiest)."""
HISTOGRAM_LIMIT_MAX = 1000
SCHEDULE_DAYS_MAX = 30
"""Most days one ``trading_schedule`` call may cover."""

_TIMEOUT_GRACE = 2.0
"""Seconds ``_call`` waits beyond ib_async's own historical-data timeout, so ib_async
cancels the request at IBKR first."""
_TIMED_OUT_FRACTION = 0.9
"""An empty bar answer that took at least this share of ib_async's timeout is taken as
that timeout (ib_async returns ``[]`` then), not as "no data"."""

_PACING_HINT = (
    "IBKR's historical-data pacing was exceeded (for bars of 30 seconds or less and for "
    "ticks: at most 60 requests per 10 minutes, 5 for the same contract and data type in "
    "2 seconds, and no identical request within 15 seconds; BID_ASK counts twice). Wait "
    "a minute or two before retrying, and prefer fewer, larger requests or bars of 1 "
    "minute or more."
)
_PERMISSION_HINT = (
    "Historical data needs the same market-data subscription as live quotes for this "
    "exchange and instrument type; check the login's market-data subscriptions."
)
_DROPPED_HINT = (
    "IBKR dropped the query (often after a pacing violation, a timeout or a data-farm "
    "reconnect). Wait about 15 seconds and retry."
)
_OTHER_SESSION_HINT = (
    "Another session of this login (TWS, Client Portal or mobile) on a different IP holds "
    "the market data; IBKR serves historical data to one session only. Close the other "
    "session or move the data to this one."
)
_VALIDATION_HINT = (
    "IBKR rejected the parameters: check bar_size, duration, what_to_show (e.g. forex "
    "has no TRADES) and the time range."
)
_TOO_LONG_HINT = f"Request a shorter duration or larger bars. {DURATION_GUIDE}"
_PERMISSION_CODES = frozenset({354, 10089, 10090, 10167, 10168})
_COMPETING_SESSION = 10197
"""IB error 10197: no market data during a competing live session."""


def _check_what_to_show(contract: Contract, what_to_show: str) -> None:
    if contract.secType == "CASH" and what_to_show == "TRADES":
        raise InvalidRequestError(
            "IBKR has no TRADES data for forex (CASH): use MIDPOINT, BID, ASK or BID_ASK."
        )


# --- conversions --------------------------------------------------------------------------


def _bar_time(value: date | datetime) -> date | datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    return value


def _bar_out(bar: BarData) -> Bar:
    volume = clean_float(bar.volume)
    has_volume = volume is not None and volume >= 0
    count = clean_int(bar.barCount)
    return Bar(
        time=_bar_time(bar.date),
        open=clean_float(bar.open),
        high=clean_float(bar.high),
        low=clean_float(bar.low),
        close=clean_float(bar.close),
        volume=volume if has_volume else None,
        wap=clean_float(bar.average) if has_volume else None,
        bar_count=count if count is not None and count >= 0 else None,
    )


def _tick_out(tick: object, what_to_show: HistoricalTickType) -> HistoricalTickOut | None:
    if isinstance(tick, HistoricalTickLast):
        return HistoricalTickOut(
            time=ensure_utc(tick.time),
            price=clean_float(tick.price),
            size=clean_float(tick.size),
            exchange=clean_str(tick.exchange),
            special_conditions=clean_str(tick.specialConditions),
            past_limit=tick.tickAttribLast.pastLimit,
            unreported=tick.tickAttribLast.unreported,
        )
    if isinstance(tick, HistoricalTickBidAsk):
        return HistoricalTickOut(
            time=ensure_utc(tick.time),
            bid=clean_float(tick.priceBid),
            ask=clean_float(tick.priceAsk),
            bid_size=clean_float(tick.sizeBid),
            ask_size=clean_float(tick.sizeAsk),
            bid_past_low=tick.tickAttribBidAsk.bidPastLow,
            ask_past_high=tick.tickAttribBidAsk.askPastHigh,
        )
    if isinstance(tick, HistoricalTick):
        return HistoricalTickOut(
            time=ensure_utc(tick.time),
            price=clean_float(tick.price),
            size=None if what_to_show == "MIDPOINT" else clean_float(tick.size),
        )
    logger.debug("Ignoring unexpected historical tick %r", tick)
    return None


def _histogram_entry(item: HistogramData) -> HistogramEntry | None:
    price = clean_float(item.price)
    count = clean_int(item.count)
    if price is None or count is None:
        return None
    return HistogramEntry(price=price, count=count)


def _schedule_time(text: str, zone: tzinfo | None) -> datetime | None:
    text = text.strip()
    if zone is None or not text:
        return None
    for layout in ("%Y%m%d-%H:%M:%S", "%Y%m%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(text, layout).replace(tzinfo=zone)
        except ValueError:
            continue
    logger.warning("Unparsable trading schedule time %r", text)
    return None


def _ref_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%Y%m%d").date()
    except ValueError:
        return None


def _session_out(session: HistoricalSession, zone: tzinfo | None) -> ScheduleSession:
    return ScheduleSession(
        start=_schedule_time(session.startDateTime, zone),
        end=_schedule_time(session.endDateTime, zone),
        ref_date=_ref_date(session.refDate),
    )


_HINTS: tuple[tuple[Callable[[int, str], bool], str], ...] = (
    (lambda code, text: "pacing" in text or code == 420, _PACING_HINT),
    (
        lambda code, text: (
            code in _PERMISSION_CODES or "permission" in text or "not subscribed" in text
        ),
        _PERMISSION_HINT,
    ),
    (lambda code, text: code == 366 or "query cancelled" in text, _DROPPED_HINT),
    (
        lambda code, text: (
            code == _COMPETING_SESSION or "different ip" in text or "competing" in text
        ),
        _OTHER_SESSION_HINT,
    ),
    (lambda _code, text: "exceed" in text or "duration" in text, _TOO_LONG_HINT),
    (lambda code, _text: code == 321, _VALIDATION_HINT),
)
"""Advice for IBKR errors on historical requests, first match wins."""


def _hint_for(code: int, text: str) -> str | None:
    """The advice for an IBKR error on a historical request, if there is any."""
    return next((hint for matches, hint in _HINTS if matches(code, text)), None)


def _explain(exc: IbApiError, subject: str) -> IbGatewayMcpError:
    """Turn an IBKR error on a historical request into the most useful error."""
    text = exc.error_message.lower()
    hint = _hint_for(exc.error_code, text)
    if hint is not None:
        return exc.with_hint(hint)
    if exc.error_code == 200:
        return NotFoundError(f"IBKR has no contract matching {subject}: {exc.error_message}")
    if "no data" in text:
        return NotFoundError(
            f"IBKR has no data for {subject}: {exc.error_message}. Check what_to_show "
            "(forex has no TRADES: use MIDPOINT), try use_rth=false for thinly traded "
            "instruments, check the time range, and use get_head_timestamp for the "
            "earliest available date. IBKR keeps no data for expired options."
        )
    return exc


def _spec_key(spec: ContractSpec) -> str:
    return spec.model_dump_json()


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


# --- the service --------------------------------------------------------------------------


class HistoryService(BaseService):
    """Historical bars and ticks, head timestamps, histograms and trading schedules."""

    def __init__(
        self, gateway: Gateway, *, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        super().__init__(gateway)
        self._monotonic = monotonic
        self._recent = RecentResults(monotonic)

    # --- bars ----------------------------------------------------------------------------

    async def historical_bars(
        self,
        contract: ContractSpec,
        *,
        bar_size: BarSize = "1 hour",
        duration: str = "1 D",
        end: datetime | None = None,
        what_to_show: WhatToShow = "TRADES",
        use_rth: bool = True,
        limit: int | None = None,
    ) -> BarList:
        """Fetch OHLCV bars ending at ``end`` (None: now), oldest first.

        Args:
            contract: The instrument; qualified first.
            bar_size: Bar length, spelled as IBKR wants it (``"1 min"``, ``"1 day"``...).
            duration: How far back from ``end``, e.g. ``"5 D"``, ``"1800 S"``, ``"6 M"``
                (words such as ``"30 mins"`` or ``"2 weeks"`` work too).
            end: End of the range; naive datetimes are taken as UTC.
            what_to_show: Data the bars are built from; ADJUSTED_LAST needs ``end=None``.
            use_rth: Only regular trading hours.
            limit: Bars to return (default 1000, at most 10000); the newest are kept.

        Raises:
            InvalidRequestError: A malformed duration or a combination IBKR rejects.
            NotFoundError: No such contract, or IBKR has no data in the range.
            RateLimitError: Sending the request now would break IBKR's pacing.
            RequestTimeoutError: IBKR did not finish in ``IB_REQUEST_TIMEOUT``.
            IbApiError: Any other IBKR error (with a hint for pacing and permissions).
        """
        normalized, duration_seconds = normalize_duration(duration)
        end_utc = ensure_utc(end)
        check_bar_request(bar_size, normalized, duration_seconds, what_to_show, end_utc)
        wanted = clamp_limit(limit, default=BARS_LIMIT_DEFAULT, maximum=BARS_LIMIT_MAX)
        key = (
            "bars",
            _spec_key(contract),
            _iso(end_utc),
            normalized,
            bar_size,
            what_to_show,
            use_rth,
        )
        cached = self._recent.get(key)
        if cached is None:
            qualified = await self.qualify(contract)
            _check_what_to_show(qualified, what_to_show)
            subject = f"{bar_size} {what_to_show} bars for {describe_spec(contract)}"
            if BAR_SECONDS[bar_size] <= SMALL_BAR_SECONDS:
                self.gateway.pacing.admit(qualified, "bars", what_to_show, subject)
            raw = await self._fetch_bars(
                qualified,
                end=end_utc,
                duration=normalized,
                bar_size=bar_size,
                what_to_show=what_to_show,
                use_rth=use_rth,
                subject=subject,
            )
            cached = (contract_to_out(qualified), [_bar_out(bar) for bar in raw])
            self._recent.put(key, cached)
        out, bars = cached
        kept = bars[-wanted:]
        return BarList(
            contract=out,
            bar_size=bar_size,
            duration=normalized,
            what_to_show=what_to_show,
            use_rth=use_rth,
            end=end_utc,
            total=len(bars),
            bars=kept,
            truncated=len(bars) > wanted,
        )

    async def _fetch_bars(
        self,
        contract: Contract,
        *,
        end: datetime | None,
        duration: str,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
        subject: str,
    ) -> Sequence[BarData]:
        ib = self.ib
        seconds = self.settings.request_timeout
        sent_at: list[float] = []

        def send() -> Awaitable[Sequence[BarData]]:
            sent_at.append(self._monotonic())  # after any wait for a free request slot
            return ib.reqHistoricalDataAsync(
                contract,
                end or "",
                duration,
                bar_size,
                what_to_show,
                use_rth,
                formatDate=2,
                timeout=seconds,
            )

        bars = await self._request(
            send,
            contract,
            subject=subject,
            timeout=seconds + _TIMEOUT_GRACE,
            cancel="cancelHistoricalData",
        )
        if bars:
            return list(bars)
        # Loop timers may fire a clock tick early, so "about as long as the timeout" counts.
        if sent_at and self._monotonic() - sent_at[0] >= seconds * _TIMED_OUT_FRACTION:
            # ib_async's own timeout: it cancelled the request at IBKR and returned [].
            raise RequestTimeoutError(
                self._timeout_message(subject, seconds)
                + " Large requests take longer: request a shorter duration or larger bars."
            )
        raise NotFoundError(
            f"IBKR returned no {subject} over {duration}"
            f"{f' ending {end.isoformat()}' if end else ''}. Check the time range (weekends "
            "and holidays have no bars), try use_rth=false, and use get_head_timestamp for "
            "the earliest available date."
        )

    # --- ticks ---------------------------------------------------------------------------

    async def historical_ticks(
        self,
        contract: ContractSpec,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        count: int = TICKS_MAX,
        what_to_show: HistoricalTickType = "TRADES",
        use_rth: bool = True,
        ignore_size: bool = False,
    ) -> HistoricalTickList:
        """Fetch up to ``count`` historical ticks after ``start`` or before ``end``.

        Give exactly one of ``start`` and ``end`` (naive datetimes are taken as UTC).
        IBKR may return a few more than ``count`` to complete the last second.

        Raises:
            InvalidRequestError: Both or neither of start/end, count outside 1-1000, or
                TRADES for forex.
            NotFoundError: No such contract, or no ticks in the range.
            RateLimitError: Sending the request now would break IBKR's pacing.
            RequestTimeoutError, IbApiError: As for :meth:`historical_bars`.
        """
        if (start is None) == (end is None):
            raise InvalidRequestError(
                "give exactly one of start (ticks from then on) and end (ticks up to then)."
            )
        if not 1 <= count <= TICKS_MAX:
            raise InvalidRequestError(
                f"count must be 1-{TICKS_MAX}; IBKR sends at most {TICKS_MAX} ticks per "
                "request. Page with start or end for more."
            )
        start_utc, end_utc = ensure_utc(start), ensure_utc(end)
        key = (
            "ticks",
            _spec_key(contract),
            _iso(start_utc),
            _iso(end_utc),
            count,
            what_to_show,
            use_rth,
            ignore_size,
        )
        cached = self._recent.get(key)
        if cached is None:
            qualified = await self.qualify(contract)
            _check_what_to_show(qualified, what_to_show)
            subject = f"{what_to_show} ticks for {describe_spec(contract)}"
            self.gateway.pacing.admit(qualified, "ticks", what_to_show, subject)
            ib = self.ib
            raw = await self._request(
                lambda: ib.reqHistoricalTicksAsync(
                    qualified,
                    start_utc or "",
                    end_utc or "",
                    count,
                    what_to_show,
                    use_rth,
                    ignoreSize=ignore_size,
                ),
                qualified,
                subject=subject,
            )
            ticks = [row for tick in raw or [] if (row := _tick_out(tick, what_to_show))]
            if not ticks:
                edge = f"after {_iso(start_utc)}" if start_utc else f"before {_iso(end_utc)}"
                raise NotFoundError(
                    f"IBKR returned no {subject} {edge}. Check the time range (was the "
                    "market open?), try use_rth=false, and note that IBKR serves historical "
                    "ticks only with market-data permissions for the instrument."
                )
            cached = (contract_to_out(qualified), ticks)
            self._recent.put(key, cached)
        out, ticks = cached
        return HistoricalTickList(
            contract=out,
            what_to_show=what_to_show,
            use_rth=use_rth,
            start=start_utc,
            end=end_utc,
            count=count,
            ticks=ticks,
            truncated=len(ticks) >= count,
        )

    # --- head timestamp --------------------------------------------------------------------

    async def head_timestamp(
        self,
        contract: ContractSpec,
        *,
        what_to_show: WhatToShow = "TRADES",
        use_rth: bool = True,
    ) -> HeadTimestamp:
        """Return the earliest time IBKR has ``what_to_show`` data for the contract.

        Raises:
            NotFoundError: No such contract, or no data of that type.
            RequestTimeoutError, IbApiError: As for :meth:`historical_bars`.
        """
        key = ("head", _spec_key(contract), what_to_show, use_rth)
        cached = self._recent.get(key)
        if cached is None:
            qualified = await self.qualify(contract)
            _check_what_to_show(qualified, what_to_show)
            subject = f"the earliest {what_to_show} data for {describe_spec(contract)}"
            ib = self.ib
            earliest: object = await self._request(
                lambda: ib.reqHeadTimeStampAsync(qualified, what_to_show, use_rth, formatDate=2),
                qualified,
                subject=subject,
                cancel="cancelHeadTimeStamp",
            )
            if not isinstance(earliest, date):
                raise NotFoundError(f"IBKR reported no {subject}.")
            cached = (contract_to_out(qualified), ensure_utc(earliest))
            self._recent.put(key, cached)
        out, earliest_utc = cached
        return HeadTimestamp(
            contract=out, what_to_show=what_to_show, use_rth=use_rth, earliest=earliest_utc
        )

    # --- histogram -----------------------------------------------------------------------

    async def histogram(
        self,
        contract: ContractSpec,
        *,
        period: str = "1 week",
        use_rth: bool = True,
        limit: int | None = None,
    ) -> Histogram:
        """Return how much traded at each price over ``period`` (e.g. ``"3 days"``).

        With more price levels than ``limit`` (default 200, at most 1000), the busiest
        levels are kept; entries are sorted by price.

        Raises:
            InvalidRequestError: A malformed period.
            NotFoundError: No such contract, or no data for the period.
            RequestTimeoutError, IbApiError: As for :meth:`historical_bars`.
        """
        normalized = normalize_period(period)
        wanted = clamp_limit(limit, default=HISTOGRAM_LIMIT_DEFAULT, maximum=HISTOGRAM_LIMIT_MAX)
        key = ("histogram", _spec_key(contract), normalized, use_rth)
        cached = self._recent.get(key)
        if cached is None:
            qualified = await self.qualify(contract)
            subject = f"the {normalized} histogram for {describe_spec(contract)}"
            ib = self.ib
            raw = await self._request(
                lambda: ib.reqHistogramDataAsync(qualified, use_rth, normalized),
                qualified,
                subject=subject,
                cancel="cancelHistogramData",
            )
            entries = [entry for item in raw or [] if (entry := _histogram_entry(item))]
            if not entries:
                raise NotFoundError(
                    f"IBKR returned no data for {subject}. Try a longer period or use_rth=false."
                )
            cached = (contract_to_out(qualified), entries)
            self._recent.put(key, cached)
        out, entries = cached
        kept = sorted(entries, key=lambda entry: entry.count, reverse=True)[:wanted]
        return Histogram(
            contract=out,
            period=normalized,
            use_rth=use_rth,
            total=len(entries),
            entries=sorted(kept, key=lambda entry: entry.price),
            truncated=len(entries) > wanted,
        )

    # --- trading schedule ----------------------------------------------------------------

    async def trading_schedule(
        self,
        contract: ContractSpec,
        *,
        num_days: int = 5,
        end: datetime | None = None,
        use_rth: bool = True,
    ) -> TradingSchedule:
        """Return the trading sessions of the ``num_days`` days up to ``end`` (None: now).

        Raises:
            InvalidRequestError: ``num_days`` outside 1-30.
            NotFoundError: No such contract, or no sessions in the range.
            RequestTimeoutError, IbApiError: As for :meth:`historical_bars`.
        """
        if not 1 <= num_days <= SCHEDULE_DAYS_MAX:
            raise InvalidRequestError(f"num_days must be 1-{SCHEDULE_DAYS_MAX}.")
        end_utc = ensure_utc(end)
        key = ("schedule", _spec_key(contract), num_days, _iso(end_utc), use_rth)
        cached = self._recent.get(key)
        if cached is None:
            qualified = await self.qualify(contract)
            subject = f"the trading schedule for {describe_spec(contract)}"
            ib = self.ib
            schedule: object = await self._request(
                lambda: ib.reqHistoricalScheduleAsync(qualified, num_days, end_utc or "", use_rth),
                qualified,
                subject=subject,
                cancel="cancelHistoricalData",
            )
            if not isinstance(schedule, HistoricalSchedule) or not schedule.sessions:
                raise NotFoundError(
                    f"IBKR returned no sessions for {subject} in the {num_days} days up to "
                    f"{_iso(end_utc) or 'now'}."
                )
            cached = (contract_to_out(qualified), schedule)
            self._recent.put(key, cached)
        out, schedule = cached
        return _schedule_out(out, schedule, use_rth)

    # --- plumbing ------------------------------------------------------------------------

    async def _request[T](
        self,
        make: Callable[[], Awaitable[T]],
        contract: Contract,
        *,
        subject: str,
        timeout: float | None = None,
        cancel: str | None = None,
    ) -> T:
        """Send one historical request and translate what can go wrong.

        Holds one of :data:`MAX_OPEN_REQUESTS` slots, fails at once on error 321 for
        ``contract``, abandons the request on a timeout (cancelling it at IBKR with
        ``ib.client.<cancel>`` when there is such a call), and maps IBKR's errors through
        :func:`_explain`.
        """

        async def run() -> T:
            async with self.gateway.pacing.slot():
                return await self._unless_rejected(make, contract)

        try:
            return await self._call(run, what=subject, timeout=timeout)
        except RequestTimeoutError:
            self._abandon(contract, cancel)
            raise
        except IbApiError as exc:
            raise _explain(exc, subject) from exc

    async def _unless_rejected[T](self, make: Callable[[], Awaitable[T]], contract: Contract) -> T:
        """Send ``make()`` and await it, failing fast on a request-ending warning for ``contract``.

        ib_async reports errors with the contract of the request they belong to, so the
        request id is not needed: ``contract`` is this request's own object. IBKR has
        refused a rejected request, but ib_async still tracks it, so it is forgotten.
        """
        ib = self.ib

        def ended(
            req_id: int, code: int, message: str, err_contract: object
        ) -> RequestError | None:
            if err_contract is contract and code in REQUEST_ENDING_WARNINGS:
                return RequestError(req_id, code, message)
            return None

        return await self._await_or_reject(
            make, ended, on_reject=lambda error: self._forget(ib, error.reqId)
        )

    def _abandon(self, contract: Contract, cancel: str | None) -> None:
        """Give up the requests still open for ``contract`` (after a timeout).

        Cancels each at IBKR with ``ib.client.<cancel>`` when given (historical ticks have
        no cancel call at API 178) and drops ib_async's bookkeeping for it.
        """
        try:
            ib = self.ib
        except NotConnectedError:
            return
        for req_id in requests_for_contract(ib.wrapper, contract):
            if cancel is not None:
                try:
                    getattr(ib.client, cancel)(req_id)
                except Exception:  # best effort: the timeout is what the caller needs to hear
                    logger.warning("Could not cancel historical request %s", req_id, exc_info=True)
            self._forget(ib, req_id)

    @staticmethod
    def _forget(ib: Any, req_id: int) -> None:
        """Drop ib_async's pending state for ``req_id``.

        The caller's future is already cancelled or failed, so nothing is resolved; a late
        answer from IBKR for this id is then ignored by ib_async.
        """
        try:
            end_request(ib.wrapper, req_id)
        except Exception:  # best effort
            logger.warning("Could not drop historical request %s", req_id, exc_info=True)


def _schedule_out(out: ContractOut, schedule: HistoricalSchedule, use_rth: bool) -> TradingSchedule:
    zone = zone_or_none(schedule.timeZone, what="a trading schedule")
    return TradingSchedule(
        contract=out,
        time_zone=schedule.timeZone,
        use_rth=use_rth,
        start=_schedule_time(schedule.startDateTime, zone),
        end=_schedule_time(schedule.endDateTime, zone),
        sessions=[_session_out(session, zone) for session in schedule.sessions],
    )
