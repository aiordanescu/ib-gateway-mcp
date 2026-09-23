"""MarketDataService: quote snapshots, market data type, streams and subscription management."""

from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from typing import Any, get_args
from unittest.mock import MagicMock

import pytest
from ib_async import (
    BarDataList,
    ComboLeg,
    Contract,
    Dividends,
    DOMLevel,
    FundamentalRatios,
    MktDepthData,
    RealTimeBar,
    RealTimeBarList,
    TickAttribBidAsk,
    TickAttribLast,
    TickByTickAllLast,
    TickByTickBidAsk,
    TickByTickMidPoint,
    TickData,
    Ticker,
)
from ib_async.wrapper import RequestError
from pydantic import BaseModel

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    ConfigurationError,
    IbApiError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RateLimitError,
    RequestTimeoutError,
    SubscriptionLimitError,
    SubscriptionNotFoundError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.common import BarSize, ComboLegSpec, ContractSpec, LiveBarSize
from ib_gateway_mcp.models.market_data import (
    DepthData,
    LiveBarsData,
    QuoteStreamData,
    RealtimeBarsData,
    TickByTickData,
)
from ib_gateway_mcp.safety import AUDIT_LOGGER_NAME
from ib_gateway_mcp.services import _pacing as pacing_module
from ib_gateway_mcp.services.market_data import MarketDataService
from ib_gateway_mcp.subscriptions import Stream
from tests.fakes import (
    FIXED_TIME,
    bar,
    contract_details,
    go_offline,
    option,
    option_computation,
    pending,
    raises,
    returns,
    stock,
    ticker,
)

AAPL = ContractSpec(symbol="AAPL")
MSFT = ContractSpec(symbol="MSFT")


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(request_timeout=0.2)


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MarketDataService, "settle_seconds", 0.02)


@pytest.fixture
def service(gateway: Gateway) -> MarketDataService:
    return gateway.market_data


def known(*contracts: Contract) -> Callable[..., Any]:
    """A ``reqContractDetailsAsync`` side effect that knows ``contracts`` by symbol/conId."""

    async def side_effect(request: Contract, *_args: Any, **_kwargs: Any) -> Any:
        for contract in contracts:
            if (request.conId and request.conId == contract.conId) or (
                request.symbol and request.symbol == contract.symbol
            ):
                return [contract_details(contract)]
        raise RequestError(9, 200, "No security definition has been found for the request")

    return side_effect


def ib_error(
    fake_ib: MagicMock,
    code: int,
    message: str = "",
    *,
    req_id: int = 42,
    contract: Contract | None = None,
) -> None:
    fake_ib.errorEvent.emit(req_id, code, message or f"error {code}", contract)


def error_soon(fake_ib: MagicMock, code: int, message: str = "", **kwargs: Any) -> None:
    """Emit an IB error on the next loop iteration, as if it came from the socket."""
    asyncio.get_running_loop().call_soon(lambda: ib_error(fake_ib, code, message, **kwargs))


def map_req_id(fake_ib: MagicMock, the_ticker: Ticker, request_key: str, req_id: int) -> None:
    """Expose the request id the way ib_async's wrapper keeps it."""
    mapping = getattr(fake_ib.wrapper, "ticker2ReqId", None)
    if not isinstance(mapping, dict):
        mapping = defaultdict(dict)
        fake_ib.wrapper.ticker2ReqId = mapping
    mapping[request_key][the_ticker] = req_id


def level1_update(the_ticker: Ticker, **fields: Any) -> None:
    """Deliver a level-1 update: set fields and emit ``updateEvent`` like the wrapper."""
    for name, value in fields.items():
        setattr(the_ticker, name, value)
    the_ticker.ticks = [TickData(FIXED_TIME, 1, the_ticker.bid, the_ticker.bidSize)]
    the_ticker.updateEvent.emit(the_ticker)


# --- get_quotes ------------------------------------------------------------------------------


async def test_quotes_snapshot(service: MarketDataService, fake_ib: MagicMock) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    fake_ib.reqTickersAsync.side_effect = returns([ticker(aapl, bboExchange="9c0001")])

    result = await service.quotes([AAPL])

    assert len(result.quotes) == 1
    quote = result.quotes[0]
    assert (quote.bid, quote.ask, quote.last, quote.close) == (99.5, 100.5, 100.0, 98.0)
    assert quote.contract.con_id == 265598
    assert quote.market_data_type == "live"
    assert quote.bbo_exchange == "9c0001"
    assert result.errors == []
    assert result.notices == []
    assert result.regulatory_snapshots == 0
    call = fake_ib.reqTickersAsync.call_args
    assert call.args[0].conId == 265598
    assert call.kwargs == {"regulatorySnapshot": False}


async def test_quotes_turn_nan_and_empty_sides_into_null(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    empty = ticker(
        aapl, bid=-1.0, bidSize=0.0, ask=math.nan, last=math.nan, close=math.nan, volume=math.inf
    )
    fake_ib.reqTickersAsync.side_effect = returns([empty])

    result = await service.quotes([AAPL])

    quote = result.quotes[0]
    assert quote.bid is None
    assert quote.bid_size == 0
    assert quote.ask is None
    assert quote.volume is None
    assert any("No prices for AAPL" in notice for notice in result.notices)


async def test_quotes_regulatory_snapshot_needs_the_operator_and_is_audited(
    service: MarketDataService,
    gateway: Gateway,
    fake_ib: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    fake_ib.reqTickersAsync.side_effect = returns([ticker(aapl)])
    with pytest.raises(ConfigurationError, match="IBKR_MCP_ALLOW_REGULATORY_SNAPSHOTS"):
        await service.quotes([AAPL], regulatory_snapshot=True)
    fake_ib.reqTickersAsync.assert_not_called()

    monkeypatch.setattr(gateway.settings, "allow_regulatory_snapshots", True)
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    result = await service.quotes([AAPL], regulatory_snapshot=True)

    assert fake_ib.reqTickersAsync.call_args.kwargs == {"regulatorySnapshot": True}
    assert result.regulatory_snapshots == 1
    assert any("USD 0.01" in notice for notice in result.notices)
    [entry] = [r.getMessage() for r in caplog.records if r.name == AUDIT_LOGGER_NAME]
    assert '"event": "regulatory_snapshot"' in entry
    assert '"count": 1' in entry


async def test_quotes_report_per_contract_failures(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    fake_ib.reqTickersAsync.side_effect = returns([ticker(aapl)])

    result = await service.quotes([AAPL, ContractSpec(symbol="NOPE")])

    assert [q.contract.symbol for q in result.quotes] == ["AAPL"]
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.code == "not_found"
    assert error.contract == "NOPE STK SMART USD"
    assert error.ib_error_code is None


async def test_quotes_raise_with_a_hint_when_nothing_succeeds(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = known(stock())
    fake_ib.reqTickersAsync.side_effect = raises(
        RequestError(5, 354, "Requested market data is not subscribed.")
    )

    with pytest.raises(IbApiError, match="set_market_data_type") as info:
        await service.quotes([AAPL])
    assert info.value.error_code == 354


async def test_quotes_list_ib_errors_with_their_code(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl, msft = stock(), stock("MSFT", 272093)
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl, msft)

    async def snapshot(contract: Contract, **_kwargs: Any) -> list[Ticker]:
        if contract.symbol == "MSFT":
            raise RequestError(6, 10090, "Part of requested market data is not subscribed.")
        return [ticker(contract)]

    fake_ib.reqTickersAsync.side_effect = snapshot

    result = await service.quotes([AAPL, MSFT])

    assert [q.contract.symbol for q in result.quotes] == ["AAPL"]
    assert result.errors[0].ib_error_code == 10090
    assert "subscribe_quotes keeps streaming" in result.errors[0].message


async def test_quotes_limits(service: MarketDataService) -> None:
    with pytest.raises(InvalidRequestError, match="at least one"):
        await service.quotes([])
    with pytest.raises(InvalidRequestError, match="At most 25"):
        await service.quotes([AAPL] * 26)


async def test_quotes_snapshot_each_contract_once(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    fake_ib.reqTickersAsync.side_effect = returns([ticker(aapl)])

    result = await service.quotes([AAPL, ContractSpec(con_id=265598)])

    assert len(result.quotes) == 2
    assert fake_ib.reqTickersAsync.await_count == 1


async def test_quotes_time_out(service: MarketDataService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = known(stock())
    fake_ib.reqTickersAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match="quote snapshot for AAPL"):
        await service.quotes([AAPL])


async def test_quotes_for_options_carry_greeks_and_delayed_notice(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    call = option()
    fake_ib.reqContractDetailsAsync.side_effect = known(call)
    snap = ticker(call, marketDataType=3, modelGreeks=option_computation())
    fake_ib.reqTickersAsync.side_effect = returns([snap])

    result = await service.quotes([ContractSpec(con_id=700001)])

    quote = result.quotes[0]
    assert quote.market_data_type == "delayed"
    assert quote.greeks is not None
    assert quote.greeks.delta == 0.52
    assert any("15-20 minutes" in notice for notice in result.notices)


async def test_quotes_need_a_connection(service: MarketDataService, fake_ib: MagicMock) -> None:
    go_offline(fake_ib)
    with pytest.raises(NotConnectedError):
        await service.quotes([AAPL])


async def test_quotes_are_answered_from_an_open_stream(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    live = ticker(aapl)
    fake_ib.reqMktData.return_value = live
    await service.subscribe_quotes(AAPL)
    level1_update(live, bid=101.0)

    result = await service.quotes([AAPL])

    assert result.quotes[0].bid == 101.0
    fake_ib.reqTickersAsync.assert_not_called()


# --- set_market_data_type ----------------------------------------------------------------------


async def test_set_market_data_type(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    result = service.set_market_data_type("delayed")
    assert (result.data_type, result.code, result.previous) == ("delayed", 3, "live")
    assert "no market depth" in result.note
    fake_ib.reqMarketDataType.assert_called_with(3)
    assert gateway.connection.market_data_type == 3


async def test_set_market_data_type_rejects_unknown_names(service: MarketDataService) -> None:
    with pytest.raises(InvalidRequestError, match="data_type must be one of"):
        service.set_market_data_type("realtime")  # type: ignore[arg-type]


# --- subscribe_quotes --------------------------------------------------------------------------


async def test_subscribe_quotes_streams_and_snapshots(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    live = ticker(aapl)
    fake_ib.reqMktData.return_value = live

    sub = await service.subscribe_quotes(AAPL)

    assert (sub.kind, sub.key, sub.deduplicated) == ("quotes", "265598", False)
    assert sub.contract is not None
    assert sub.contract.con_id == 265598
    assert sub.idle_ttl_s == gateway.settings.subscription_idle_ttl
    args = fake_ib.reqMktData.call_args.args
    assert (args[0].conId, args[1]) == (265598, "")

    level1_update(live, bid=99.75, halted=0.0)
    out = service.subscription_data(sub.subscription_id)
    data = QuoteStreamData.model_validate(out.data)
    assert data.quote.bid == 99.75
    assert data.quote.halted is False
    assert data.updates == 1
    assert data.active is True
    assert data.extras is None
    assert out.stale is False


async def test_subscribe_quotes_with_generic_ticks(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    live = ticker(aapl)
    fake_ib.reqMktData.return_value = live

    sub = await service.subscribe_quotes(AAPL, ["shortable", "misc_stats", "fundamental_ratios"])

    assert fake_ib.reqMktData.call_args.args[1] == "165,236,258"
    live.shortable = 3.0
    live.shortableShares = 25000.0
    live.high52week = 250.0
    live.avVolume = math.nan
    live.fundamentalRatios = FundamentalRatios(PEEXCLXOR=28.5, NPRICE=math.nan, CURRENCY="USD")
    live.dividends = Dividends(0.96, 1.0, FIXED_TIME.date(), 0.25)
    data = QuoteStreamData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert data.generic_ticks == ["misc_stats", "shortable", "fundamental_ratios"]
    assert data.extras is not None
    assert (data.extras.shortable, data.extras.shortable_shares) == (3.0, 25000.0)
    assert data.extras.high_52_week == 250.0
    assert data.extras.avg_volume is None
    assert data.extras.fundamental_ratios == {"PEEXCLXOR": 28.5, "NPRICE": None, "CURRENCY": "USD"}
    assert data.extras.dividends is not None
    assert data.extras.dividends.next_amount == 0.25


async def test_etf_nav_and_other_newer_generic_ticks(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    spy = stock("SPY", 756733)
    fake_ib.reqContractDetailsAsync.side_effect = known(spy)
    live = ticker(spy)
    fake_ib.reqMktData.return_value = live

    sub = await service.subscribe_quotes(
        ContractSpec(symbol="SPY"),
        ["etf_nav_last", "etf_nav_bid_ask", "short_term_volume", "last_rth_trade", "ipo_prices"],
    )

    assert fake_ib.reqMktData.call_args.args[1] == "318,576,577,586,595"
    live.etfNavLast = 501.25
    live.etfNavBid = 501.0
    live.etfNavAsk = 501.5
    live.volumeRate3Min = 1200.0
    live.volumeRate10Min = -1.0
    live.lastRthTrade = 500.75
    data = QuoteStreamData.model_validate(service.subscription_data(sub.subscription_id).data)
    extras = data.extras
    assert extras is not None
    assert (extras.etf_nav_last, extras.etf_nav_bid, extras.etf_nav_ask) == (501.25, 501.0, 501.5)
    assert (extras.volume_3_min, extras.volume_5_min, extras.volume_10_min) == (1200.0, None, None)
    assert extras.last_rth_trade == 500.75
    assert extras.final_ipo_last is None


async def test_subscribe_quotes_rejects_unknown_generic_ticks(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match=r"Unknown generic tick.*'news'"):
        await service.subscribe_quotes(AAPL, ["shortable", "news"])  # type: ignore[list-item]
    fake_ib.reqContractDetailsAsync.assert_not_called()
    fake_ib.reqMktData.assert_not_called()


async def test_quote_stream_ignores_updates_of_other_streams_on_the_ticker(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    shared = ticker(aapl)  # ib_async keeps one Ticker per contract
    fake_ib.reqMktData.return_value = shared
    fake_ib.reqMktDepth.return_value = shared
    sub = await service.subscribe_quotes(AAPL)
    await service.subscribe_market_depth(AAPL)

    shared.ticks = []
    shared.domTicks = [MktDepthData(FIXED_TIME, 0, "", 0, 1, 99.0, 100.0)]
    shared.updateEvent.emit(shared)  # a depth-only packet

    data = QuoteStreamData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert data.updates == 0
    # Not flowing yet, so a snapshot is still requested instead of reading the stream.
    fake_ib.reqTickersAsync.side_effect = returns([ticker(aapl)])
    await service.quotes([AAPL])
    fake_ib.reqTickersAsync.assert_awaited_once()

    shared.domTicks = []
    level1_update(shared, bid=99.25)
    data = QuoteStreamData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert data.updates == 1


async def test_subscribe_quotes_twice_reuses_the_stream(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = known(stock())
    fake_ib.reqMktData.return_value = ticker(stock())

    first = await service.subscribe_quotes(AAPL)
    second = await service.subscribe_quotes(ContractSpec(con_id=265598))

    assert second.subscription_id == first.subscription_id
    assert second.deduplicated is True
    assert fake_ib.reqMktData.call_count == 1


async def test_subscribe_quotes_widens_generic_ticks(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = known(stock())
    fake_ib.reqMktData.return_value = ticker(stock())

    first = await service.subscribe_quotes(AAPL, ["mark_price"])
    second = await service.subscribe_quotes(AAPL, ["shortable"])

    assert second.subscription_id == first.subscription_id
    fake_ib.cancelMktData.assert_called_once()
    assert fake_ib.reqMktData.call_args.args[1] == "221,236"
    assert gateway.subscriptions.get(first.subscription_id).meta["generic_ticks"] == [
        "mark_price",
        "shortable",
    ]


async def test_widening_rolls_back_when_ibkr_refuses(
    service: MarketDataService, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    live = ticker(aapl)
    fake_ib.reqMktData.return_value = live
    sub = await service.subscribe_quotes(AAPL, ["mark_price"])
    monkeypatch.setattr(MarketDataService, "settle_seconds", 1.0)

    def refuse(contract: Contract, ticks: str = "", *_args: Any) -> Ticker:
        if "236" in ticks:
            error_soon(fake_ib, 321, "Error validating request: invalid tick type", contract=aapl)
        return live

    fake_ib.reqMktData.side_effect = refuse

    with pytest.raises(IbApiError, match="generic tick"):
        await service.subscribe_quotes(AAPL, ["shortable"])

    assert fake_ib.reqMktData.call_args.args[1] == "221"
    data = QuoteStreamData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert data.active is True
    assert data.generic_ticks == ["mark_price"]


async def test_widening_on_a_dropped_socket_is_not_connected(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    """ib_async raises a bare ConnectionError; the caller must get not_connected."""
    fake_ib.reqContractDetailsAsync.side_effect = known(stock())
    fake_ib.reqMktData.return_value = ticker(stock())
    sub = await service.subscribe_quotes(AAPL, ["mark_price"])
    fake_ib.reqMktData.side_effect = ConnectionError("Not connected")

    with pytest.raises(NotConnectedError, match="previous ticks"):
        await service.subscribe_quotes(AAPL, ["shortable"])
    data = QuoteStreamData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert data.generic_ticks == ["mark_price"]
    assert gateway.subscriptions.get(sub.subscription_id).meta["generic_ticks"] == ["mark_price"]


async def test_subscribe_quotes_fails_clearly_without_permissions(
    service: MarketDataService,
    gateway: Gateway,
    fake_ib: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MarketDataService, "settle_seconds", 1.0)
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)

    def refuse(contract: Contract, *_args: Any) -> Ticker:
        error_soon(
            fake_ib,
            10089,
            "Requested market data requires additional subscription for API.",
            contract=aapl,
        )
        return ticker(contract)

    fake_ib.reqMktData.side_effect = refuse

    with pytest.raises(IbApiError, match="set_market_data_type") as info:
        await service.subscribe_quotes(AAPL)

    assert info.value.error_code == 10089
    # IB Gateway 10.45 answers 10089 with delayed data selected too: no promise of delayed data.
    assert "already selected" in str(info.value)
    fake_ib.cancelMktData.assert_called_once()
    assert len(gateway.subscriptions) == 0


async def test_line_limit_becomes_a_subscription_limit_error(
    service: MarketDataService, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MarketDataService, "settle_seconds", 1.0)
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)

    def refuse(contract: Contract, *_args: Any) -> Ticker:
        error_soon(fake_ib, 101, "Max number of tickers has been reached", contract=aapl)
        return ticker(contract)

    fake_ib.reqMktData.side_effect = refuse
    with pytest.raises(SubscriptionLimitError, match="market data lines"):
        await service.subscribe_quotes(AAPL)


async def test_stream_errors_match_the_request_id(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    live = ticker(aapl)

    def stream(*_args: Any) -> Ticker:
        map_req_id(fake_ib, live, "mktData", 77)
        return live

    fake_ib.reqMktData.side_effect = stream
    sub = await service.subscribe_quotes(AAPL)

    ib_error(fake_ib, 354, "not subscribed", req_id=78, contract=aapl)  # another request
    ib_error(fake_ib, 10167, "Displaying delayed market data.", req_id=77)
    data = QuoteStreamData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert data.active is True
    assert [n.code for n in data.notices] == [10167]
    assert data.notices[0].fatal is False
    assert data.notices[0].hint is not None

    ib_error(fake_ib, 10197, "No market data during competing live session", req_id=77)
    data = QuoteStreamData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert data.active is False
    assert data.error is not None
    assert data.error.code == 10197
    assert "competing live session" in (data.error.hint or "")


async def test_unsubscribe_cancels_the_quote_stream(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    live = ticker(aapl)
    fake_ib.reqMktData.return_value = live
    listeners = len(fake_ib.errorEvent)
    sub = await service.subscribe_quotes(AAPL)
    assert len(fake_ib.errorEvent) == listeners + 1
    level1_update(live)

    result = await service.unsubscribe(sub.subscription_id)

    assert [c.subscription_id for c in result.cancelled] == [sub.subscription_id]
    assert result.remaining == 0
    fake_ib.cancelMktData.assert_called_once_with(aapl)
    assert len(fake_ib.errorEvent) == listeners
    assert len(live.updateEvent) == 0
    # A later snapshot request goes to IBKR again (the stream no longer answers it).
    fake_ib.reqTickersAsync.side_effect = returns([ticker(aapl)])
    await service.quotes([AAPL])
    fake_ib.reqTickersAsync.assert_awaited_once()


async def test_quote_stream_follows_the_new_ticker_after_a_reconnect(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    old, new = ticker(aapl, bid=1.0), ticker(aapl, bid=2.0)
    fake_ib.reqMktData.return_value = old
    sub = await service.subscribe_quotes(AAPL)

    fake_ib.reqMktData.return_value = new
    assert await gateway.subscriptions.resubscribe_all() == 1

    level1_update(new, bid=2.5)
    level1_update(old, bid=9.9)  # the old ticker is no longer followed
    data = QuoteStreamData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert data.quote.bid == 2.5
    assert data.updates == 1
    assert fake_ib.reqMktData.call_count == 2


# --- subscribe_market_depth --------------------------------------------------------------------


async def test_subscribe_market_depth(service: MarketDataService, fake_ib: MagicMock) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    book = ticker(aapl)
    book.domBidsDict = {1: DOMLevel(99.0, 300.0, "ARCA"), 0: DOMLevel(99.5, 100.0, "NSDQ")}
    book.domAsks = [DOMLevel(100.5, 200.0, "")]
    fake_ib.reqMktDepth.return_value = book

    sub = await service.subscribe_market_depth(AAPL, rows=5, smart_depth=True)

    assert (sub.kind, sub.key) == ("depth", "265598")
    call = fake_ib.reqMktDepth.call_args
    assert call.kwargs == {"numRows": 5, "isSmartDepth": True}
    data = DepthData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert [(b.position, b.price, b.market_maker) for b in data.bids] == [
        (0, 99.5, "NSDQ"),
        (1, 99.0, "ARCA"),
    ]
    assert [(a.position, a.price, a.market_maker) for a in data.asks] == [(0, 100.5, None)]
    assert (data.rows, data.smart_depth) == (5, True)

    await service.unsubscribe(sub.subscription_id)
    fake_ib.cancelMktDepth.assert_called_once_with(aapl, isSmartDepth=True)


async def test_depth_streams_are_capped(
    service: MarketDataService, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MarketDataService, "max_depth_streams", 1)
    aapl, msft = stock(), stock("MSFT", 272093)
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl, msft)
    fake_ib.reqMktDepth.side_effect = lambda contract, **_kw: ticker(contract)

    await service.subscribe_market_depth(AAPL)
    again = await service.subscribe_market_depth(AAPL)  # the same book: no new stream
    assert again.deduplicated is True
    with pytest.raises(SubscriptionLimitError, match="1 market depth streams are open"):
        await service.subscribe_market_depth(MSFT)
    assert fake_ib.reqMktDepth.call_count == 1


async def test_depth_limit_error_from_ibkr(
    service: MarketDataService, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MarketDataService, "settle_seconds", 1.0)
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)

    def refuse(contract: Contract, **_kw: Any) -> Ticker:
        error_soon(fake_ib, 309, "Max number (3) of market depth requests reached", contract=aapl)
        return ticker(contract)

    fake_ib.reqMktDepth.side_effect = refuse
    with pytest.raises(SubscriptionLimitError, match="IB error 309"):
        await service.subscribe_market_depth(AAPL)
    fake_ib.cancelMktDepth.assert_called_once()


async def test_depth_and_ticks_need_live_data(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    service.set_market_data_type("delayed")
    with pytest.raises(InvalidRequestError, match="Market depth needs live market data"):
        await service.subscribe_market_depth(AAPL)
    with pytest.raises(InvalidRequestError, match="Tick-by-tick data needs live"):
        await service.subscribe_tick_by_tick(AAPL, "Last")
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_depth_rejects_bad_arguments(service: MarketDataService) -> None:
    with pytest.raises(InvalidRequestError, match="rows must be 1-50"):
        await service.subscribe_market_depth(AAPL, rows=0)
    combo = ContractSpec(
        symbol="AAPL",
        sec_type="BAG",
        combo_legs=[
            ComboLegSpec(con_id=1, action="BUY"),
            ComboLegSpec(con_id=2, action="SELL"),
        ],
    )
    with pytest.raises(InvalidRequestError, match="not available for combos"):
        await service.subscribe_market_depth(combo)


# --- subscribe_tick_by_tick --------------------------------------------------------------------


async def test_tick_by_tick_fills_a_ring_buffer(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    tape = ticker(aapl)
    fake_ib.reqTickByTickData.return_value = tape

    sub = await service.subscribe_tick_by_tick(AAPL, "AllLast", buffer_size=2)

    assert (sub.kind, sub.key) == ("tick_by_tick", "265598:AllLast")
    call = fake_ib.reqTickByTickData.call_args
    assert call.args[1] == "AllLast"
    assert call.kwargs == {"numberOfTicks": 0, "ignoreSize": False}

    attrib = TickAttribLast(pastLimit=False, unreported=True)
    later = FIXED_TIME + timedelta(seconds=1)
    tape.tickByTicks = [
        TickByTickAllLast(2, FIXED_TIME, 100.0, 10.0, attrib, "NYSE", "  "),
        TickByTickAllLast(1, FIXED_TIME, 100.1, 5.0, attrib, "NYSE", ""),  # a Last tick
        TickByTickMidPoint(FIXED_TIME, 100.2),  # another stream's tick
        TickByTickAllLast(2, later, 100.2, 20.0, attrib, "ARCA", "I"),
        TickByTickAllLast(2, later, -1.0, 0.0, attrib, "", ""),
    ]
    tape.updateEvent.emit(tape)

    data = TickByTickData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert data.received == 3
    assert data.buffer_size == 2
    assert [(t.price, t.size, t.exchange) for t in data.ticks] == [  # type: ignore[union-attr]
        (100.2, 20.0, "ARCA"),
        (None, 0.0, None),
    ]
    assert data.ticks[0].unreported is True  # type: ignore[union-attr]

    await service.unsubscribe(sub.subscription_id)
    fake_ib.cancelTickByTickData.assert_called_once_with(aapl, "AllLast")


async def test_tick_by_tick_bid_ask_and_midpoint(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    tape = ticker(aapl)
    fake_ib.reqTickByTickData.return_value = tape
    quotes = await service.subscribe_tick_by_tick(AAPL, "BidAsk", ignore_size=True)
    mids = await service.subscribe_tick_by_tick(AAPL, "MidPoint")
    assert quotes.subscription_id != mids.subscription_id

    tape.tickByTicks = [
        TickByTickBidAsk(FIXED_TIME, 99.9, 0.0, 300.0, 0.0, TickAttribBidAsk(bidPastLow=True)),
        TickByTickMidPoint(FIXED_TIME, math.nan),
    ]
    tape.updateEvent.emit(tape)

    bid_ask = TickByTickData.model_validate(service.subscription_data(quotes.subscription_id).data)
    tick = bid_ask.ticks[0]
    assert (tick.bid, tick.ask, tick.bid_size, tick.bid_past_low) == (  # type: ignore[union-attr]
        99.9,
        None,
        300.0,
        True,
    )
    mid = TickByTickData.model_validate(service.subscription_data(mids.subscription_id).data)
    assert mid.ticks[0].mid_point is None  # type: ignore[union-attr]


async def test_tick_by_tick_streams_are_capped(
    service: MarketDataService, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MarketDataService, "max_tick_by_tick_streams", 1)
    fake_ib.reqContractDetailsAsync.side_effect = known(stock())
    fake_ib.reqTickByTickData.return_value = ticker(stock())
    await service.subscribe_tick_by_tick(AAPL, "Last")
    with pytest.raises(SubscriptionLimitError, match="tick-by-tick"):
        await service.subscribe_tick_by_tick(AAPL, "BidAsk")


async def test_tick_by_tick_rejects_bad_arguments(service: MarketDataService) -> None:
    with pytest.raises(InvalidRequestError, match="tick_type"):
        await service.subscribe_tick_by_tick(AAPL, "Trades")  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError, match="buffer_size"):
        await service.subscribe_tick_by_tick(AAPL, "Last", buffer_size=5001)


# --- subscribe_realtime_bars -------------------------------------------------------------------


def realtime_list(contract: Contract, req_id: int = 11) -> RealTimeBarList:
    bars = RealTimeBarList()
    bars.reqId = req_id
    bars.contract = contract
    return bars


async def test_realtime_bars_move_into_the_ring_buffer(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    source = realtime_list(aapl)
    fake_ib.reqRealTimeBars.return_value = source

    sub = await service.subscribe_realtime_bars(AAPL, what_to_show="MIDPOINT", buffer_size=2)

    assert (sub.kind, sub.key) == ("realtime_bars", "265598:MIDPOINT:all")
    assert fake_ib.reqRealTimeBars.call_args.args[1:] == (5, "MIDPOINT", False)
    for second in range(3):
        source.append(
            RealTimeBar(FIXED_TIME + timedelta(seconds=5 * second), -1, 1, 2, 0.5, 1.5, -1, -1, -1)
        )
        source.updateEvent.emit(source, True)
    assert len(source) == 0  # consumed: ib_async's list no longer grows

    data = RealtimeBarsData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert [b.time for b in data.bars] == [
        FIXED_TIME + timedelta(seconds=5),
        FIXED_TIME + timedelta(seconds=10),
    ]
    assert (data.bars[0].open, data.bars[0].volume, data.bars[0].wap, data.bars[0].count) == (
        1.0,
        None,
        None,
        None,
    )

    await service.unsubscribe(sub.subscription_id)
    fake_ib.cancelRealTimeBars.assert_called_once_with(source)


async def test_realtime_bars_refused_by_ibkr(
    service: MarketDataService, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MarketDataService, "settle_seconds", 1.0)
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    source = realtime_list(aapl, req_id=11)

    def refuse(*_args: Any) -> RealTimeBarList:
        error_soon(fake_ib, 420, "Invalid Real-time Query", req_id=11)
        return source

    fake_ib.reqRealTimeBars.side_effect = refuse
    with pytest.raises(IbApiError, match="real-time bars request") as info:
        await service.subscribe_realtime_bars(AAPL)
    assert info.value.error_code == 420
    fake_ib.cancelRealTimeBars.assert_called_once_with(source)


async def test_realtime_bars_resubscribe_after_data_loss(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    old, new = realtime_list(aapl, 11), realtime_list(aapl, 12)
    fake_ib.reqRealTimeBars.return_value = old
    sub = await service.subscribe_realtime_bars(AAPL)
    old.append(RealTimeBar(FIXED_TIME, -1, 1, 2, 0.5, 1.5, 100, 1.2, 3))
    old.updateEvent.emit(old, True)

    fake_ib.realtimeBars.return_value = [old]  # 1101: still registered in ib_async
    fake_ib.reqRealTimeBars.return_value = new
    await gateway.subscriptions.resubscribe_all()

    fake_ib.cancelRealTimeBars.assert_called_once_with(old)
    new.append(RealTimeBar(FIXED_TIME + timedelta(seconds=5), -1, 2, 3, 1, 2.5, 50, 2.0, 1))
    new.updateEvent.emit(new, True)
    data = RealtimeBarsData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert [b.close for b in data.bars] == [1.5, 2.5]


async def test_realtime_bars_reject_bad_arguments(service: MarketDataService) -> None:
    with pytest.raises(InvalidRequestError, match="what_to_show"):
        await service.subscribe_realtime_bars(AAPL, what_to_show="BID_ASK")  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError, match="buffer_size"):
        await service.subscribe_realtime_bars(AAPL, buffer_size=0)


# --- subscribe_bars ----------------------------------------------------------------------------


def bar_list(contract: Contract, *bars: Any, req_id: int = 12) -> BarDataList:
    bars_list = BarDataList(bars)
    bars_list.reqId = req_id
    bars_list.contract = contract
    return bars_list


async def test_subscribe_bars_backfills_and_updates(
    service: MarketDataService, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MarketDataService, "bars_kept", 2)
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    source = bar_list(aapl, bar(FIXED_TIME, 100.0), bar(FIXED_TIME + timedelta(minutes=5), 101.0))
    fake_ib.reqHistoricalDataAsync.side_effect = returns(source)

    sub = await service.subscribe_bars(AAPL, "5 mins", duration=" 2  d ", use_rth=False)

    assert (sub.kind, sub.key) == ("bars", "265598:5 mins:2 D:TRADES:all")
    call = fake_ib.reqHistoricalDataAsync.call_args
    assert call.args[1:] == ("", "2 D", "5 mins", "TRADES", False)
    assert call.kwargs == {"formatDate": 2, "keepUpToDate": True, "timeout": 0}

    source.append(bar(FIXED_TIME + timedelta(minutes=10), 102.0, volume=-1, average=-1))
    source.updateEvent.emit(source, True)
    assert len(source) == 2  # trimmed to bars_kept

    data = LiveBarsData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert [b.close for b in data.bars] == [101.0, 102.0]
    assert (data.bars[-1].volume, data.bars[-1].average) == (None, None)
    assert (data.bar_size, data.duration, data.use_rth) == ("5 mins", "2 D", False)

    await service.unsubscribe(sub.subscription_id)
    fake_ib.cancelHistoricalData.assert_called_once_with(source)


async def test_realtime_bars_share_the_history_pacing_budget(
    service: MarketDataService, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pacing_module, "PACING_BURST_MAX", 1)
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    fake_ib.reqRealTimeBars.return_value = realtime_list(aapl)

    await service.subscribe_realtime_bars(AAPL)
    with pytest.raises(RateLimitError, match="same contract"):
        await service.subscribe_realtime_bars(AAPL, use_rth=True)
    assert fake_ib.reqRealTimeBars.call_count == 1  # refused before anything was sent


async def test_small_bar_backfills_are_paced_and_larger_ones_are_not(
    service: MarketDataService, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pacing_module, "PACING_BURST_MAX", 1)
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)

    async def backfill(contract: Contract, *_args: Any, **_kwargs: Any) -> BarDataList:
        return bar_list(contract, bar(FIXED_TIME, 100.0))

    fake_ib.reqHistoricalDataAsync.side_effect = backfill

    await service.subscribe_bars(AAPL, "30 secs", use_rth=False)
    with pytest.raises(RateLimitError, match="same contract"):
        await service.subscribe_bars(AAPL, "30 secs", use_rth=True)
    assert fake_ib.reqHistoricalDataAsync.call_count == 1
    # Bars over 30 seconds are not paced this way.
    await service.subscribe_bars(AAPL, "5 mins")
    await service.subscribe_bars(AAPL, "5 mins", use_rth=False)
    assert fake_ib.reqHistoricalDataAsync.call_count == 3


async def test_a_loading_backfill_does_not_block_other_subscriptions(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    fake_ib.reqMktData.return_value = ticker(aapl)
    answer: asyncio.Future[BarDataList] = asyncio.get_running_loop().create_future()

    async def backfill(*_args: Any, **_kwargs: Any) -> BarDataList:
        return await answer

    fake_ib.reqHistoricalDataAsync.side_effect = backfill
    loading = asyncio.create_task(service.subscribe_bars(AAPL, "1 hour"))
    await asyncio.sleep(0.01)
    quotes = await asyncio.wait_for(service.subscribe_quotes(AAPL), timeout=0.15)
    assert quotes.kind == "quotes"
    assert gateway.ops.health().subscriptions_used == 1  # the loading backfill is not listed
    answer.set_result(bar_list(aapl, bar(FIXED_TIME, 100.0)))
    bars = await loading
    assert bars.kind == "bars"
    assert len(gateway.subscriptions) == 2


async def test_a_backfill_waits_for_a_free_historical_request_slot(
    service: MarketDataService,
    gateway: Gateway,
    fake_ib: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    fake_ib.reqHistoricalDataAsync.side_effect = returns(bar_list(aapl, bar(FIXED_TIME, 100.0)))
    monkeypatch.setattr(gateway.pacing, "_open_requests", asyncio.Semaphore(0))  # all busy

    with pytest.raises(RequestTimeoutError):
        await service.subscribe_bars(AAPL, "1 hour")
    fake_ib.reqHistoricalDataAsync.assert_not_called()
    assert len(gateway.subscriptions) == 0


async def test_subscribe_bars_refuses_an_empty_backfill(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    source = bar_list(aapl)
    fake_ib.reqHistoricalDataAsync.side_effect = returns(source)

    with pytest.raises(NotFoundError, match="longer duration"):
        await service.subscribe_bars(AAPL, "1 min")

    fake_ib.cancelHistoricalData.assert_called_once_with(source)
    assert len(gateway.subscriptions) == 0


async def test_subscribe_bars_timeout_cancels_the_orphaned_request(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = known(stock())
    registered: list[BarDataList] = []
    unrelated = bar_list(stock("MSFT", 272093))
    fake_ib.realtimeBars.side_effect = lambda: [unrelated, *registered]

    async def backfill(contract: Contract, *_args: Any, **_kwargs: Any) -> BarDataList:
        registered.append(bar_list(contract))  # ib_async registers it before sending
        never: asyncio.Future[BarDataList] = asyncio.get_running_loop().create_future()
        return await never

    fake_ib.reqHistoricalDataAsync.side_effect = backfill

    with pytest.raises(RequestTimeoutError):
        await service.subscribe_bars(AAPL, "1 hour")

    fake_ib.cancelHistoricalData.assert_called_once_with(registered[0])


async def test_subscribe_bars_fails_fast_when_ibkr_rejects_the_request(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    # 321 is a warning to ib_async, so the backfill would wait out the timeout.
    fake_ib.reqContractDetailsAsync.side_effect = known(stock())
    registered: list[BarDataList] = []
    fake_ib.realtimeBars.side_effect = lambda: list(registered)
    listeners = len(fake_ib.errorEvent)

    async def backfill(contract: Contract, *_args: Any, **_kwargs: Any) -> BarDataList:
        registered.append(bar_list(contract))
        error_soon(fake_ib, 321, "Error validating request: invalid bar size", contract=contract)
        never: asyncio.Future[BarDataList] = asyncio.get_running_loop().create_future()
        return await never

    fake_ib.reqHistoricalDataAsync.side_effect = backfill

    with pytest.raises(IbApiError, match="invalid bar size") as info:
        await service.subscribe_bars(AAPL, "1 min")

    assert info.value.error_code == 321
    fake_ib.cancelHistoricalData.assert_called_once_with(registered[0])
    assert len(fake_ib.errorEvent) == listeners
    assert len(gateway.subscriptions) == 0


async def test_bar_backfill_ignores_errors_of_other_requests_for_the_contract(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    source = bar_list(aapl, bar(FIXED_TIME, 100.0))

    async def backfill(contract: Contract, *_args: Any, **_kwargs: Any) -> BarDataList:
        # A snapshot of the same instrument fails meanwhile: same conId, other request.
        ib_error(fake_ib, 354, "not subscribed", req_id=99, contract=stock())
        await asyncio.sleep(0)
        return source

    fake_ib.reqHistoricalDataAsync.side_effect = backfill

    sub = await service.subscribe_bars(AAPL, "1 hour")

    data = LiveBarsData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert data.active is True
    assert data.notices == []


async def test_a_failed_resubscribe_releases_the_stream(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    listeners = len(fake_ib.errorEvent)
    old = bar_list(aapl, bar(FIXED_TIME, 100.0))
    fake_ib.reqHistoricalDataAsync.side_effect = returns(old)
    sub = await service.subscribe_bars(AAPL, "1 hour")
    assert len(fake_ib.errorEvent) == listeners + 1

    fake_ib.realtimeBars.return_value = []  # a reconnect
    fake_ib.reqHistoricalDataAsync.side_effect = raises(
        RequestError(13, 162, "Historical Market Data Service error message:pacing violation")
    )
    assert await gateway.subscriptions.resubscribe_all() == 0

    assert len(fake_ib.errorEvent) == listeners
    assert len(old.updateEvent) == 0
    with pytest.raises(SubscriptionNotFoundError):
        service.subscription_data(sub.subscription_id)


async def test_subscribe_bars_errors_carry_a_hint(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = known(stock())
    fake_ib.reqHistoricalDataAsync.side_effect = raises(
        RequestError(12, 162, "Historical Market Data Service error message:pacing violation")
    )
    with pytest.raises(IbApiError, match="pacing violation") as info:
        await service.subscribe_bars(AAPL, "1 min")
    assert info.value.error_code == 162
    assert "wait a minute" in str(info.value)


def test_live_bar_sizes_are_the_bar_sizes_but_one_second() -> None:
    assert list(get_args(LiveBarSize)) == [s for s in get_args(BarSize) if s != "1 secs"]


async def test_subscribe_bars_rejects_bad_arguments(service: MarketDataService) -> None:
    with pytest.raises(InvalidRequestError, match="5 secs or more"):
        await service.subscribe_bars(AAPL, "1 secs")  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError, match="duration must look like"):
        await service.subscribe_bars(AAPL, "1 min", duration="1 day")
    with pytest.raises(InvalidRequestError, match="one of 5 secs, 10 secs"):
        await service.subscribe_bars(AAPL, "7 mins")  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError, match="what_to_show must be one of"):
        await service.subscribe_bars(AAPL, "1 min", what_to_show="LAST")  # type: ignore[arg-type]


async def test_subscribe_bars_resubscribes_after_a_reconnect(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    old = bar_list(aapl, bar(FIXED_TIME, 100.0))
    new = bar_list(aapl, bar(FIXED_TIME, 100.0), bar(FIXED_TIME + timedelta(hours=1), 105.0))
    fake_ib.reqHistoricalDataAsync.side_effect = returns(old)
    sub = await service.subscribe_bars(AAPL, "1 hour")

    fake_ib.realtimeBars.return_value = []  # a reconnect: ib_async forgot the old list
    fake_ib.reqHistoricalDataAsync.side_effect = returns(new)
    assert await gateway.subscriptions.resubscribe_all() == 1

    fake_ib.cancelHistoricalData.assert_not_called()
    data = LiveBarsData.model_validate(service.subscription_data(sub.subscription_id).data)
    assert [b.close for b in data.bars] == [100.0, 105.0]


# --- list_subscriptions / subscription_data / unsubscribe ---------------------------------------


class Headline(BaseModel):
    time: str
    headline: str


class OtherKindSnapshot(BaseModel):
    rows: list[dict[str, int]]
    headlines: list[Headline]


async def other_kind(service: MarketDataService, count: int = 5) -> str:
    """Register a stream of another toolset's kind, with a time series and plain rows."""
    snapshot = OtherKindSnapshot(
        rows=[{"rank": i} for i in range(count)],
        headlines=[
            Headline(time=(FIXED_TIME + timedelta(minutes=i)).isoformat(), headline=f"h{i}")
            for i in range(count)
        ],
    )
    info = await service.subs.add(
        "news",
        "BRFG",
        opener=lambda: Stream(cancel=lambda: None, snapshot=lambda: snapshot),
        meta={"provider": "BRFG", "handle": object()},
    )
    return info.id


async def test_list_subscriptions(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = known(stock())
    fake_ib.reqMktData.return_value = ticker(stock())
    fake_ib.reqMktDepth.return_value = ticker(stock())
    quotes = await service.subscribe_quotes(AAPL, ["mark_price"])
    await service.subscribe_market_depth(AAPL)
    await other_kind(service)

    listing = service.list_subscriptions()

    assert listing.used == 3
    assert listing.max == gateway.settings.max_subscriptions
    assert (listing.depth_used, listing.depth_max) == (1, 3)
    assert (listing.tick_by_tick_used, listing.tick_by_tick_max) == (0, 3)
    assert listing.market_data_type == "live"
    first = listing.subscriptions[0]
    assert first.subscription_id == quotes.subscription_id
    assert first.params == {"generic_ticks": ["mark_price"]}
    assert first.contract is not None
    assert first.idle_expires_at == first.created_at + timedelta(
        seconds=gateway.settings.subscription_idle_ttl
    )
    assert listing.subscriptions[2].params == {"provider": "BRFG"}  # non-JSON values dropped


async def test_subscription_data_windows_time_series_of_any_kind(
    service: MarketDataService,
) -> None:
    sub_id = await other_kind(service)

    out = service.subscription_data(sub_id, limit=2)
    assert out.kind == "news"
    assert [h["headline"] for h in out.data["headlines"]] == ["h3", "h4"]
    assert len(out.data["rows"]) == 5  # not a time series: left alone
    assert out.data["truncated"] is True
    assert out.last_read_at is None

    since = FIXED_TIME + timedelta(minutes=2)
    later = service.subscription_data(sub_id, since=since)
    assert [h["headline"] for h in later.data["headlines"]] == ["h3", "h4"]
    assert "truncated" not in later.data
    assert later.last_read_at is not None


async def test_subscription_data_unknown_id(service: MarketDataService) -> None:
    with pytest.raises(SubscriptionNotFoundError):
        service.subscription_data("quotes-999")


async def test_unsubscribe_variants(service: MarketDataService, gateway: Gateway) -> None:
    first = await other_kind(service)
    with pytest.raises(InvalidRequestError, match="either subscription_id"):
        await service.unsubscribe()
    with pytest.raises(InvalidRequestError, match="either subscription_id"):
        await service.unsubscribe(first, all_subscriptions=True)
    with pytest.raises(SubscriptionNotFoundError):
        await service.unsubscribe("quotes-999")

    result = await service.unsubscribe(all_subscriptions=True)
    assert [c.subscription_id for c in result.cancelled] == [first]
    assert (result.cancelled[0].kind, result.remaining) == ("news", 0)
    assert len(gateway.subscriptions) == 0


async def test_unsubscribe_all_cancels_every_kind_at_ibkr(
    service: MarketDataService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    fake_ib.reqMktData.return_value = ticker(aapl)
    source = realtime_list(aapl)
    fake_ib.reqRealTimeBars.return_value = source
    quotes = await service.subscribe_quotes(AAPL)
    bars = await service.subscribe_realtime_bars(AAPL)
    news = await other_kind(service)

    result = await service.unsubscribe_all()

    assert [c.subscription_id for c in result.cancelled] == [
        quotes.subscription_id,
        bars.subscription_id,
        news,
    ]
    assert result.remaining == 0
    assert len(gateway.subscriptions) == 0
    fake_ib.cancelMktData.assert_called_once()
    fake_ib.cancelRealTimeBars.assert_called_once_with(source)
    assert (await service.unsubscribe_all()).cancelled == []


async def test_stream_yields_changes_until_the_subscription_ends(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    live = ticker(aapl)
    fake_ib.reqMktData.return_value = live
    sub = await service.subscribe_quotes(AAPL)

    seen: list[float | None] = []
    async for update in service.stream(sub.subscription_id, interval=0.01):
        seen.append(update.data["quote"]["bid"])
        if len(seen) == 1:
            level1_update(live, bid=101.25)
        elif len(seen) == 2:
            await service.unsubscribe(sub.subscription_id)
    assert seen[1] == 101.25
    assert len(seen) == 2  # unchanged polls in between were not yielded


async def test_stream_refuses_unknown_ids_and_bad_intervals(service: MarketDataService) -> None:
    with pytest.raises(SubscriptionNotFoundError):
        await anext(service.stream("quotes-999"))
    with pytest.raises(InvalidRequestError, match="interval"):
        await anext(service.stream("quotes-999", interval=0))


async def test_stream_can_yield_every_poll(service: MarketDataService) -> None:
    sub_id = await other_kind(service)
    updates = service.stream(sub_id, interval=0.01, only_changes=False)
    first = await anext(updates)
    second = await anext(updates)
    assert first.data == second.data
    assert second.last_read_at is not None  # each poll counts as a read
    await updates.aclose()


async def test_snapshot_returns_the_typed_model_and_refuses_another(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    live = ticker(aapl)
    fake_ib.reqMktData.return_value = live
    sub = await service.subscribe_quotes(AAPL)
    level1_update(live, bid=101.25)

    data = service.snapshot(sub.subscription_id, QuoteStreamData)
    assert data.quote.bid == 101.25
    with pytest.raises(InvalidRequestError, match="streams quotes, whose snapshot is a Quote"):
        service.snapshot(sub.subscription_id, DepthData)
    with pytest.raises(SubscriptionNotFoundError):
        service.snapshot("quotes-999", QuoteStreamData)


async def test_watch_yields_typed_changes_until_the_subscription_ends(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    aapl = stock()
    fake_ib.reqContractDetailsAsync.side_effect = known(aapl)
    live = ticker(aapl)
    fake_ib.reqMktData.return_value = live
    sub = await service.subscribe_quotes(AAPL)

    seen: list[float | None] = []
    async for data in service.watch(sub.subscription_id, QuoteStreamData, interval=0.01):
        seen.append(data.quote.bid)
        if len(seen) == 1:
            level1_update(live, bid=101.25)
        elif len(seen) == 2:
            await service.unsubscribe(sub.subscription_id)
    assert seen[1] == 101.25
    assert len(seen) == 2


async def test_combo_quotes_are_keyed_by_their_legs(
    service: MarketDataService, fake_ib: MagicMock
) -> None:
    combo = ContractSpec(
        symbol="AAPL",
        sec_type="BAG",
        combo_legs=[
            ComboLegSpec(con_id=700002, action="SELL"),
            ComboLegSpec(con_id=700001, action="BUY"),
        ],
    )
    bag = Contract(
        secType="BAG",
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
        comboLegs=[ComboLeg(conId=700001, ratio=1, action="BUY", exchange="SMART")],
    )
    fake_ib.reqMktData.return_value = ticker(bag)

    sub = await service.subscribe_quotes(combo)

    assert sub.key == "BAG:AAPL:SMART:700001x1BUY@SMART,700002x1SELL@SMART"
    fake_ib.reqContractDetailsAsync.assert_not_called()
