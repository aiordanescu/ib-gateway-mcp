"""Server-held registry of streaming subscriptions.

MCP tools are request/response, but much of the TWS API streams: quotes, depth,
tick-by-tick data, real-time bars, scanner results, bulletins. A service hands
:meth:`SubscriptionRegistry.add` an *opener*: a callable that opens the stream at IBKR
and returns a :class:`Stream` with three callables:

* ``cancel()`` stops the stream at IBKR,
* ``snapshot()`` renders the current state (usually from a bounded
  ``collections.deque`` ring buffer) as a pydantic model,
* ``resubscribe()`` (optional) re-requests the stream after IBKR lost it (error 1101)
  or after a reconnect. A stream without one is dropped when that happens.

The registry calls the opener only when ``(kind, key)`` is not already open and a slot
is free, so a refused or duplicate request never reaches IBKR. That order matters:
ib_async keys tickers (``reqMktData``, ``reqMktDepth``, ``reqTickByTickData``) by
contract, so opening a second stream for the same contract and cancelling it would
also cut the first one loose. For those streams the key must identify the contract
(its ``conId``), one stream per contract and kind.

The model then polls with the ``get_subscription_data`` tool. Subscriptions are capped
at ``IBKR_MCP_MAX_SUBSCRIPTIONS``, deduplicated by ``(kind, key)``, and cancelled after
``IBKR_MCP_SUBSCRIPTION_IDLE_TTL`` seconds without a read, so a forgetful model cannot
exhaust the account's market-data lines. While the gateway connection is down, every
subscription is marked ``stale``; snapshots should surface that flag.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from ib_gateway_mcp._util import utc_now
from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import SubscriptionLimitError, SubscriptionNotFoundError

__all__ = [
    "CancelFn",
    "OpenFn",
    "ResubscribeFn",
    "SnapshotFn",
    "Stream",
    "SubscriptionInfo",
    "SubscriptionRegistry",
]

logger = logging.getLogger(__name__)

CancelFn = Callable[[], Awaitable[None] | None]
"""Stops a stream at IBKR. May be sync or async."""
SnapshotFn = Callable[[], BaseModel]
"""Renders the stream's current state. Must be cheap and must not block."""
ResubscribeFn = Callable[[], Awaitable[None] | None]
"""Re-requests a stream IBKR dropped. May be sync or async."""


@dataclass(frozen=True)
class Stream:
    """An open stream, as an opener returns it to the registry."""

    cancel: CancelFn
    snapshot: SnapshotFn
    resubscribe: ResubscribeFn | None = None


OpenFn = Callable[[], Awaitable[Stream] | Stream]
"""Opens a stream at IBKR and returns its :class:`Stream`. May be sync or async."""

_MIN_REAP_INTERVAL = 1.0
_MAX_REAP_INTERVAL = 60.0


class SubscriptionInfo(BaseModel):
    """A live subscription, as shown to callers."""

    id: str = Field(description="Handle for the get_subscription_data and unsubscribe tools.")
    kind: str = Field(description="What streams: quotes, depth, tick_by_tick, bars, ...")
    key: str = Field(description="What it streams for, e.g. a contract id plus parameters.")
    created_at: datetime
    last_read_at: datetime | None = None
    stale: bool = Field(
        False,
        description=(
            "True while the stream is not flowing: the gateway connection dropped or IBKR "
            "lost market data, and it has not been re-requested yet. Values may be old."
        ),
    )
    meta: dict[str, Any] = Field(default_factory=dict)


@dataclass
class _Entry:
    info: SubscriptionInfo
    cancel: CancelFn
    snapshot: SnapshotFn
    resubscribe: ResubscribeFn | None


async def _maybe_await(result: Awaitable[None] | None) -> None:
    if inspect.isawaitable(result):
        await result


class SubscriptionRegistry:
    """Tracks open subscriptions, enforces the cap and reaps idle ones.

    Args:
        settings: Supplies ``max_subscriptions`` and ``subscription_idle_ttl``.
        clock: Returns the current UTC time; injectable for tests.
    """

    def __init__(self, settings: Settings, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._max = settings.max_subscriptions
        self._idle_ttl = timedelta(seconds=settings.subscription_idle_ttl)
        self._resubscribe_timeout = settings.request_timeout
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self._ids = itertools.count(1)
        self._reaper: asyncio.Task[None] | None = None
        self._open_lock = asyncio.Lock()
        self._opening: dict[tuple[str, str], asyncio.Future[None]] = {}
        """Slow opens in progress (``add(slow_open=True)``): each holds a slot."""
        self._resubscribing = False
        self._rerun = False

    # --- lifecycle ------------------------------------------------------------------------

    def start(self) -> None:
        """Start the idle reaper. Idempotent."""
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_forever(), name="subscription-reaper")

    async def stop(self) -> None:
        """Stop the reaper and cancel every subscription."""
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        await self.cancel_all()

    # --- registration ---------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def find(self, kind: str, key: str) -> SubscriptionInfo | None:
        """Return the live subscription for ``(kind, key)``, if any."""
        sub_id = self._by_key.get((kind, key))
        return self._entries[sub_id].info if sub_id else None

    def ensure_capacity(self) -> None:
        """Raise :class:`SubscriptionLimitError` if no subscription slot is free.

        :meth:`add` checks this before calling the opener, so a refused request costs
        nothing at IBKR.
        """
        used = len(self._entries) + len(self._opening)
        if used >= self._max:
            raise SubscriptionLimitError(
                f"{used} subscriptions are open or opening, the limit is {self._max} "
                "(IBKR_MCP_MAX_SUBSCRIPTIONS). Unsubscribe from some first; "
                "list_subscriptions shows them."
            )

    async def add(
        self,
        kind: str,
        key: str,
        *,
        opener: OpenFn,
        meta: Mapping[str, Any] | None = None,
        slow_open: bool = False,
    ) -> SubscriptionInfo:
        """Return the subscription for ``(kind, key)``, opening the stream only if needed.

        If ``(kind, key)`` is already open, its handle is returned and ``opener`` is not
        called, so IBKR never carries duplicates. Otherwise the cap is checked and then
        ``opener`` opens the stream. Opens are serialized, so concurrent calls cannot
        race past the cap or open the same key twice. Resolve contracts before calling;
        the opener should only issue the ib_async request and build the :class:`Stream`.

        Args:
            kind: The subscription kind.
            key: What it streams for (for ticker streams, starting with the conId).
            opener: Opens the stream at IBKR.
            meta: Extra facts kept with the entry.
            slow_open: The opener waits on IBKR for a while (a historical backfill). It
                then runs outside the registry's open lock, holding a reserved slot, so
                other subscribe calls are not blocked meanwhile; concurrent calls for
                the same ``(kind, key)`` still wait for it and get its handle. Openers
                that check per-kind limits against :meth:`list` must not use it.

        Raises:
            SubscriptionLimitError: No slot is free; nothing was opened.
        """
        while True:
            async with self._open_lock:
                existing = self.find(kind, key)
                if existing is not None:
                    return existing
                in_progress = self._opening.get((kind, key))
                if in_progress is None:
                    self.ensure_capacity()
                    if not slow_open:
                        stream = await self._run(opener)
                        return self._register(kind, key, stream, meta)
                    reserved = asyncio.get_running_loop().create_future()
                    self._opening[(kind, key)] = reserved
                    break
            # Another call is opening this key: wait for it, then look again.
            await asyncio.wait([in_progress])
        try:
            stream = await self._run(opener)
            return self._register(kind, key, stream, meta)
        finally:
            del self._opening[(kind, key)]
            reserved.set_result(None)

    @staticmethod
    async def _run(opener: OpenFn) -> Stream:
        opened = opener()
        return await opened if inspect.isawaitable(opened) else opened

    def _register(
        self, kind: str, key: str, stream: Stream, meta: Mapping[str, Any] | None
    ) -> SubscriptionInfo:
        sub_id = f"{kind}-{next(self._ids)}"
        info = SubscriptionInfo(
            id=sub_id, kind=kind, key=key, created_at=self._clock(), meta=dict(meta or {})
        )
        self._entries[sub_id] = _Entry(info, stream.cancel, stream.snapshot, stream.resubscribe)
        self._by_key[(kind, key)] = sub_id
        logger.debug("Subscription %s opened for %s", sub_id, key)
        return info

    # --- access ---------------------------------------------------------------------------

    def _entry(self, sub_id: str) -> _Entry:
        try:
            return self._entries[sub_id]
        except KeyError:
            raise SubscriptionNotFoundError(
                f"No subscription {sub_id!r}. It may have been cancelled after "
                f"{self._idle_ttl.total_seconds():.0f}s without a read; subscribe again."
            ) from None

    def get(self, sub_id: str) -> SubscriptionInfo:
        """Return a subscription's handle without counting it as a read."""
        return self._entry(sub_id).info

    def latest(self, sub_id: str) -> BaseModel:
        """Return the subscription's current snapshot and mark it as read."""
        entry = self._entry(sub_id)
        snapshot = entry.snapshot()
        entry.info.last_read_at = self._clock()
        return snapshot

    # --- teardown -------------------------------------------------------------------------

    async def remove(self, sub_id: str) -> SubscriptionInfo:
        """Cancel a subscription at IBKR and forget it."""
        entry = self._forget(sub_id)
        await self._safe_cancel(entry.cancel, sub_id)
        return entry.info

    def _forget(self, sub_id: str) -> _Entry:
        entry = self._entry(sub_id)
        del self._entries[sub_id]
        self._by_key.pop((entry.info.kind, entry.info.key), None)
        return entry

    async def cancel_all(self) -> int:
        """Cancel every subscription; return how many there were."""
        ids = list(self._entries)
        for sub_id in ids:
            await self.remove(sub_id)
        return len(ids)

    def mark_all_stale(self) -> None:
        """Flag every subscription as stale (the gateway connection dropped)."""
        for entry in self._entries.values():
            entry.info.stale = True

    async def resubscribe_all(self) -> int:
        """Re-request every stream after IBKR lost it; drop the ones that cannot be.

        Called after IBKR reports lost market data (error 1101) and after a reconnect.
        By then the old streams are gone at IBKR (and, after a reconnect, from ib_async
        too), so a subscription without a ``resubscribe`` callable, or whose resubscribe
        fails or takes longer than ``IB_REQUEST_TIMEOUT``, is forgotten rather than left
        frozen; ``get_subscription_data`` then reports it as not found. Returns how many
        streams were re-requested.

        Runs are never concurrent: a call while one is in progress (repeated 1101s from
        a flapping link, or a 1101 during the pass after a reconnect) returns 0 at once
        and makes the running call do one more pass when it finishes. Two overlapping
        passes would each re-request the same stream, and the request the second one
        replaces could then never be cancelled.
        """
        if self._resubscribing:
            self._rerun = True
            return 0
        self._resubscribing = True
        try:
            count = 0
            while True:
                self._rerun = False
                count = await self._resubscribe_pass()
                if not self._rerun:
                    return count
        finally:
            self._resubscribing = False

    async def _resubscribe_pass(self) -> int:
        count = 0
        for sub_id, entry in list(self._entries.items()):
            if self._entries.get(sub_id) is not entry:
                continue  # removed meanwhile
            entry.info.stale = True
            if entry.resubscribe is None:
                self._forget(sub_id)
                logger.warning("Subscription %s cannot be re-requested; dropped it", sub_id)
                continue
            try:
                await asyncio.wait_for(_maybe_await(entry.resubscribe()), self._resubscribe_timeout)
            except Exception:
                logger.exception("Resubscribing %s failed; dropped it", sub_id)
                if self._entries.get(sub_id) is entry:
                    self._forget(sub_id)
            else:
                entry.info.stale = False
                count += 1
        if count:
            logger.info("Re-requested %d subscription(s)", count)
        return count

    async def reap_idle(self) -> list[str]:
        """Cancel subscriptions not read within the idle TTL; return their ids."""
        cutoff = self._clock() - self._idle_ttl
        idle = [
            sub_id
            for sub_id, entry in self._entries.items()
            if (entry.info.last_read_at or entry.info.created_at) < cutoff
        ]
        for sub_id in idle:
            if sub_id in self._entries:
                await self.remove(sub_id)
                logger.info("Subscription %s cancelled after being idle", sub_id)
        return idle

    async def _reap_forever(self) -> None:
        interval = min(
            max(self._idle_ttl.total_seconds() / 4, _MIN_REAP_INTERVAL), _MAX_REAP_INTERVAL
        )
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap_idle()
            except Exception:
                logger.exception("Idle subscription reaper failed")

    @staticmethod
    async def _safe_cancel(cancel: CancelFn, label: str) -> None:
        try:
            await _maybe_await(cancel())
        except Exception:
            logger.exception("Cancelling subscription %s failed", label)

    # Defined last so the method name does not shadow the builtin in annotations above.
    def list(self) -> list[SubscriptionInfo]:
        """Return every live subscription, oldest first."""
        return [entry.info for entry in self._entries.values()]
