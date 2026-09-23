"""SubscriptionRegistry: handles, dedup, caps, idle reaping and resubscription."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from ib_async import IB, Stock
from pydantic import BaseModel

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import SubscriptionLimitError, SubscriptionNotFoundError
from ib_gateway_mcp.subscriptions import (
    CancelFn,
    ResubscribeFn,
    SnapshotFn,
    Stream,
    SubscriptionInfo,
    SubscriptionRegistry,
)


class Snapshot(BaseModel):
    value: int


class Clock:
    """A settable clock for idle-TTL tests."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def registry(settings_factory: Callable[..., Settings], clock: Clock) -> SubscriptionRegistry:
    settings = settings_factory(max_subscriptions=2, subscription_idle_ttl=60)
    return SubscriptionRegistry(settings, clock=clock)


async def add(
    registry: SubscriptionRegistry,
    kind: str,
    key: str,
    *,
    cancel: CancelFn | None = None,
    snapshot: SnapshotFn | None = None,
    resubscribe: ResubscribeFn | None = None,
    meta: dict[str, object] | None = None,
) -> SubscriptionInfo:
    """Register a stream through a synchronous opener."""
    stream = Stream(cancel or MagicMock(), snapshot or MagicMock(), resubscribe)
    return await registry.add(kind, key, opener=lambda: stream, meta=meta)


async def test_add_get_latest_list(registry: SubscriptionRegistry, clock: Clock) -> None:
    info = await add(registry, "quote", "265598", snapshot=lambda: Snapshot(value=1), meta={"a": 1})
    assert info.id == "quote-1"
    assert info.created_at == clock.now
    assert info.last_read_at is None
    assert info.meta == {"a": 1}
    assert registry.get(info.id) is info
    assert registry.find("quote", "265598") is info
    assert registry.find("quote", "other") is None

    clock.advance(5)
    assert registry.latest(info.id) == Snapshot(value=1)
    assert registry.get(info.id).last_read_at == clock.now
    assert registry.list() == [info]
    assert len(registry) == 1


async def test_duplicate_returns_existing_without_opening_a_stream(
    registry: SubscriptionRegistry,
) -> None:
    first = await add(registry, "quote", "k", snapshot=lambda: Snapshot(value=1))
    opener = MagicMock()
    second = await registry.add("quote", "k", opener=opener)
    assert second is first
    opener.assert_not_called()
    assert len(registry) == 1


async def test_async_openers_are_awaited(registry: SubscriptionRegistry) -> None:
    async def opener() -> Stream:
        await asyncio.sleep(0)
        return Stream(cancel=MagicMock(), snapshot=lambda: Snapshot(value=7))

    info = await registry.add("bars", "k", opener=opener)
    assert registry.latest(info.id) == Snapshot(value=7)


async def test_concurrent_adds_open_one_stream_per_key(registry: SubscriptionRegistry) -> None:
    opened = 0

    async def opener() -> Stream:
        nonlocal opened
        opened += 1
        await asyncio.sleep(0.01)
        return Stream(cancel=MagicMock(), snapshot=MagicMock())

    first, second = await asyncio.gather(
        registry.add("quote", "k", opener=opener), registry.add("quote", "k", opener=opener)
    )
    assert first is second
    assert opened == 1


async def test_limit_is_enforced_before_opening(registry: SubscriptionRegistry) -> None:
    await add(registry, "quote", "a")
    await add(registry, "quote", "b")
    with pytest.raises(SubscriptionLimitError, match="limit is 2"):
        registry.ensure_capacity()
    opener = MagicMock()
    with pytest.raises(SubscriptionLimitError):
        await registry.add("quote", "c", opener=opener)
    opener.assert_not_called()


async def test_ticker_streams_survive_a_duplicate_request(
    registry: SubscriptionRegistry,
) -> None:
    """With ib_async's contract-keyed tickers, a duplicate must never reach IBKR.

    Opening a second reqMktData for the same contract and cancelling it would pop the
    shared ticker's only reqId, leaving the first stream running at IBKR, uncancellable.
    """
    ib = IB()
    ib.client.getReqId = MagicMock(side_effect=itertools.count(100).__next__)  # type: ignore[method-assign]
    ib.client.reqMktData = MagicMock()  # type: ignore[method-assign]
    ib.client.cancelMktData = MagicMock()  # type: ignore[method-assign]
    contract = Stock("AAPL", "SMART", "USD", conId=265598)

    def opener() -> Stream:
        ib.reqMktData(contract)

        def cancel() -> None:
            ib.cancelMktData(contract)

        return Stream(cancel=cancel, snapshot=MagicMock())

    first = await registry.add("quote", "265598", opener=opener)
    second = await registry.add("quote", "265598", opener=opener)
    assert second is first
    assert ib.client.reqMktData.call_count == 1
    await registry.remove(first.id)
    ib.client.cancelMktData.assert_called_once()  # the one stream is really cancelled


async def test_remove_cancels_sync_and_async(registry: SubscriptionRegistry) -> None:
    sync_cancel = MagicMock(return_value=None)
    async_cancel = AsyncMock()
    a = await add(registry, "quote", "a", cancel=sync_cancel)
    b = await add(registry, "depth", "b", cancel=async_cancel)

    assert await registry.remove(a.id) is a
    sync_cancel.assert_called_once_with()
    assert await registry.cancel_all() == 1
    async_cancel.assert_awaited_once()
    assert registry.list() == []
    with pytest.raises(SubscriptionNotFoundError, match=b.id):
        registry.get(b.id)
    with pytest.raises(SubscriptionNotFoundError):
        registry.latest("nope")
    with pytest.raises(SubscriptionNotFoundError):
        await registry.remove("nope")


async def test_cancel_failures_are_logged_not_raised(
    registry: SubscriptionRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    info = await add(registry, "quote", "a", cancel=MagicMock(side_effect=RuntimeError("boom")))
    await registry.remove(info.id)
    assert "Cancelling subscription" in caplog.text


async def test_resubscribe_all_drops_what_cannot_be_restored(
    settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> None:
    registry = SubscriptionRegistry(settings_factory(max_subscriptions=5, request_timeout=0.05))
    good = AsyncMock()
    bad = MagicMock(side_effect=RuntimeError("gone"))

    async def hangs() -> None:
        await asyncio.sleep(10)

    kept = await add(registry, "quote", "a", resubscribe=good)
    failed = await add(registry, "depth", "b", resubscribe=bad)
    slow = await add(registry, "bars", "c", resubscribe=hangs)
    frozen = await add(registry, "news", "d")  # no resubscribe: cannot be restored
    registry.mark_all_stale()
    assert all(info.stale for info in registry.list())

    assert await registry.resubscribe_all() == 1
    good.assert_awaited_once()
    assert [info.id for info in registry.list()] == [kept.id]
    assert registry.get(kept.id).stale is False
    for gone in (failed, slow, frozen):
        with pytest.raises(SubscriptionNotFoundError):
            registry.latest(gone.id)
    assert "Resubscribing depth-2 failed" in caplog.text
    assert "news-4 cannot be re-requested" in caplog.text


async def test_overlapping_resubscribes_run_one_pass_after_another(
    registry: SubscriptionRegistry,
) -> None:
    """A second 1101 during a pass must not re-request streams concurrently."""
    running = 0
    overlap = 0
    calls = 0
    release = asyncio.Event()

    async def resubscribe() -> None:
        nonlocal running, overlap, calls
        calls += 1
        running += 1
        overlap = max(overlap, running)
        await release.wait()
        running -= 1

    await add(registry, "bars", "a", resubscribe=resubscribe)
    first = asyncio.create_task(registry.resubscribe_all())
    await asyncio.sleep(0)
    assert await registry.resubscribe_all() == 0  # coalesced into the running call
    assert await registry.resubscribe_all() == 0
    release.set()
    assert await first == 1
    assert overlap == 1
    assert calls == 2  # the running pass, then exactly one more


async def test_reap_idle(registry: SubscriptionRegistry, clock: Clock) -> None:
    cancel_idle = MagicMock()
    idle = await add(registry, "quote", "idle", cancel=cancel_idle)
    busy = await add(registry, "quote", "busy")
    clock.advance(45)
    registry.latest(busy.id)
    clock.advance(30)

    assert await registry.reap_idle() == [idle.id]
    cancel_idle.assert_called_once_with()
    assert [s.id for s in registry.list()] == [busy.id]


async def test_reaper_task_runs_and_stop_cancels_everything(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ib_gateway_mcp.subscriptions._MIN_REAP_INTERVAL", 0.005)
    registry = SubscriptionRegistry(settings_factory(subscription_idle_ttl=0.01))
    cancel = MagicMock()
    await add(registry, "quote", "a", cancel=cancel)
    registry.start()
    registry.start()  # idempotent
    for _ in range(100):
        if not registry.list():
            break
        await asyncio.sleep(0.02)
    assert registry.list() == []
    cancel.assert_called_once_with()

    stop_cancel = MagicMock()
    await add(registry, "quote", "b", cancel=stop_cancel)
    await registry.stop()
    stop_cancel.assert_called_once_with()


async def test_a_slow_open_does_not_block_other_subscriptions(
    registry: SubscriptionRegistry,
) -> None:
    release = asyncio.Event()
    calls = 0

    async def slow() -> Stream:
        nonlocal calls
        calls += 1
        await release.wait()
        return Stream(MagicMock(), MagicMock())

    first = asyncio.create_task(registry.add("bars", "slow", opener=slow, slow_open=True))
    await asyncio.sleep(0)
    # Another stream opens while the slow one is still loading.
    other = await asyncio.wait_for(add(registry, "quote", "fast"), timeout=1)
    assert registry.find("quote", "fast") is other
    # The reserved slot counts against the cap (2): nothing else fits.
    with pytest.raises(SubscriptionLimitError, match="open or opening"):
        await add(registry, "quote", "third")
    # A second call for the same key waits for the first and gets its handle.
    twin = asyncio.create_task(registry.add("bars", "slow", opener=slow, slow_open=True))
    await asyncio.sleep(0)
    release.set()
    info = await first
    assert await twin is info
    assert calls == 1
    assert registry.find("bars", "slow") is info


async def test_a_failed_slow_open_releases_its_slot(registry: SubscriptionRegistry) -> None:
    async def failing() -> Stream:
        await asyncio.sleep(0)
        raise RuntimeError("backfill failed")

    with pytest.raises(RuntimeError, match="backfill failed"):
        await registry.add("bars", "k", opener=failing, slow_open=True)
    assert len(registry) == 0
    await add(registry, "bars", "k")
    await add(registry, "quote", "other")
    assert len(registry) == 2


async def test_a_cancelled_slow_open_releases_its_slot(registry: SubscriptionRegistry) -> None:
    async def never() -> Stream:
        await asyncio.get_running_loop().create_future()
        raise AssertionError("unreachable")

    task = asyncio.create_task(registry.add("bars", "k", opener=never, slow_open=True))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    info = await add(registry, "bars", "k")
    assert registry.find("bars", "k") is info
