"""HistoryService: bars, ticks, head timestamp, histogram and trading schedule."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, get_args
from unittest.mock import MagicMock

import pytest
from ib_async import (
    Contract,
    Forex,
    HistogramData,
    HistoricalSchedule,
    HistoricalSession,
    HistoricalTick,
    HistoricalTickBidAsk,
    HistoricalTickLast,
    TickAttribBidAsk,
    TickAttribLast,
)
from ib_async.wrapper import RequestError

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    IbApiError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RateLimitError,
    RequestTimeoutError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.common import BarSize, ContractSpec
from ib_gateway_mcp.services._pacing import HistoricalPacing
from ib_gateway_mcp.services.history import (
    BAR_SECONDS,
    IDENTICAL_REQUEST_TTL,
    PACING_MAX_REQUESTS,
    HistoryService,
    normalize_duration,
    normalize_period,
)
from tests.fakes import (
    FIXED_TIME,
    FakeClock,
    bar,
    contract_details,
    go_offline,
    pending,
    raises,
    returns,
    stock,
)

AAPL = ContractSpec(symbol="AAPL")


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    """Short timeouts so the timeout tests stay fast."""
    return settings_factory(request_timeout=0.2)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(gateway: Gateway, fake_ib: MagicMock, clock: FakeClock) -> HistoryService:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    gateway.pacing = HistoricalPacing(clock.monotonic)
    return HistoryService(gateway, monotonic=clock.monotonic)


def sent(mock: MagicMock) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Positional and keyword arguments of the last call."""
    return tuple(mock.call_args.args), dict(mock.call_args.kwargs)


def daily_bar(day: int, close: float = 100.0, **kwargs: Any) -> Any:
    return bar(date=date(2026, 1, day), close=close, **kwargs)


def hanging(fake_ib: MagicMock, req_id: int) -> Callable[..., Any]:
    """A side effect that registers ``req_id`` for its contract, as ib_async does, and
    never answers."""
    fake_ib.wrapper._reqId2Contract = {}

    async def side_effect(contract: Contract, *_args: Any, **_kwargs: Any) -> Any:
        fake_ib.wrapper._reqId2Contract[req_id] = contract
        return await asyncio.get_running_loop().create_future()

    return side_effect


# --- durations and periods ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5 D", ("5 D", 5 * 86400)),
        ("1800 s", ("1800 S", 1800)),
        ("1800S", ("1800 S", 1800)),
        ("30 mins", ("1800 S", 1800)),
        ("2 hours", ("7200 S", 7200)),
        ("48 hours", ("2 D", 2 * 86400)),
        ("3 days", ("3 D", 3 * 86400)),
        ("2 weeks", ("2 W", 14 * 86400)),
        ("6 M", ("6 M", 180 * 86400)),
        ("6 months", ("6 M", 180 * 86400)),
        (" 1 y ", ("1 Y", 365 * 86400)),
    ],
)
def test_normalize_duration(raw: str, expected: tuple[str, int]) -> None:
    assert normalize_duration(raw) == expected


@pytest.mark.parametrize("raw", ["", "5", "D", "0 D", "5 X", "90000 S", "25 hours", "-1 D"])
def test_normalize_duration_rejects(raw: str) -> None:
    with pytest.raises(InvalidRequestError, match="duration"):
        normalize_duration(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3 days", "3 days"),
        ("1 week", "1 week"),
        ("1 weeks", "1 week"),
        ("2 W", "2 weeks"),
        ("1 M", "1 month"),
        ("1 year", "1 year"),
    ],
)
def test_normalize_period(raw: str, expected: str) -> None:
    assert normalize_period(raw) == expected


@pytest.mark.parametrize("raw", ["1800 S", "3 hours", "week", "0 days"])
def test_normalize_period_rejects(raw: str) -> None:
    with pytest.raises(InvalidRequestError, match="period"):
        normalize_period(raw)


def test_every_bar_size_has_a_length() -> None:
    assert set(BAR_SECONDS) == set(get_args(BarSize))


# --- historical bars --------------------------------------------------------------------------


async def test_bars_request_and_conversion(
    service: HistoryService, fake_ib: MagicMock, settings: Settings
) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns(
        [bar(FIXED_TIME, 100.0), bar(FIXED_TIME + timedelta(hours=1), 101.0)]
    )
    result = await service.historical_bars(AAPL, duration="2 days", bar_size="1 hour")

    args, kwargs = sent(fake_ib.reqHistoricalDataAsync)
    contract, end, duration, bar_size, what, use_rth = args
    assert (contract.conId, end, duration, bar_size, what, use_rth) == (
        265598,
        "",
        "2 D",
        "1 hour",
        "TRADES",
        True,
    )
    assert kwargs == {"formatDate": 2, "timeout": settings.request_timeout}
    assert result.contract.con_id == 265598
    assert (result.duration, result.total, result.truncated, result.end) == ("2 D", 2, False, None)
    first = result.bars[0]
    assert first.time == FIXED_TIME
    assert (first.open, first.high, first.low, first.close) == (99.0, 101.0, 98.0, 100.0)
    assert (first.volume, first.wap, first.bar_count) == (1000.0, 100.0, 10)


async def test_bars_times_are_utc_and_daily_bars_keep_their_date(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    naive = datetime(2026, 1, 2, 15, 30)
    fake_ib.reqHistoricalDataAsync.side_effect = returns([bar(naive), daily_bar(5)])
    result = await service.historical_bars(AAPL, duration="1 W", bar_size="1 day")
    assert result.bars[0].time == datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    assert result.bars[1].time == date(2026, 1, 5)
    assert not isinstance(result.bars[1].time, datetime)


async def test_bars_nan_and_quote_bars_become_none(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns(
        [
            bar(open=math.nan, high=math.inf),
            bar(FIXED_TIME + timedelta(hours=1), volume=-1.0, average=-1.0, barCount=-1),
        ]
    )
    result = await service.historical_bars(AAPL, what_to_show="MIDPOINT")
    assert (result.bars[0].open, result.bars[0].high) == (None, None)
    quote_bar = result.bars[1]
    assert (quote_bar.volume, quote_bar.wap, quote_bar.bar_count) == (None, None, None)
    assert quote_bar.close == 100.0


async def test_bars_truncation_keeps_the_newest(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns(
        [daily_bar(day, close=float(day)) for day in range(1, 6)]
    )
    result = await service.historical_bars(AAPL, duration="1 M", bar_size="1 day", limit=2)
    assert result.truncated is True
    assert result.total == 5
    assert [b.close for b in result.bars] == [4.0, 5.0]


async def test_bars_default_limit(service: HistoryService, fake_ib: MagicMock) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    fake_ib.reqHistoricalDataAsync.side_effect = returns(
        [bar(start + timedelta(minutes=i)) for i in range(1001)]
    )
    result = await service.historical_bars(AAPL, duration="1 D", bar_size="1 min")
    assert (len(result.bars), result.total, result.truncated) == (1000, 1001, True)
    assert result.bars[-1].time == start + timedelta(minutes=1000)


async def test_bars_end_is_sent_as_utc(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns([daily_bar(2)])
    naive_end = datetime(2026, 1, 2, 21, 0)
    result = await service.historical_bars(AAPL, end=naive_end, bar_size="1 day", duration="5 D")
    sent_end = sent(fake_ib.reqHistoricalDataAsync)[0][1]
    assert sent_end == datetime(2026, 1, 2, 21, 0, tzinfo=UTC)
    assert result.end == sent_end


async def test_bars_empty_answer_is_not_found(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns([])
    with pytest.raises(NotFoundError, match="returned no 1 hour TRADES bars for AAPL") as info:
        await service.historical_bars(AAPL)
    assert "get_head_timestamp" in str(info.value)


async def test_bars_empty_answer_after_the_timeout_is_a_timeout(
    service: HistoryService, fake_ib: MagicMock, clock: FakeClock, settings: Settings
) -> None:
    async def times_out(*_args: Any, **_kwargs: Any) -> list[Any]:
        # ib_async gave up and returned []; its timer may fire a hair early.
        clock.advance(settings.request_timeout * 0.95)
        return []

    fake_ib.reqHistoricalDataAsync.side_effect = times_out
    with pytest.raises(RequestTimeoutError, match="shorter duration"):
        await service.historical_bars(AAPL)


async def test_bars_timeout_beyond_ib_async_cancels_at_ibkr(
    service: HistoryService, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ib_gateway_mcp.services.history._TIMEOUT_GRACE", 0.05)
    fake_ib.reqHistoricalDataAsync.side_effect = hanging(fake_ib, 41)
    with pytest.raises(RequestTimeoutError, match="1 hour TRADES bars"):
        await service.historical_bars(AAPL)
    fake_ib.client.cancelHistoricalData.assert_called_once_with(41)
    fake_ib.wrapper._endReq.assert_called_once_with(41)


HMDS = "Historical Market Data Service error message:"


@pytest.mark.parametrize(
    ("ib_error", "expected"),
    [
        ((162, f"{HMDS}HMDS query returned no data: X"), (NotFoundError, "get_head_timestamp")),
        ((162, f"{HMDS}Historical data request pacing violation"), (IbApiError, "Wait a minute")),
        ((162, f"{HMDS}No market data permissions for NYSE STK"), (IbApiError, "market-data")),
        ((354, "Requested market data is not subscribed."), (IbApiError, "market-data")),
        ((366, "No historical data query found for ticker id:7"), (IbApiError, "dropped")),
        ((162, f"{HMDS}API historical data query cancelled: 7"), (IbApiError, "dropped")),
        (
            (162, f"{HMDS}Trading TWS session is connected from a different IP address"),
            (IbApiError, "one session"),
        ),
        ((162, f"{HMDS}Time length exceed max."), (IbApiError, "shorter duration")),
        ((10197, "No market data during competing live session"), (IbApiError, "one session")),
        ((200, "No security definition has been found"), (NotFoundError, "no contract")),
        ((10314, "End Date/Time: The date is invalid."), (IbApiError, "End Date/Time")),
    ],
)
async def test_bars_ib_errors_are_explained(
    service: HistoryService,
    fake_ib: MagicMock,
    ib_error: tuple[int, str],
    expected: tuple[type[Exception], str],
) -> None:
    code, message = ib_error
    error, needle = expected
    fake_ib.reqHistoricalDataAsync.side_effect = raises(RequestError(7, code, message))
    with pytest.raises(error, match=needle) as info:
        await service.historical_bars(AAPL)
    if isinstance(info.value, IbApiError):
        assert info.value.error_code == code
        assert info.value.error_message == message
        assert str(info.value).startswith(f"IB error {code}: {message}")


async def test_bars_fail_fast_on_321_for_their_contract(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    async def rejected(contract: Contract, *_args: Any, **_kwargs: Any) -> Any:
        fake_ib.errorEvent.emit(7, 321, "Error validating request.-'bS' : cause - bad", contract)
        return await asyncio.get_running_loop().create_future()

    fake_ib.reqHistoricalDataAsync.side_effect = rejected
    listeners = len(fake_ib.errorEvent)
    with pytest.raises(IbApiError, match="IBKR rejected the parameters") as info:
        await asyncio.wait_for(service.historical_bars(AAPL), 1.0)
    assert info.value.error_code == 321
    assert len(fake_ib.errorEvent) == listeners  # ours is gone again
    fake_ib.wrapper._endReq.assert_called_once_with(7)  # ib_async's pending state dropped


async def test_bars_ignore_321_for_other_contracts(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    async def other_rejected(*_args: Any, **_kwargs: Any) -> Any:
        fake_ib.errorEvent.emit(8, 321, "Error validating request", stock("MSFT", 272093))
        await asyncio.sleep(0)
        return [bar()]

    fake_ib.reqHistoricalDataAsync.side_effect = other_rejected
    result = await service.historical_bars(AAPL)
    assert len(result.bars) == 1


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"bar_size": "1 secs", "duration": "1 D"}, "does not serve 1 secs bars over 1 D"),
        ({"bar_size": "5 secs", "duration": "14400 S"}, "does not serve 5 secs bars"),
        ({"bar_size": "30 secs", "duration": "1 D"}, "does not serve 30 secs bars"),
        ({"bar_size": "1 day", "duration": "3600 S"}, "longer than the duration"),
        ({"bar_size": "1 week", "duration": "5 D"}, "longer than the duration"),
        (
            {"what_to_show": "ADJUSTED_LAST", "end": FIXED_TIME, "bar_size": "1 day"},
            "leave end empty",
        ),
        ({"duration": "one day"}, "not valid"),
    ],
)
async def test_bars_invalid_requests_send_nothing(
    service: HistoryService, fake_ib: MagicMock, kwargs: dict[str, Any], needle: str
) -> None:
    with pytest.raises(InvalidRequestError, match=needle):
        await service.historical_bars(AAPL, **kwargs)
    fake_ib.reqContractDetailsAsync.assert_not_called()
    fake_ib.reqHistoricalDataAsync.assert_not_called()


@pytest.mark.parametrize(
    ("bar_size", "duration"),
    [("1 secs", "1800 S"), ("5 secs", "3600 S"), ("30 secs", "28800 S"), ("1 min", "1 W")],
)
async def test_bars_allowed_combinations(
    service: HistoryService, fake_ib: MagicMock, bar_size: BarSize, duration: str
) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns([bar()])
    result = await service.historical_bars(AAPL, bar_size=bar_size, duration=duration)
    assert result.bar_size == bar_size


async def test_bars_forex_trades_are_refused(service: HistoryService, fake_ib: MagicMock) -> None:
    fx = Forex("EURUSD", conId=12087792)
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(fx)])
    spec = ContractSpec(symbol="EUR", sec_type="CASH", exchange="IDEALPRO")
    with pytest.raises(InvalidRequestError, match="MIDPOINT"):
        await service.historical_bars(spec)
    fake_ib.reqHistoricalDataAsync.assert_not_called()


async def test_bars_unknown_contract(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(RequestError(3, 200, "No security"))
    with pytest.raises(NotFoundError, match="No contract matches NOPE"):
        await service.historical_bars(ContractSpec(symbol="NOPE"))


async def test_bars_when_disconnected(service: HistoryService, fake_ib: MagicMock) -> None:
    go_offline(fake_ib)
    with pytest.raises(NotConnectedError):
        await service.historical_bars(AAPL)


# --- cache and pacing -------------------------------------------------------------------------


async def test_identical_requests_are_answered_from_the_cache(
    service: HistoryService, fake_ib: MagicMock, clock: FakeClock
) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns([bar()])
    first = await service.historical_bars(AAPL, bar_size="5 secs", duration="600 S")
    again = await service.historical_bars(AAPL, bar_size="5 secs", duration="600 S", limit=1)
    assert fake_ib.reqHistoricalDataAsync.call_count == 1
    assert again.bars == first.bars
    clock.advance(IDENTICAL_REQUEST_TTL)
    await service.historical_bars(AAPL, bar_size="5 secs", duration="600 S")
    assert fake_ib.reqHistoricalDataAsync.call_count == 2


async def test_small_bar_pacing_window(
    service: HistoryService, fake_ib: MagicMock, clock: FakeClock
) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns([bar()])
    for i in range(PACING_MAX_REQUESTS):
        await service.historical_bars(AAPL, bar_size="5 secs", duration=f"{600 + i} S")
        clock.advance(0.5)
    with pytest.raises(RateLimitError, match=r"Retry in \d+ s"):
        await service.historical_bars(AAPL, bar_size="5 secs", duration="3000 S")
    assert fake_ib.reqHistoricalDataAsync.call_count == PACING_MAX_REQUESTS
    clock.advance(600)
    await service.historical_bars(AAPL, bar_size="5 secs", duration="3000 S")


async def test_small_bar_burst_limit(
    service: HistoryService, fake_ib: MagicMock, clock: FakeClock
) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns([bar()])
    for i in range(5):
        await service.historical_bars(AAPL, bar_size="1 secs", duration=f"{60 + i} S")
    with pytest.raises(RateLimitError, match="sixth request"):
        await service.historical_bars(AAPL, bar_size="1 secs", duration="70 S")
    # Another data type is a different burst.
    await service.historical_bars(AAPL, bar_size="1 secs", duration="70 S", what_to_show="BID")
    clock.advance(2)
    await service.historical_bars(AAPL, bar_size="1 secs", duration="70 S")


async def test_bid_ask_counts_twice(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns([bar()])
    for duration in ("60 S", "61 S"):
        await service.historical_bars(
            AAPL, bar_size="1 secs", duration=duration, what_to_show="BID_ASK"
        )
    with pytest.raises(RateLimitError):
        await service.historical_bars(
            AAPL, bar_size="1 secs", duration="62 S", what_to_show="BID_ASK"
        )


async def test_large_bars_are_not_paced(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns([bar()])
    for i in range(PACING_MAX_REQUESTS + 5):
        await service.historical_bars(AAPL, bar_size="1 hour", duration=f"{i + 1} D")
    assert fake_ib.reqHistoricalDataAsync.call_count == PACING_MAX_REQUESTS + 5


# --- historical ticks -----------------------------------------------------------------------


def last_tick(second: int = 0, price: float = 100.0) -> HistoricalTickLast:
    return HistoricalTickLast(
        FIXED_TIME + timedelta(seconds=second),
        TickAttribLast(pastLimit=False, unreported=True),
        price,
        10.0,
        "NASDAQ",
        "  ",
    )


async def test_ticks_trades(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalTicksAsync.side_effect = returns([last_tick(0), last_tick(1, math.nan)])
    result = await service.historical_ticks(AAPL, start=FIXED_TIME, count=10)

    args, kwargs = sent(fake_ib.reqHistoricalTicksAsync)
    contract, start, end, count, what, use_rth = args
    assert (contract.conId, start, end, count, what, use_rth) == (
        265598,
        FIXED_TIME,
        "",
        10,
        "TRADES",
        True,
    )
    assert kwargs == {"ignoreSize": False}
    tick = result.ticks[0]
    assert (tick.time, tick.price, tick.size, tick.exchange) == (FIXED_TIME, 100.0, 10.0, "NASDAQ")
    assert (tick.special_conditions, tick.past_limit, tick.unreported) == (None, False, True)
    assert tick.bid is None
    assert result.ticks[1].price is None
    assert (result.truncated, result.count, result.start, result.end) == (
        False,
        10,
        FIXED_TIME,
        None,
    )


async def test_ticks_bid_ask(service: HistoryService, fake_ib: MagicMock) -> None:
    tick = HistoricalTickBidAsk(
        FIXED_TIME, TickAttribBidAsk(bidPastLow=True, askPastHigh=False), 99.5, 100.5, 3.0, 4.0
    )
    fake_ib.reqHistoricalTicksAsync.side_effect = returns([tick])
    result = await service.historical_ticks(
        AAPL, end=FIXED_TIME, count=1, what_to_show="BID_ASK", ignore_size=True
    )
    args, kwargs = sent(fake_ib.reqHistoricalTicksAsync)
    assert (args[1], args[2], kwargs) == ("", FIXED_TIME, {"ignoreSize": True})
    out = result.ticks[0]
    assert (out.bid, out.ask, out.bid_size, out.ask_size) == (99.5, 100.5, 3.0, 4.0)
    assert (out.bid_past_low, out.ask_past_high, out.price) == (True, False, None)
    assert result.truncated is True  # the full count came back


async def test_ticks_midpoint_has_no_size(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalTicksAsync.side_effect = returns([HistoricalTick(FIXED_TIME, 100.0, 0.0)])
    result = await service.historical_ticks(AAPL, start=FIXED_TIME, what_to_show="MIDPOINT")
    assert (result.ticks[0].price, result.ticks[0].size) == (100.0, None)


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({}, "exactly one of start"),
        ({"start": FIXED_TIME, "end": FIXED_TIME}, "exactly one of start"),
        ({"start": FIXED_TIME, "count": 0}, "count must be 1-1000"),
        ({"start": FIXED_TIME, "count": 1001}, "count must be 1-1000"),
    ],
)
async def test_ticks_invalid_requests(
    service: HistoryService, fake_ib: MagicMock, kwargs: dict[str, Any], needle: str
) -> None:
    with pytest.raises(InvalidRequestError, match=needle):
        await service.historical_ticks(AAPL, **kwargs)
    fake_ib.reqHistoricalTicksAsync.assert_not_called()


async def test_ticks_empty_is_not_found(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalTicksAsync.side_effect = returns([])
    with pytest.raises(NotFoundError, match="before 2026-01-02T15:30:00"):
        await service.historical_ticks(AAPL, end=FIXED_TIME)


async def test_ticks_pacing_error(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalTicksAsync.side_effect = raises(
        RequestError(9, 162, "Historical Market Data Service error message:pacing violation")
    )
    with pytest.raises(IbApiError, match="Wait a minute") as info:
        await service.historical_ticks(AAPL, start=FIXED_TIME)
    assert info.value.error_code == 162


async def test_ticks_are_paced(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalTicksAsync.side_effect = returns([last_tick()])
    for second in range(5):
        await service.historical_ticks(AAPL, start=FIXED_TIME + timedelta(seconds=second))
    with pytest.raises(RateLimitError):
        await service.historical_ticks(AAPL, start=FIXED_TIME + timedelta(seconds=9))


async def test_ticks_timeout(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalTicksAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match="TRADES ticks"):
        await service.historical_ticks(AAPL, start=FIXED_TIME)


async def test_ticks_timeout_drops_the_request_without_a_cancel(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    fake_ib.reqHistoricalTicksAsync.side_effect = hanging(fake_ib, 45)
    with pytest.raises(RequestTimeoutError):
        await service.historical_ticks(AAPL, start=FIXED_TIME)
    fake_ib.wrapper._endReq.assert_called_once_with(45)  # no cancelHistoricalTicks at v178
    fake_ib.client.cancelHistoricalData.assert_not_called()


async def test_ticks_forex_trades_are_refused(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [contract_details(Forex("EURUSD", conId=12087792))]
    )
    spec = ContractSpec(symbol="EUR", sec_type="CASH", exchange="IDEALPRO")
    with pytest.raises(InvalidRequestError, match="MIDPOINT"):
        await service.historical_ticks(spec, start=FIXED_TIME)
    fake_ib.reqHistoricalTicksAsync.assert_not_called()


# --- head timestamp -------------------------------------------------------------------------


async def test_head_timestamp(service: HistoryService, fake_ib: MagicMock) -> None:
    earliest = datetime(1980, 12, 12, 14, 30, tzinfo=UTC)
    fake_ib.reqHeadTimeStampAsync.side_effect = returns(earliest)
    result = await service.head_timestamp(AAPL, what_to_show="MIDPOINT", use_rth=False)
    args, kwargs = sent(fake_ib.reqHeadTimeStampAsync)
    assert (args[0].conId, args[1:], kwargs) == (265598, ("MIDPOINT", False), {"formatDate": 2})
    assert (result.earliest, result.what_to_show, result.use_rth) == (earliest, "MIDPOINT", False)


async def test_head_timestamp_date_becomes_utc_midnight(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    fake_ib.reqHeadTimeStampAsync.side_effect = returns(date(1980, 12, 12))
    result = await service.head_timestamp(AAPL)
    assert result.earliest == datetime(1980, 12, 12, tzinfo=UTC)


async def test_head_timestamp_empty_is_not_found(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    fake_ib.reqHeadTimeStampAsync.side_effect = returns([])
    with pytest.raises(NotFoundError, match="earliest TRADES data"):
        await service.head_timestamp(AAPL)


async def test_head_timestamp_timeout_cancels_at_ibkr(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    fake_ib.reqHeadTimeStampAsync.side_effect = hanging(fake_ib, 42)
    with pytest.raises(RequestTimeoutError):
        await service.head_timestamp(AAPL)
    fake_ib.client.cancelHeadTimeStamp.assert_called_once_with(42)
    fake_ib.wrapper._endReq.assert_called_once_with(42)


# --- histogram ------------------------------------------------------------------------------


async def test_histogram(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistogramDataAsync.side_effect = returns(
        [HistogramData(101.0, 5), HistogramData(100.0, 7), HistogramData(math.nan, 1)]
    )
    result = await service.histogram(AAPL, period="3 D", use_rth=False)
    args, _kwargs = sent(fake_ib.reqHistogramDataAsync)
    assert (args[0].conId, args[1], args[2]) == (265598, False, "3 days")
    assert [(e.price, e.count) for e in result.entries] == [(100.0, 7), (101.0, 5)]
    assert (result.period, result.total, result.truncated) == ("3 days", 2, False)


async def test_histogram_keeps_the_busiest_levels(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    counts = {100.0: 1, 101.0: 9, 102.0: 3, 103.0: 8, 104.0: 2}
    fake_ib.reqHistogramDataAsync.side_effect = returns(
        [HistogramData(price, count) for price, count in counts.items()]
    )
    result = await service.histogram(AAPL, limit=2)
    assert [e.price for e in result.entries] == [101.0, 103.0]
    assert (result.total, result.truncated) == (5, True)


async def test_histogram_empty_is_not_found(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistogramDataAsync.side_effect = returns([])
    with pytest.raises(NotFoundError, match="1 week histogram"):
        await service.histogram(AAPL)


async def test_histogram_invalid_period(service: HistoryService, fake_ib: MagicMock) -> None:
    with pytest.raises(InvalidRequestError):
        await service.histogram(AAPL, period="3 hours")
    fake_ib.reqHistogramDataAsync.assert_not_called()


async def test_histogram_timeout_cancels_at_ibkr(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    fake_ib.reqHistogramDataAsync.side_effect = hanging(fake_ib, 43)
    with pytest.raises(RequestTimeoutError):
        await service.histogram(AAPL)
    fake_ib.client.cancelHistogramData.assert_called_once_with(43)


# --- trading schedule -----------------------------------------------------------------------


def schedule(time_zone: str = "US/Eastern", sessions: int = 2) -> HistoricalSchedule:
    return HistoricalSchedule(
        startDateTime="20260102-09:30:00",
        endDateTime="20260105-16:00:00",
        timeZone=time_zone,
        sessions=[
            HistoricalSession(
                startDateTime=f"2026010{day}-09:30:00",
                endDateTime=f"2026010{day}-16:00:00",
                refDate=f"2026010{day}",
            )
            for day in (2, 5)[:sessions]
        ],
    )


async def test_trading_schedule(service: HistoryService, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalScheduleAsync.side_effect = returns(schedule())
    result = await service.trading_schedule(AAPL, num_days=3)
    args, _kwargs = sent(fake_ib.reqHistoricalScheduleAsync)
    assert (args[0].conId, args[1:]) == (265598, (3, "", True))
    assert result.time_zone == "US/Eastern"
    first = result.sessions[0]
    assert first.start is not None
    assert first.start.isoformat() == "2026-01-02T09:30:00-05:00"
    assert first.start == datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    assert first.ref_date == date(2026, 1, 2)
    assert result.end is not None
    assert result.end.isoformat() == "2026-01-05T16:00:00-05:00"


async def test_trading_schedule_end_and_unknown_zone(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    fake_ib.reqHistoricalScheduleAsync.side_effect = returns(schedule(time_zone="Nowhere/Land"))
    result = await service.trading_schedule(AAPL, end=FIXED_TIME, use_rth=False)
    args, _kwargs = sent(fake_ib.reqHistoricalScheduleAsync)
    assert args[2:] == (FIXED_TIME, False)
    assert (result.sessions[0].start, result.sessions[0].ref_date) == (None, date(2026, 1, 2))


async def test_trading_schedule_without_sessions(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    fake_ib.reqHistoricalScheduleAsync.side_effect = returns(schedule(sessions=0))
    with pytest.raises(NotFoundError, match="no sessions"):
        await service.trading_schedule(AAPL)


@pytest.mark.parametrize("num_days", [0, 31])
async def test_trading_schedule_days_range(
    service: HistoryService, fake_ib: MagicMock, num_days: int
) -> None:
    with pytest.raises(InvalidRequestError, match="num_days"):
        await service.trading_schedule(AAPL, num_days=num_days)
    fake_ib.reqHistoricalScheduleAsync.assert_not_called()


async def test_trading_schedule_timeout_cancels_at_ibkr(
    service: HistoryService, fake_ib: MagicMock
) -> None:
    fake_ib.reqHistoricalScheduleAsync.side_effect = hanging(fake_ib, 44)
    with pytest.raises(RequestTimeoutError):
        await service.trading_schedule(AAPL)
    fake_ib.client.cancelHistoricalData.assert_called_once_with(44)


async def test_gateway_exposes_the_service(gateway: Gateway) -> None:
    assert isinstance(gateway.history, HistoryService)
