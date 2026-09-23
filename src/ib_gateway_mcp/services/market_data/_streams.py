"""The market data streams behind the subscriptions: one class per stream kind."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, TypedDict

from ib_async import IB, BarDataList, Contract, RealTimeBarList, Ticker
from pydantic import BaseModel

from ib_gateway_mcp._util import clean_int, ensure_utc, quote_from_ticker, utc_now
from ib_gateway_mcp.errors import IbApiError, NotConnectedError, NotFoundError
from ib_gateway_mcp.models.common import (
    BarSize,
    ContractOut,
    QuoteOut,
    RealtimeWhatToShow,
    WhatToShow,
)
from ib_gateway_mcp.models.market_data import (
    BidAskTick,
    DepthData,
    GenericTick,
    LiveBarsData,
    MarketDataNotice,
    MidPointTick,
    QuoteStreamData,
    RealtimeBar,
    RealtimeBarsData,
    TickByTickData,
    TickByTickType,
    TradeTick,
)
from ib_gateway_mcp.services.market_data._constants import (
    _HINTS,
    _REALTIME_BAR_SECONDS,
    BARS_KEPT_MAX,
    NOTICES_KEPT,
)
from ib_gateway_mcp.services.market_data._convert import (
    _convert_tick,
    _describe,
    _extras,
    _generic_tick_list,
    _is_notice,
    _levels,
    _realtime_bar,
    _sorted_ticks,
    _stream_bar,
    _ticker_req_id,
    _update_event,
)

logger = logging.getLogger(__name__)


# --- streams ------------------------------------------------------------------------------


class _StreamStatus(TypedDict):
    """The fields every stream snapshot model starts with."""

    contract: ContractOut
    active: bool
    error: MarketDataNotice | None
    notices: list[MarketDataNotice]


class _Stream(ABC):
    """What every market-data stream shares: the IBKR messages about its request, and
    the short wait for its first data (or error) after it opens."""

    def __init__(self, ib: IB, contract: Contract, out: ContractOut) -> None:
        self.ib = ib
        self.contract = contract
        self.out = out
        self.req_id: int | None = None
        self.notices: deque[MarketDataNotice] = deque(maxlen=NOTICES_KEPT)
        self.error: MarketDataNotice | None = None
        self.failure: IbApiError | None = None
        self._settled = asyncio.Event()
        self._listening = True
        ib.errorEvent += self._on_error

    # errors --------------------------------------------------------------------------------

    def _is_mine(self, req_id: int, contract: object) -> bool:
        if self.req_id is not None:
            return req_id == self.req_id
        # Request id not known yet (a bar backfill still loading): ib_async reports errors
        # with the Contract object of the request, and this stream's object is its own.
        # Matching the conId instead would take errors of other requests (a snapshot of
        # the same instrument) for this stream's.
        return contract is self.contract

    def _on_error(self, req_id: int, code: int, message: str, contract: object) -> None:
        if not self._is_mine(req_id, contract):
            return
        fatal = not _is_notice(code)
        notice = MarketDataNotice(
            code=code, message=message, hint=_HINTS.get(code), at=utc_now(), fatal=fatal
        )
        self.notices.append(notice)
        if fatal and self.error is None:
            self.error = notice
            self.failure = IbApiError(code, message, req_id)
            self._settled.set()
            logger.info("Market data stream for %s stopped: %s", _describe(self.contract), message)

    def _data_arrived(self) -> None:
        self._settled.set()

    def _reset_status(self) -> None:
        self.req_id = None
        self.error = None
        self.failure = None
        self._settled = asyncio.Event()

    async def settle(self, seconds: float) -> None:
        """Wait up to ``seconds`` for the first data or an error; raise the error."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._settled.wait()
        if self.failure is not None:
            raise self.failure

    def _stop_listening(self) -> None:
        if self._listening:
            self.ib.errorEvent -= self._on_error
            self._listening = False

    # lifecycle -----------------------------------------------------------------------------

    @abstractmethod
    async def start(self, settle_seconds: float) -> None:
        """Send the request and wait for it to settle."""

    @abstractmethod
    def cancel(self) -> None:
        """Stop the stream at IBKR and stop listening. Safe to call more than once."""

    @abstractmethod
    def snapshot(self) -> BaseModel:
        """The stream's current state as a pydantic model."""

    @abstractmethod
    def resubscribe(self) -> Awaitable[None] | None:
        """Re-request the stream after IBKR lost it (1101) or after a reconnect."""

    def status(self) -> _StreamStatus:
        return {
            "contract": self.out,
            "active": self.error is None,
            "error": self.error,
            "notices": list(self.notices),
        }


class _TickerStream(_Stream):
    """A stream that lands in the contract's shared ``Ticker`` (quotes, depth, ticks)."""

    request_key: str = ""
    """The key ib_async files the request under in ``wrapper.ticker2ReqId``."""

    def __init__(self, ib: IB, contract: Contract, out: ContractOut) -> None:
        super().__init__(ib, contract, out)
        self.ticker: Ticker | None = None

    @abstractmethod
    def _send(self) -> Ticker:
        """Send the request; ib_async hands back the contract's ``Ticker``."""

    @abstractmethod
    def _stop(self) -> None:
        """Cancel the request at IBKR."""

    @abstractmethod
    def _on_update(self, ticker: Ticker) -> None:
        """Handle one update of the shared ``Ticker``."""

    def request(self) -> None:
        """Send the request (again); follows the ``Ticker`` ib_async hands back."""
        ticker = self._send()
        if ticker is not self.ticker:
            self._unwatch()
            self.ticker = ticker
            _update_event(ticker).connect(self._on_update)
        self.req_id = _ticker_req_id(self.ib, ticker, self.request_key)

    def _unwatch(self) -> None:
        if self.ticker is not None:
            _update_event(self.ticker).disconnect(self._on_update)

    async def start(self, settle_seconds: float) -> None:
        self.request()
        await self.settle(settle_seconds)

    def resubscribe(self) -> None:
        """Re-request after IBKR lost the stream (1101) or after a reconnect."""
        self._reset_status()
        self.request()

    def cancel(self) -> None:
        self._unwatch()
        self._stop_listening()
        if self.ticker is not None and self.ib.isConnected():
            self._stop()
        self.ticker = None


class _QuoteStream(_TickerStream):
    request_key = "mktData"

    def __init__(
        self, ib: IB, contract: Contract, out: ContractOut, generic_ticks: Iterable[GenericTick]
    ) -> None:
        super().__init__(ib, contract, out)
        self.generic_ticks = _sorted_ticks(generic_ticks)
        self.updates = 0
        self._widening = asyncio.Lock()

    def _send(self) -> Ticker:
        return self.ib.reqMktData(self.contract, _generic_tick_list(self.generic_ticks))

    def _stop(self) -> None:
        self.ib.cancelMktData(self.contract)

    def _on_update(self, ticker: Ticker) -> None:
        # The contract's Ticker is shared with its depth and tick-by-tick streams; a
        # packet carrying only their ticks is not a quote update.
        if ticker.ticks or not (ticker.domTicks or ticker.tickByTicks):
            self.updates += 1
            self._data_arrived()

    def _restart_or_raise(
        self, generic_ticks: Iterable[GenericTick], fallback: Sequence[GenericTick]
    ) -> None:
        """:meth:`_restart`, turning a dropped socket into :class:`NotConnectedError`.

        The stream keeps ``fallback`` as its ticks, so the re-request after the
        reconnect restores it as it was.
        """
        try:
            self._restart(generic_ticks)
        except ConnectionError as exc:
            self.generic_ticks = _sorted_ticks(fallback)
            raise NotConnectedError(
                f"Lost the gateway connection while re-requesting the quote stream: {exc}. "
                "It is requested again with its previous ticks once the gateway is back; "
                "check get_health."
            ) from exc

    def _restart(self, generic_ticks: Iterable[GenericTick]) -> None:
        if not self._listening:
            return  # unsubscribed meanwhile: must not re-open it
        if self.ticker is not None and self.ib.isConnected():
            self._stop()
        self.generic_ticks = _sorted_ticks(generic_ticks)
        self._reset_status()
        self.request()

    async def widen(self, generic_ticks: Iterable[GenericTick], settle_seconds: float) -> bool:
        """Add generic ticks by re-requesting the stream; roll back if IBKR refuses them.

        Returns whether the stream was re-requested. Concurrent calls run one at a time,
        so each re-request settles before the next one replaces it.
        """
        async with self._widening:
            before = list(self.generic_ticks)
            wanted = set(before) | set(generic_ticks)
            if wanted == set(before) or not self._listening:
                return False
            self._restart_or_raise(wanted, before)
            try:
                await self.settle(settle_seconds)
            except IbApiError:
                self._restart_or_raise(before, before)
                raise
            return True

    def snapshot(self) -> QuoteStreamData:
        ticker = self.ticker
        quote = (
            quote_from_ticker(ticker, self.out)
            if ticker is not None
            else QuoteOut(contract=self.out)
        )
        return QuoteStreamData(
            **self.status(),
            quote=quote,
            generic_ticks=self.generic_ticks,
            extras=_extras(ticker) if ticker is not None and self.generic_ticks else None,
            updates=self.updates,
        )


class _DepthStream(_TickerStream):
    request_key = "mktDepth"

    def __init__(
        self, ib: IB, contract: Contract, out: ContractOut, rows: int, smart_depth: bool
    ) -> None:
        super().__init__(ib, contract, out)
        self.rows = rows
        self.smart_depth = smart_depth

    def _send(self) -> Ticker:
        return self.ib.reqMktDepth(self.contract, numRows=self.rows, isSmartDepth=self.smart_depth)

    def _stop(self) -> None:
        self.ib.cancelMktDepth(self.contract, isSmartDepth=self.smart_depth)

    def _on_update(self, ticker: Ticker) -> None:
        if ticker.domTicks:
            self._data_arrived()

    def snapshot(self) -> DepthData:
        ticker = self.ticker
        return DepthData(
            **self.status(),
            rows=self.rows,
            smart_depth=self.smart_depth,
            bids=_levels(ticker.domBidsDict, ticker.domBids) if ticker else [],
            asks=_levels(ticker.domAsksDict, ticker.domAsks) if ticker else [],
            time=ensure_utc(ticker.time) if ticker else None,
        )


class _TickByTickStream(_TickerStream):
    def __init__(
        self,
        ib: IB,
        contract: Contract,
        out: ContractOut,
        *,
        tick_type: TickByTickType,
        ignore_size: bool,
        buffer_size: int,
    ) -> None:
        super().__init__(ib, contract, out)
        self.request_key = tick_type
        self.tick_type: TickByTickType = tick_type
        self.ignore_size = ignore_size
        self.ticks: deque[TradeTick | BidAskTick | MidPointTick] = deque(maxlen=buffer_size)
        self.received = 0

    def _send(self) -> Ticker:
        return self.ib.reqTickByTickData(
            self.contract, self.tick_type, numberOfTicks=0, ignoreSize=self.ignore_size
        )

    def _stop(self) -> None:
        self.ib.cancelTickByTickData(self.contract, self.tick_type)

    def _on_update(self, ticker: Ticker) -> None:
        # ticker.tickByTicks holds this packet's ticks only (cleared on the next one), and
        # every tick-by-tick type of the contract lands in it.
        for raw in ticker.tickByTicks:
            tick = _convert_tick(raw, self.tick_type)
            if tick is not None:
                self.ticks.append(tick)
                self.received += 1
                self._data_arrived()

    def snapshot(self) -> TickByTickData:
        return TickByTickData(
            **self.status(),
            tick_type=self.tick_type,
            ignore_size=self.ignore_size,
            buffer_size=self.ticks.maxlen or 0,
            ticks=list(self.ticks),
            received=self.received,
        )


class _RealtimeBarsStream(_Stream):
    def __init__(
        self,
        ib: IB,
        contract: Contract,
        out: ContractOut,
        *,
        what_to_show: RealtimeWhatToShow,
        use_rth: bool,
        buffer_size: int,
    ) -> None:
        super().__init__(ib, contract, out)
        self.what_to_show: RealtimeWhatToShow = what_to_show
        self.use_rth = use_rth
        self.bars: deque[RealtimeBar] = deque(maxlen=buffer_size)
        self.source: RealTimeBarList | None = None

    def request(self) -> None:
        source = self.ib.reqRealTimeBars(
            self.contract, _REALTIME_BAR_SECONDS, self.what_to_show, self.use_rth
        )
        self._unwatch()
        self.source = source
        _update_event(source).connect(self._on_bars)
        self.req_id = clean_int(getattr(source, "reqId", None))

    def _unwatch(self) -> None:
        if self.source is not None:
            _update_event(self.source).disconnect(self._on_bars)

    def _on_bars(self, source: RealTimeBarList, _has_new_bar: bool = True) -> None:
        # ib_async appends every bar to its list forever; move them into the ring buffer.
        self.bars.extend(_realtime_bar(bar) for bar in source)
        source.clear()
        self._data_arrived()

    async def start(self, settle_seconds: float) -> None:
        self.request()
        await self.settle(settle_seconds)

    def resubscribe(self) -> None:
        old = self.source
        self._unwatch()
        if old is not None and any(item is old for item in self.ib.realtimeBars()):
            self.ib.cancelRealTimeBars(old)  # still registered: IBKR lost it (1101)
        self.source = None
        self._reset_status()
        self.request()

    def cancel(self) -> None:
        self._unwatch()
        self._stop_listening()
        if self.source is not None and self.ib.isConnected():
            self.ib.cancelRealTimeBars(self.source)
        self.source = None

    def snapshot(self) -> RealtimeBarsData:
        return RealtimeBarsData(
            **self.status(),
            what_to_show=self.what_to_show,
            use_rth=self.use_rth,
            buffer_size=self.bars.maxlen or 0,
            bars=list(self.bars),
        )


Call = Callable[..., Awaitable[Any]]


class _LiveBarsStream(_Stream):
    def __init__(
        self,
        ib: IB,
        contract: Contract,
        out: ContractOut,
        call: Call,
        *,
        bar_size: BarSize,
        duration: str,
        what_to_show: WhatToShow,
        use_rth: bool,
        slot: Callable[[], AbstractAsyncContextManager[None]] = contextlib.nullcontext,
        keep: int = BARS_KEPT_MAX,
    ) -> None:
        super().__init__(ib, contract, out)
        self._call = call
        self._slot = slot
        self._keep = keep
        self.bar_size: BarSize = bar_size
        self.duration = duration
        self.what_to_show: WhatToShow = what_to_show
        self.use_rth = use_rth
        self.source: BarDataList | None = None

    async def _backfill(self) -> BarDataList:
        """Request the bars, failing at once if IBKR rejects the request with an error
        ib_async treats as a warning (321, e.g. an invalid bar size): ib_async would
        otherwise wait for data that never comes."""
        # The backfill holds one of IBKR's 50 open historical request slots, shared with
        # the history tools, until it answers.
        async with self._slot():
            return await self._backfill_now()

    async def _backfill_now(self) -> BarDataList:
        # timeout=0: _call enforces the timeout; ib_async's own would cancel the request
        # at IBKR but leave the live subscription registered.
        answer = asyncio.ensure_future(
            self.ib.reqHistoricalDataAsync(
                self.contract,
                "",
                self.duration,
                self.bar_size,
                self.what_to_show,
                self.use_rth,
                formatDate=2,
                keepUpToDate=True,
                timeout=0,
            )
        )
        rejected = asyncio.ensure_future(self._settled.wait())
        try:
            await asyncio.wait({answer, rejected}, return_when=asyncio.FIRST_COMPLETED)
            if answer.done() or self.failure is None:
                return await answer
            raise self.failure
        finally:
            answer.cancel()  # no-op when it finished
            rejected.cancel()

    async def request(self) -> None:
        ib = self.ib
        before = {id(item) for item in ib.realtimeBars()}
        try:
            source = await self._call(
                self._backfill, what=f"{self.bar_size} bars for {_describe(self.contract)}"
            )
        except BaseException:
            self._drop_orphans(before)
            raise
        self._unwatch()
        self.source = source
        _update_event(source).connect(self._on_bars)
        self.req_id = clean_int(getattr(source, "reqId", None))

    def _drop_orphans(self, before: set[int]) -> None:
        """Cancel the live bar list a failed or timed-out request left registered."""
        ib = self.ib
        for item in ib.realtimeBars():
            if (
                id(item) not in before
                and isinstance(item, BarDataList)
                and getattr(item, "contract", None) is self.contract
                and ib.isConnected()
            ):
                ib.cancelHistoricalData(item)

    def _unwatch(self) -> None:
        if self.source is not None:
            _update_event(self.source).disconnect(self._on_bars)

    def _on_bars(self, source: BarDataList, _has_new_bar: bool = True) -> None:
        excess = len(source) - self._keep
        if excess > 0:
            del source[:excess]  # ib_async only reads the last bar
        self._data_arrived()

    async def start(self, settle_seconds: float) -> None:
        await self.request()
        if not self.source:
            raise NotFoundError(
                f"IBKR returned no {self.what_to_show} bars for {_describe(self.contract)} over "
                f"{self.duration} ({'regular hours' if self.use_rth else 'all hours'}), so the "
                "series cannot update. Use a longer duration or use_rth=false."
            )

    async def resubscribe(self) -> None:
        old = self.source
        self._unwatch()
        if old is not None and any(item is old for item in self.ib.realtimeBars()):
            self.ib.cancelHistoricalData(old)  # still registered: IBKR lost it (1101)
        self.source = None
        self._reset_status()
        await self.request()

    def cancel(self) -> None:
        self._unwatch()
        self._stop_listening()
        if self.source is not None and self.ib.isConnected():
            self.ib.cancelHistoricalData(self.source)
        self.source = None

    def snapshot(self) -> LiveBarsData:
        return LiveBarsData(
            **self.status(),
            bar_size=self.bar_size,
            duration=self.duration,
            what_to_show=self.what_to_show,
            use_rth=self.use_rth,
            bars=[_stream_bar(bar) for bar in self.source or []],
        )
