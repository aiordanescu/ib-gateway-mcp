"""The test doubles themselves: both shapes of ib_async ``*Async`` request."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from ib_async.wrapper import RequestError

from tests.fakes import FIXED_TIME, FakeClock, make_fake_ib, pending, raises, returns, stock


async def test_returns_and_raises_work_for_plain_and_async_def_methods() -> None:
    ib = make_fake_ib()
    # reqCurrentTimeAsync is a plain def returning a future; qualifyContractsAsync is async def.
    assert isinstance(ib.reqCurrentTimeAsync, MagicMock)
    assert not isinstance(ib.reqCurrentTimeAsync, AsyncMock)
    assert isinstance(ib.qualifyContractsAsync, AsyncMock)

    ib.reqCurrentTimeAsync.side_effect = returns(FIXED_TIME)
    ib.qualifyContractsAsync.side_effect = returns([stock()])
    assert await ib.reqCurrentTimeAsync() == FIXED_TIME
    assert await ib.qualifyContractsAsync(stock()) == [stock()]

    error = RequestError(7, 200, "No security definition")
    ib.reqContractDetailsAsync.side_effect = raises(error)
    ib.reqHistoricalDataAsync.side_effect = raises(error)
    with pytest.raises(RequestError):
        await ib.reqContractDetailsAsync(stock())
    with pytest.raises(RequestError):
        await ib.reqHistoricalDataAsync(stock(), "", "1 D", "1 hour", "TRADES", True)


async def test_the_fake_ib_keeps_the_real_signatures() -> None:
    ib = make_fake_ib()
    with pytest.raises(TypeError):
        ib.reqMktData(stock(), "", False, False, [], "extra")  # one argument too many
    with pytest.raises(TypeError):
        await ib.qualifyContractsAsync(stock(), bogus=True)
    with pytest.raises(AttributeError):
        _ = ib.reqSomethingThatDoesNotExist
    with pytest.raises(AttributeError):
        _ = ib.client.notAClientMethod
    assert ib.client.serverVersion() == 178
    ib.reqMktData(stock(), "", False, False, [])
    ib.reqMktData.assert_called_once()
    assert isinstance(ib.wrapper.startReq, MagicMock)


async def test_pending_never_answers() -> None:
    ib = make_fake_ib()
    ib.reqTickersAsync.side_effect = pending()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await ib.reqTickersAsync(stock())


def test_fake_clock_moves_every_view_together() -> None:
    clock = FakeClock()
    start = (clock.time(), clock.monotonic(), clock.now())
    clock.advance(5)
    assert clock.time() - start[0] == 5
    assert clock.monotonic() - start[1] == 5
    assert (clock.now() - start[2]).total_seconds() == 5
