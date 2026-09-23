"""The market data service: quote snapshots, streams and subscription management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, get_args

from ib_async import Contract, Ticker
from pydantic import BaseModel

from ib_gateway_mcp._util import (
    clamp_limit,
    contract_to_out,
    ensure_utc,
    quote_from_ticker,
    subscription_out,
)
from ib_gateway_mcp.errors import (
    ConfigurationError,
    IbApiError,
    IbGatewayMcpError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    SubscriptionLimitError,
    SubscriptionNotFoundError,
)
from ib_gateway_mcp.models.common import (
    MARKET_DATA_TYPE_CODES,
    MARKET_DATA_TYPE_NAMES,
    ContractSpec,
    LiveBarSize,
    MarketDataTypeName,
    QuoteOut,
    RealtimeWhatToShow,
    SubscriptionDataOut,
    SubscriptionKind,
    SubscriptionOut,
    WhatToShow,
)
from ib_gateway_mcp.models.market_data import (
    CancelledSubscription,
    GenericTick,
    MarketDataTypeOut,
    QuoteError,
    QuoteList,
    SubscriptionEntry,
    SubscriptionList,
    TickByTickType,
    UnsubscribeResult,
)
from ib_gateway_mcp.safety.audit import AuditEvent
from ib_gateway_mcp.services.base import BaseService
from ib_gateway_mcp.services.market_data._constants import (
    _DELAYED_TYPES,
    _DURATION,
    _SNAPSHOT_HINTS,
    BARS_KEPT_MAX,
    DATA_LIMIT_DEFAULT,
    DATA_LIMIT_MAX,
    DEPTH_ROWS_MAX,
    MAX_QUOTE_CONTRACTS,
    REALTIME_BUFFER_MAX,
    TICK_BUFFER_MAX,
)
from ib_gateway_mcp.services.market_data._convert import (
    _cancelled,
    _checked_ticks,
    _contract_key,
    _describe,
    _params,
    _quote_error,
    _quote_notices,
    _window,
    _with_hint,
)
from ib_gateway_mcp.services.market_data._streams import (
    _DepthStream,
    _LiveBarsStream,
    _QuoteStream,
    _RealtimeBarsStream,
    _Stream,
    _TickByTickStream,
)
from ib_gateway_mcp.subscriptions import Stream

if TYPE_CHECKING:
    from ib_gateway_mcp.gateway import Gateway

logger = logging.getLogger(__name__)


# --- the service --------------------------------------------------------------------------


class MarketDataService(BaseService):
    """Quote snapshots and streaming quotes, depth, tick-by-tick data and bars.

    Streams are server-side subscriptions (see
    :class:`~ib_gateway_mcp.subscriptions.SubscriptionRegistry`): the ``subscribe_*``
    methods return a handle, :meth:`subscription_data` reads it, :meth:`unsubscribe`
    stops it. Every stream needs a qualified contract; the methods qualify the spec.

    Attributes:
        settle_seconds: How long a new stream waits for its first data or an IBKR error
            before returning.
        max_depth_streams: IBKR's default limit on simultaneous market depth streams.
        max_tick_by_tick_streams: IBKR's default limit on simultaneous tick-by-tick
            streams.
        bars_kept: Most bars a live bar series keeps (:data:`BARS_KEPT_MAX`).
    """

    settle_seconds: float = 1.0
    max_depth_streams: int = 3
    max_tick_by_tick_streams: int = 3
    bars_kept: int = BARS_KEPT_MAX

    def __init__(self, gateway: Gateway) -> None:
        super().__init__(gateway)
        self._quote_streams: dict[str, _QuoteStream] = {}

    # --- snapshots ---------------------------------------------------------------------

    async def quotes(
        self, contracts: Sequence[ContractSpec], *, regulatory_snapshot: bool = False
    ) -> QuoteList:
        """Take a top-of-book snapshot of 1 to :data:`MAX_QUOTE_CONTRACTS` contracts.

        A contract with an open quote stream is answered from the stream. Contracts that
        fail (unknown, ambiguous, not permissioned) are listed in ``errors``; when none
        succeeds, the first failure is raised.

        Args:
            contracts: The instruments, in the order the quotes are returned.
            regulatory_snapshot: Ask for a regulatory (NBBO) snapshot, which IBKR bills
                at about USD 0.01 each for US stocks without a live subscription. Refused
                unless ``IBKR_MCP_ALLOW_REGULATORY_SNAPSHOTS`` is on; each billed
                request is audited.

        Raises:
            ConfigurationError: A regulatory snapshot without the operator's opt-in.
            InvalidRequestError: No contracts, or too many.
            NotFoundError, AmbiguousContractError, IbApiError, RequestTimeoutError: When
                no contract got a quote.
        """
        if not contracts:
            raise InvalidRequestError("Give at least one contract.")
        if regulatory_snapshot and not self.settings.allow_regulatory_snapshots:
            raise ConfigurationError(
                "Regulatory snapshots cost money (IBKR bills about USD 0.01 each), so this "
                "server only sends them when its operator sets "
                "IBKR_MCP_ALLOW_REGULATORY_SNAPSHOTS=true. Ask without regulatory_snapshot."
            )
        if len(contracts) > MAX_QUOTE_CONTRACTS:
            raise InvalidRequestError(
                f"At most {MAX_QUOTE_CONTRACTS} contracts per call ({len(contracts)} given); "
                "each snapshot holds a market data line for up to about 11 seconds."
            )
        _ = self.ib  # fail fast, with the health hint, when not connected
        qualified = await asyncio.gather(
            *(self.qualify(spec) for spec in contracts), return_exceptions=True
        )
        answers, requested = await self._fetch_quotes(qualified, regulatory_snapshot)
        if regulatory_snapshot and requested:
            self.safety.audit.record(
                AuditEvent.REGULATORY_SNAPSHOT,
                count=requested,
                contracts=[_describe(c) for c in qualified if isinstance(c, Contract)],
            )
        quotes: list[QuoteOut] = []
        errors: list[QuoteError] = []
        failures: list[IbGatewayMcpError] = []
        empty: list[str] = []
        for spec, result in zip(contracts, qualified, strict=True):
            answer = answers[_contract_key(result)] if isinstance(result, Contract) else result
            if isinstance(answer, Ticker) and isinstance(result, Contract):
                quote = quote_from_ticker(answer, contract_to_out(result))
                quotes.append(quote)
                if all(price is None for price in (quote.bid, quote.ask, quote.last, quote.close)):
                    empty.append(_describe(result))
            elif isinstance(answer, IbGatewayMcpError):
                failures.append(answer)
                errors.append(_quote_error(spec, answer))
            else:
                raise answer if isinstance(answer, BaseException) else TypeError(answer)
        if not quotes and failures:
            raise failures[0]
        return QuoteList(
            quotes=quotes,
            errors=errors,
            notices=_quote_notices(quotes, empty, requested if regulatory_snapshot else 0),
            regulatory_snapshots=requested if regulatory_snapshot else 0,
        )

    async def _fetch_quotes(
        self, qualified: Sequence[Contract | BaseException], regulatory: bool
    ) -> tuple[dict[str, Ticker | BaseException], int]:
        """Snapshot each distinct contract once, or read its open quote stream.

        Returns the answers by contract key and how many snapshots were requested.
        """
        live: dict[str, Ticker] = {}
        wanted: dict[str, Contract] = {}
        for result in qualified:
            if not isinstance(result, Contract):
                continue
            key = _contract_key(result)
            if key in live or key in wanted:
                continue
            ticker = self._live_quote(key)
            if ticker is not None:
                live[key] = ticker
            else:
                wanted[key] = result
        fetched = await asyncio.gather(
            *(self._snapshot(contract, regulatory) for contract in wanted.values()),
            return_exceptions=True,
        )
        answers: dict[str, Ticker | BaseException] = dict(live)
        answers.update(zip(wanted, fetched, strict=True))
        return answers, len(wanted)

    def _live_quote(self, key: str) -> Ticker | None:
        """The ticker of a healthy, flowing quote stream for ``key``, if there is one."""
        stream = self._quote_streams.get(key)
        if stream is None or stream.ticker is None or stream.error is not None:
            return None
        if not stream.updates:
            return None
        info = self.subs.find("quotes", key)
        if info is None or info.stale:
            return None
        return stream.ticker

    async def _snapshot(self, contract: Contract, regulatory: bool) -> Ticker:
        ib = self.ib
        try:
            tickers = await self._call(
                ib.reqTickersAsync(contract, regulatorySnapshot=regulatory),
                what=f"a quote snapshot for {_describe(contract)}",
            )
        except IbApiError as exc:
            hinted = _with_hint(exc, _SNAPSHOT_HINTS)
            if hinted is exc:
                raise
            raise hinted from exc
        if not tickers:
            raise NotFoundError(f"IBKR returned no quote for {_describe(contract)}.")
        return tickers[0]

    def set_market_data_type(self, data_type: MarketDataTypeName) -> MarketDataTypeOut:
        """Switch the connection between live, frozen, delayed and delayed-frozen data.

        Applies to every later market data request, and again after a reconnect.

        Raises:
            InvalidRequestError: Unknown ``data_type``.
        """
        code = MARKET_DATA_TYPE_CODES.get(data_type)
        if code is None:
            raise InvalidRequestError(
                f"data_type must be one of {', '.join(MARKET_DATA_TYPE_CODES)}, not {data_type!r}"
            )
        previous = MARKET_DATA_TYPE_NAMES.get(self.connection.market_data_type)
        self.connection.set_market_data_type(code)
        note = (
            "Applies to market data requested from now on; open streams keep what they "
            "had until you unsubscribe and subscribe again."
        )
        if code in _DELAYED_TYPES:
            note += " Delayed data has no market depth and no tick-by-tick data."
        return MarketDataTypeOut(data_type=data_type, code=code, previous=previous, note=note)

    # --- streams -----------------------------------------------------------------------

    async def subscribe_quotes(
        self, contract: ContractSpec, generic_ticks: Iterable[GenericTick] = ()
    ) -> SubscriptionOut:
        """Stream top-of-book quotes, plus optional generic ticks, for one contract.

        One stream per contract: subscribing again returns the same handle, re-requesting
        it with the union of generic ticks if new ones are asked for.

        Raises:
            InvalidRequestError: An unknown generic tick name.
            SubscriptionLimitError: No subscription slot, or IBKR's line limit (101).
            IbApiError: IBKR refused the stream (e.g. 354/10089/10168: no market data
                permission; the message says how to get delayed data).
        """
        ticks = _checked_ticks(generic_ticks)
        ib = self.ib
        qualified = await self.qualify(contract)
        out = contract_to_out(qualified)
        key = _contract_key(qualified)

        async def opener() -> Stream:
            stream = _QuoteStream(ib, qualified, out, ticks)
            handle = await self._open(stream)
            self._quote_streams[key] = stream

            def cancel() -> None:
                if self._quote_streams.get(key) is stream:
                    del self._quote_streams[key]
                stream.cancel()

            return Stream(cancel=cancel, snapshot=handle.snapshot, resubscribe=handle.resubscribe)

        result = await self._subscribe(
            "quotes", key, opener=opener, contract=out, meta={"generic_ticks": list(ticks)}
        )
        if result.deduplicated and ticks:
            await self._widen_quotes(key, ticks)
        return result

    async def _widen_quotes(self, key: str, ticks: Sequence[GenericTick]) -> None:
        stream = self._quote_streams.get(key)
        info = self.subs.find("quotes", key)
        if stream is None or info is None:
            return
        try:
            widened = await stream.widen(ticks, self.settle_seconds)
        except IbApiError as exc:
            hinted = _with_hint(exc)
            if hinted is exc:
                raise
            raise hinted from exc
        if widened:
            info.meta["generic_ticks"] = list(stream.generic_ticks)

    async def subscribe_market_depth(
        self, contract: ContractSpec, *, rows: int = 10, smart_depth: bool = False
    ) -> SubscriptionOut:
        """Stream the order book (Level II) of one contract.

        One depth stream per contract; subscribing again returns the same handle (with
        its original ``rows`` and ``smart_depth``). Needs live data and Level II
        permissions.

        Raises:
            InvalidRequestError: ``rows`` out of range, a combo, or delayed data selected.
            SubscriptionLimitError: :attr:`max_depth_streams` reached (or IBKR's 309).
        """
        if not 1 <= rows <= DEPTH_ROWS_MAX:
            raise InvalidRequestError(f"rows must be 1-{DEPTH_ROWS_MAX}, not {rows}.")
        self._require_live_data("Market depth")
        ib = self.ib
        qualified = await self._qualify_single(contract, "Market depth")
        out = contract_to_out(qualified)

        async def opener() -> Stream:
            self._check_kind_cap("depth", self.max_depth_streams, "market depth")
            return await self._open(_DepthStream(ib, qualified, out, rows, smart_depth))

        return await self._subscribe(
            "depth",
            _contract_key(qualified),
            opener=opener,
            contract=out,
            meta={"rows": rows, "smart_depth": smart_depth},
        )

    async def subscribe_tick_by_tick(
        self,
        contract: ContractSpec,
        tick_type: TickByTickType,
        *,
        ignore_size: bool = False,
        buffer_size: int = 500,
    ) -> SubscriptionOut:
        """Stream every trade, quote change or midpoint of one contract into a ring buffer.

        One stream per contract and tick type; subscribing again returns the same handle.

        Raises:
            InvalidRequestError: Bad ``tick_type``/``buffer_size``, a combo, or delayed data.
            SubscriptionLimitError: :attr:`max_tick_by_tick_streams` reached (or 10190).
        """
        if tick_type not in ("Last", "AllLast", "BidAsk", "MidPoint"):
            raise InvalidRequestError(
                f"tick_type must be Last, AllLast, BidAsk or MidPoint, not {tick_type!r}."
            )
        if not 1 <= buffer_size <= TICK_BUFFER_MAX:
            raise InvalidRequestError(f"buffer_size must be 1-{TICK_BUFFER_MAX}.")
        self._require_live_data("Tick-by-tick data")
        ib = self.ib
        qualified = await self._qualify_single(contract, "Tick-by-tick data")
        out = contract_to_out(qualified)

        async def opener() -> Stream:
            self._check_kind_cap("tick_by_tick", self.max_tick_by_tick_streams, "tick-by-tick")
            return await self._open(
                _TickByTickStream(
                    ib,
                    qualified,
                    out,
                    tick_type=tick_type,
                    ignore_size=ignore_size,
                    buffer_size=buffer_size,
                )
            )

        return await self._subscribe(
            "tick_by_tick",
            f"{_contract_key(qualified)}:{tick_type}",
            opener=opener,
            contract=out,
            meta={"tick_type": tick_type, "ignore_size": ignore_size, "buffer_size": buffer_size},
        )

    async def subscribe_realtime_bars(
        self,
        contract: ContractSpec,
        *,
        what_to_show: RealtimeWhatToShow = "TRADES",
        use_rth: bool = False,
        buffer_size: int = 720,
    ) -> SubscriptionOut:
        """Stream 5-second OHLCV bars into a ring buffer.

        Raises:
            InvalidRequestError: Bad ``what_to_show`` or ``buffer_size``.
            IbApiError: IBKR refused the stream (420, 162, missing permissions...).
        """
        if what_to_show not in ("TRADES", "MIDPOINT", "BID", "ASK"):
            raise InvalidRequestError(
                f"what_to_show must be TRADES, MIDPOINT, BID or ASK, not {what_to_show!r}."
            )
        if not 1 <= buffer_size <= REALTIME_BUFFER_MAX:
            raise InvalidRequestError(f"buffer_size must be 1-{REALTIME_BUFFER_MAX}.")
        ib = self.ib
        qualified = await self.qualify(contract)
        out = contract_to_out(qualified)

        async def opener() -> Stream:
            # Real-time bars count against IBKR's historical pacing, like the history tools.
            self.gateway.pacing.admit_bars(
                qualified,
                what_to_show=what_to_show,
                subject=f"5-second {what_to_show} bars for {_describe(qualified)}",
            )
            return await self._open(
                _RealtimeBarsStream(
                    ib,
                    qualified,
                    out,
                    what_to_show=what_to_show,
                    use_rth=use_rth,
                    buffer_size=buffer_size,
                )
            )

        session = "rth" if use_rth else "all"
        return await self._subscribe(
            "realtime_bars",
            f"{_contract_key(qualified)}:{what_to_show}:{session}",
            opener=opener,
            contract=out,
            meta={"what_to_show": what_to_show, "use_rth": use_rth, "buffer_size": buffer_size},
        )

    async def subscribe_bars(
        self,
        contract: ContractSpec,
        bar_size: LiveBarSize,
        *,
        duration: str = "1 D",
        what_to_show: WhatToShow = "TRADES",
        use_rth: bool = True,
    ) -> SubscriptionOut:
        """Backfill ``duration`` of bars and keep the last one updating live.

        Raises:
            InvalidRequestError: Unknown ``bar_size`` or ``what_to_show``, a bar size
                under 5 seconds, or a malformed ``duration``.
            NotFoundError: The backfill came back empty (ib_async could not update it).
            IbApiError, RequestTimeoutError: IBKR refused or did not answer the backfill.
        """
        if bar_size not in get_args(LiveBarSize):
            raise InvalidRequestError(
                "Live-updating bars need a bar_size of 5 secs or more, one of "
                f"{', '.join(get_args(LiveBarSize))}; not {bar_size!r}."
            )
        if what_to_show not in get_args(WhatToShow):
            raise InvalidRequestError(
                f"what_to_show must be one of {', '.join(get_args(WhatToShow))}, "
                f"not {what_to_show!r}."
            )
        duration = " ".join(duration.split()).upper()
        if not _DURATION.match(duration):
            raise InvalidRequestError(
                f"duration must look like '3600 S', '1 D', '2 W', '6 M' or '1 Y', not {duration!r}."
            )
        ib = self.ib
        qualified = await self.qualify(contract)
        out = contract_to_out(qualified)

        pacing = self.gateway.pacing

        async def opener() -> Stream:
            # The backfill is a historical request: small bars share IBKR's pacing budget
            # with the history tools, and every backfill takes an open-request slot.
            pacing.admit_bars(
                qualified,
                what_to_show=what_to_show,
                bar_size=bar_size,
                subject=f"{bar_size} {what_to_show} bars for {_describe(qualified)}",
            )
            return await self._open(
                _LiveBarsStream(
                    ib,
                    qualified,
                    out,
                    self._call,
                    bar_size=bar_size,
                    duration=duration,
                    what_to_show=what_to_show,
                    use_rth=use_rth,
                    slot=pacing.slot,
                    keep=self.bars_kept,
                )
            )

        session = "rth" if use_rth else "all"
        # slow_open: the backfill can take up to IB_REQUEST_TIMEOUT; other subscribe
        # calls must not wait behind it.
        return await self._subscribe(
            "bars",
            f"{_contract_key(qualified)}:{bar_size}:{duration}:{what_to_show}:{session}",
            opener=opener,
            contract=out,
            meta={
                "bar_size": bar_size,
                "duration": duration,
                "what_to_show": what_to_show,
                "use_rth": use_rth,
            },
            slow_open=True,
        )

    # --- subscription management -------------------------------------------------------

    def list_subscriptions(self) -> SubscriptionList:
        """List every open subscription (all kinds, from every toolset) and the capacity."""
        ttl = float(self.settings.subscription_idle_ttl)
        infos = self.subs.list()
        entries: list[SubscriptionEntry] = []
        for info in infos:
            base = subscription_out(info, idle_ttl_s=ttl)
            entries.append(
                SubscriptionEntry(
                    subscription_id=base.subscription_id,
                    kind=base.kind,
                    key=base.key,
                    contract=base.contract,
                    created_at=base.created_at,
                    last_read_at=info.last_read_at,
                    idle_expires_at=(info.last_read_at or info.created_at) + timedelta(seconds=ttl),
                    stale=info.stale,
                    params=_params(info.meta),
                )
            )
        kinds = Counter(info.kind for info in infos)
        return SubscriptionList(
            subscriptions=entries,
            used=len(infos),
            max=self.settings.max_subscriptions,
            depth_used=kinds["depth"],
            depth_max=self.max_depth_streams,
            tick_by_tick_used=kinds["tick_by_tick"],
            tick_by_tick_max=self.max_tick_by_tick_streams,
            idle_ttl_s=ttl,
            market_data_type=MARKET_DATA_TYPE_NAMES.get(self.connection.market_data_type),
        )

    def subscription_data(
        self,
        subscription_id: str,
        *,
        limit: int | None = None,
        since: datetime | None = None,
    ) -> SubscriptionDataOut:
        """Read the latest state of any subscription (counts as a read for the idle reaper).

        Time series in the snapshot (ticks, bars, headlines: lists whose items carry a
        ``time``) are cut to items after ``since`` and to the newest ``limit`` items
        (default :data:`DATA_LIMIT_DEFAULT`, cap :data:`DATA_LIMIT_MAX`), oldest first;
        ``data["truncated"]`` is then true.

        Raises:
            SubscriptionNotFoundError: No such subscription (expired or cancelled).
        """
        out = self._subscription_data(subscription_id)
        count = clamp_limit(limit, default=DATA_LIMIT_DEFAULT, maximum=DATA_LIMIT_MAX)
        if _window(out.data, limit=count, since=ensure_utc(since)):
            out.data["truncated"] = True
        return out

    def snapshot[M: BaseModel](self, subscription_id: str, model: type[M]) -> M:
        """The latest state of a subscription as its own model (counts as a read).

        ``model`` is the snapshot model of the subscription's kind, e.g.
        :class:`~ib_gateway_mcp.models.market_data.QuoteStreamData` for ``quotes``,
        ``DepthData`` for ``depth``, ``LiveBarsData`` for ``bars``. Unlike
        :meth:`subscription_data` nothing is windowed: every buffered item is there.

        Raises:
            SubscriptionNotFoundError: No such subscription (expired or cancelled).
            InvalidRequestError: The subscription's snapshot is another model.
        """
        info = self.subs.get(subscription_id)
        data = self.subs.latest(subscription_id)
        if not isinstance(data, model):
            raise InvalidRequestError(
                f"Subscription {subscription_id} streams {info.kind}, whose snapshot is a "
                f"{type(data).__name__}, not a {model.__name__}."
            )
        return data

    async def stream(
        self,
        subscription_id: str,
        *,
        interval: float = 1.0,
        only_changes: bool = True,
    ) -> AsyncGenerator[SubscriptionDataOut]:
        """Yield a subscription's data as it changes, until the subscription ends.

        A polling helper for library callers (MCP clients poll ``get_subscription_data``
        instead). ``data`` is the snapshot as JSON; :meth:`watch` yields the typed model.
        Every ``interval`` seconds the snapshot is read (each read keeps the subscription
        from being reaped as idle) and yielded when it differs from the previous one
        (always, with ``only_changes=False``). The iteration ends when the subscription
        is cancelled or reaped. Leaving the loop early keeps the subscription open:
        call :meth:`unsubscribe` to stop it at IBKR.

        Raises:
            InvalidRequestError: ``interval`` is not positive.
            SubscriptionNotFoundError: No such subscription when the iteration starts.
        """
        polled = _poll(
            lambda: self._subscription_data(subscription_id),
            interval=interval,
            only_changes=only_changes,
            state=lambda out: (out.stale, out.data),
        )
        async for out in polled:
            yield out

    async def watch[M: BaseModel](
        self,
        subscription_id: str,
        model: type[M],
        *,
        interval: float = 1.0,
        only_changes: bool = True,
    ) -> AsyncGenerator[M]:
        """:meth:`stream`, yielding the subscription's own snapshot model (see :meth:`snapshot`)::

            sub = await gw.market_data.subscribe_quotes(ContractSpec(symbol="AAPL"))
            async for data in gw.market_data.watch(sub.subscription_id, QuoteStreamData):
                print(data.quote.last)

        Raises:
            InvalidRequestError: ``interval`` is not positive, or the subscription's
                snapshot is another model.
            SubscriptionNotFoundError: No such subscription when the iteration starts.
        """
        polled = _poll(
            lambda: self.snapshot(subscription_id, model),
            interval=interval,
            only_changes=only_changes,
            state=lambda data: data,
        )
        async for data in polled:
            yield data

    async def unsubscribe(
        self, subscription_id: str | None = None, *, all_subscriptions: bool = False
    ) -> UnsubscribeResult:
        """Cancel one subscription (any kind), or every one, at IBKR and forget it.

        Args:
            subscription_id: The subscription to cancel.
            all_subscriptions: Cancel every subscription instead.

        Raises:
            InvalidRequestError: Neither or both of the arguments were given.
            SubscriptionNotFoundError: No such subscription.
        """
        if all_subscriptions and not subscription_id:
            return await self.unsubscribe_all()
        if not subscription_id or all_subscriptions:
            raise InvalidRequestError(
                "Pass either subscription_id (list_subscriptions shows them) or all=true."
            )
        info = await self.subs.remove(subscription_id)
        return UnsubscribeResult(cancelled=[_cancelled(info)], remaining=len(self.subs))

    async def unsubscribe_all(self) -> UnsubscribeResult:
        """Cancel every subscription, of every kind."""
        cancelled: list[CancelledSubscription] = []
        for info in self.subs.list():
            with contextlib.suppress(SubscriptionNotFoundError):
                cancelled.append(_cancelled(await self.subs.remove(info.id)))
        return UnsubscribeResult(cancelled=cancelled, remaining=len(self.subs))

    # --- internals ---------------------------------------------------------------------

    async def _open(self, stream: _Stream) -> Stream:
        """Start ``stream``; on any failure stop it at IBKR and raise a clear error."""
        try:
            await stream.start(self.settle_seconds)
        except BaseException as exc:
            try:
                stream.cancel()
            except Exception:
                logger.exception("Cleaning up a failed %s stream failed", type(stream).__name__)
            if isinstance(exc, IbApiError):
                hinted = _with_hint(exc)
                if hinted is not exc:
                    raise hinted from exc
            elif isinstance(exc, ConnectionError):
                raise NotConnectedError(
                    f"Lost the gateway connection while opening the stream: {exc}. "
                    "Check get_health."
                ) from exc
            raise

        async def resubscribe() -> None:
            # The registry forgets a stream whose resubscribe fails without cancelling
            # it, so release its listeners and anything half-opened here.
            try:
                result = stream.resubscribe()
                if result is not None:
                    await result
            except BaseException:
                try:
                    stream.cancel()
                except Exception:
                    logger.exception("Cleaning up a %s stream failed", type(stream).__name__)
                raise

        return Stream(cancel=stream.cancel, snapshot=stream.snapshot, resubscribe=resubscribe)

    async def _qualify_single(self, spec: ContractSpec, what: str) -> Contract:
        if spec.sec_type == "BAG":
            raise InvalidRequestError(f"{what} is not available for combos (BAG).")
        return await self.qualify(spec)

    def _require_live_data(self, what: str) -> None:
        code = self.connection.market_data_type
        if code in _DELAYED_TYPES:
            name = MARKET_DATA_TYPE_NAMES.get(code, str(code))
            raise InvalidRequestError(
                f"{what} needs live market data, but the connection requests {name} data "
                "(IBKR sends none of it delayed). Call set_market_data_type with 'live' first; "
                "that needs a market data subscription for the instrument."
            )

    def _check_kind_cap(self, kind: SubscriptionKind, cap: int, what: str) -> None:
        used = sum(1 for info in self.subs.list() if info.kind == kind)
        if used >= cap:
            raise SubscriptionLimitError(
                f"{used} {what} streams are open and IBKR allows {cap} at a time by default. "
                "Unsubscribe from one first; list_subscriptions shows them."
            )


async def _poll[T](
    read: Callable[[], T],
    *,
    interval: float,
    only_changes: bool,
    state: Callable[[T], object],
) -> AsyncIterator[T]:
    """Call ``read`` every ``interval`` seconds and yield what it returns (only changes,
    by ``state``, when ``only_changes``), until the subscription is gone."""
    if not interval > 0:
        raise InvalidRequestError("interval must be a positive number of seconds.")
    previous: object = None
    started = False
    while True:
        try:
            value = read()
        except SubscriptionNotFoundError:
            if not started:
                raise
            return
        current = state(value)
        if not only_changes or not started or current != previous:
            previous = current
            yield value
        started = True
        await asyncio.sleep(interval)
