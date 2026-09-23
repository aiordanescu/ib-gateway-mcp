"""Test doubles for ib_async. Nothing here touches the network.

:func:`make_fake_ib` returns an autospecced ``ib_async.IB`` (see :func:`lazy_autospec`):
every method exists with its real signature, so a service calling ib_async wrongly fails
the test, and an attribute ib_async does not have raises ``AttributeError``.
Real ``eventkit.Event`` objects stand in for the IB events, so services can subscribe
and tests can ``emit``.

ib_async's ``*Async`` requests come in two shapes: plain ``def`` methods that return a
future (``reqCurrentTimeAsync``, ``reqContractDetailsAsync``...), which autospec turns
into ``MagicMock``, and ``async def`` methods (``qualifyContractsAsync``,
``reqHistoricalDataAsync``, ``reqTickersAsync``...), which become ``AsyncMock``. The
side effects :func:`returns`, :func:`raises` and :func:`pending` work for both, because
they are ``async def`` functions: an ``AsyncMock`` awaits them, and a ``MagicMock``
returns the coroutine for the service to await::

    fake_ib.reqCurrentTimeAsync.side_effect = returns(datetime(2026, 1, 2, tzinfo=UTC))
    fake_ib.qualifyContractsAsync.side_effect = returns([stock()])
    fake_ib.reqContractDetailsAsync.side_effect = raises(RequestError(7, 200, "No security"))

:class:`FakeClock` drives the safety rails and the subscription registry in tests.
The builders at the bottom create ib_async objects with sensible defaults.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Coroutine, Iterable
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from types import FunctionType, MethodType
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, NonCallableMagicMock

from eventkit import Event
from ib_async import (
    IB,
    AccountValue,
    BarData,
    Client,
    CommissionReport,
    Contract,
    ContractDetails,
    Execution,
    Fill,
    Future,
    HistoricalNews,
    NewsArticle,
    Option,
    OptionComputation,
    Order,
    OrderState,
    OrderStatus,
    PortfolioItem,
    Position,
    ScanData,
    Stock,
    Ticker,
    Trade,
)
from ib_async.objects import ConnectionStats
from ib_async.wrapper import Wrapper

PAPER_ACCOUNT = "DU1234567"
"""Placeholder paper account id."""
LIVE_ACCOUNT = "U1234567"
"""Placeholder live account id."""
SERVER_VERSION = 178
FIXED_TIME = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)


# --- lazy autospec --------------------------------------------------------------------------


class _LazyAutospec(NonCallableMagicMock):
    """``create_autospec(cls, instance=True)``, but each attribute is specced on first use.

    ``create_autospec`` builds and signature-checks a mock for every method of the class
    up front: about 90 ms for ``IB``, ``Client`` and ``Wrapper`` together, paid by every
    test. This builds the same child the first time a test or service touches it: an
    ``AsyncMock`` for ``async def`` methods, a ``MagicMock`` otherwise, both checking
    the real signature; other attributes are autospecced as ``create_autospec`` would.
    """

    def _get_child_mock(self, /, **kw: Any) -> Any:
        name = kw.get("name")
        spec_class = self.__dict__.get("_spec_class")
        if spec_class is None or not isinstance(name, str):
            return MagicMock(**kw)
        original = getattr(spec_class, name)
        parent = kw["parent"]
        if not isinstance(original, FunctionType | MethodType):
            return mock.create_autospec(original, instance=True, _parent=parent, _name=name)
        skip_self = mock._must_skip(spec_class, name, True)  # type: ignore[attr-defined]
        klass = AsyncMock if inspect.iscoroutinefunction(original) else MagicMock
        child = klass(
            parent=parent,
            name=name,
            _new_name=name,
            _new_parent=parent,
            spec=original,
            _eat_self=skip_self,
        )
        child.return_value = klass()
        mock._check_signature(original, child, skipfirst=skip_self)  # type: ignore[attr-defined]
        return child


def lazy_autospec(cls: type) -> MagicMock:
    """An instance mock of ``cls`` with real method signatures, built on demand."""
    return _LazyAutospec(spec=cls)


# --- the fake IB --------------------------------------------------------------------------


def make_fake_ib(
    accounts: Iterable[str] = (PAPER_ACCOUNT,),
    *,
    server_version: int = SERVER_VERSION,
    connected: bool = True,
) -> MagicMock:
    """Build an autospecced ``IB`` whose ``connectAsync`` succeeds immediately.

    ``isConnected()`` tracks ``connectAsync``/``disconnect``; ``disconnect`` emits
    ``disconnectedEvent`` like the real one. ``managedAccounts()`` returns ``accounts``.
    """
    ib = lazy_autospec(IB)
    for name in IB.events:
        setattr(ib, name, Event(name))

    ib.client = lazy_autospec(Client)
    ib.client.serverVersion.return_value = server_version
    ib.client.isReady.return_value = True
    ib.client.connectionStats.return_value = ConnectionStats(
        startTime=FIXED_TIME.timestamp(),
        duration=12.5,
        numBytesRecv=2048,
        numBytesSent=1024,
        numMsgRecv=40,
        numMsgSent=20,
    )
    ib.wrapper = lazy_autospec(Wrapper)
    ib.RaiseRequestErrors = False

    state = {"connected": connected}

    async def connect_async(*_args: Any, **_kwargs: Any) -> MagicMock:
        state["connected"] = True
        ib.connectedEvent.emit()
        return ib

    def disconnect() -> None:
        was_connected = state["connected"]
        state["connected"] = False
        if was_connected:
            ib.disconnectedEvent.emit()

    ib.connectAsync.side_effect = connect_async
    ib.disconnect.side_effect = disconnect
    ib.isConnected.side_effect = lambda: state["connected"]
    ib.managedAccounts.return_value = list(accounts)
    # Startup order sync (trading profiles) resolves to "no orders", and the executions
    # fetched after it (read-only connects) to none.
    ib.reqOpenOrdersAsync.side_effect = returns([])
    ib.reqCompletedOrdersAsync.side_effect = returns([])
    ib.reqExecutionsAsync.side_effect = returns([])
    return ib


AsyncSideEffect = Callable[..., Coroutine[Any, Any, Any]]


def returns(value: Any) -> AsyncSideEffect:
    """Side effect for an ``*Async`` mock (either shape): the request answers ``value``."""

    async def side_effect(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return side_effect


def raises(exc: BaseException) -> AsyncSideEffect:
    """Side effect for an ``*Async`` mock (either shape): the request fails with ``exc``."""

    async def side_effect(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return side_effect


def pending() -> AsyncSideEffect:
    """Side effect for an ``*Async`` mock that never answers (for timeout tests)."""

    async def side_effect(*_args: Any, **_kwargs: Any) -> Any:
        return await asyncio.get_running_loop().create_future()

    return side_effect


class FakeClock:
    """One settable clock for every clock convention the library uses.

    ``time()`` (epoch seconds) and ``monotonic()`` feed
    ``SafetyRails.from_settings(settings, clock=c.time, monotonic=c.monotonic)``;
    ``now()`` (aware UTC datetime) feeds ``SubscriptionRegistry(settings, clock=c.now)``.
    """

    def __init__(self, start: datetime = FIXED_TIME) -> None:
        self._now = start
        self._monotonic = 1000.0

    def time(self) -> float:
        """Epoch seconds."""
        return self._now.timestamp()

    def monotonic(self) -> float:
        """Monotonic seconds."""
        return self._monotonic

    def now(self) -> datetime:
        """The current time as an aware UTC datetime."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move every view of the clock forward."""
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


def drop_connection(ib: MagicMock) -> None:
    """Simulate the gateway closing the socket."""
    ib.disconnect()


def go_offline(ib: MagicMock) -> None:
    """Drop the connection and make every reconnect attempt fail (gateway down)."""
    ib.connectAsync.side_effect = ConnectionRefusedError(61, "Connection refused")
    ib.disconnect()


def emit_error(ib: MagicMock, code: int, message: str = "", *, req_id: int = -1) -> None:
    """Emit ``errorEvent`` the way ib_async does: ``(reqId, code, message, contract)``."""
    ib.errorEvent.emit(req_id, code, message or f"error {code}", None)


# --- ib_async object builders -------------------------------------------------------------


def stock(symbol: str = "AAPL", con_id: int = 265598, **kwargs: Any) -> Stock:
    """A qualified-looking US stock."""
    fields: dict[str, Any] = {
        "conId": con_id,
        "primaryExchange": "NASDAQ",
        "localSymbol": symbol,
        "tradingClass": "NMS",
    }
    fields.update(kwargs)
    return Stock(symbol, "SMART", "USD", **fields)


def option(
    symbol: str = "AAPL",
    expiry: str = "20261218",
    strike: float = 200.0,
    right: str = "C",
    con_id: int = 700001,
    **kwargs: Any,
) -> Option:
    """A US equity option."""
    fields: dict[str, Any] = {"conId": con_id, "multiplier": "100", "currency": "USD"}
    fields.update(kwargs)
    return Option(symbol, expiry, strike, right, "SMART", **fields)


def future(
    symbol: str = "ES", month: str = "202612", con_id: int = 800001, **kwargs: Any
) -> Future:
    """A CME future."""
    fields: dict[str, Any] = {"conId": con_id, "multiplier": "50", "currency": "USD"}
    fields.update(kwargs)
    return Future(symbol, month, "CME", **fields)


def contract_details(contract: Contract | None = None, **kwargs: Any) -> ContractDetails:
    """Contract details for ``contract`` (default: :func:`stock`)."""
    fields: dict[str, Any] = {
        "contract": contract or stock(),
        "marketName": "NMS",
        "minTick": 0.01,
        "longName": "APPLE INC",
        "validExchanges": "SMART,NASDAQ,NYSE",
        "timeZoneId": "US/Eastern",
    }
    fields.update(kwargs)
    return ContractDetails(**fields)


def ticker(contract: Contract | None = None, **kwargs: Any) -> Ticker:
    """A ticker with a two-sided quote and a last trade.

    ``Ticker.__post_init__`` resets every price field to NaN, so the values are set on
    the instance after construction, the way ib_async's wrapper fills them in.
    """
    fields: dict[str, Any] = {
        "time": FIXED_TIME,
        "bid": 99.5,
        "bidSize": 100.0,
        "ask": 100.5,
        "askSize": 200.0,
        "last": 100.0,
        "lastSize": 10.0,
        "close": 98.0,
    }
    fields.update(kwargs)
    the_ticker = Ticker(contract=contract or stock())
    for name, value in fields.items():
        if not hasattr(the_ticker, name):
            raise AttributeError(f"Ticker has no field {name!r}")
        setattr(the_ticker, name, value)
    return the_ticker


def option_computation(**kwargs: Any) -> OptionComputation:
    """IBKR model greeks for an at-the-money call."""
    fields: dict[str, Any] = {
        "tickAttrib": 0,
        "impliedVol": 0.25,
        "delta": 0.52,
        "optPrice": 12.5,
        "pvDividend": 0.8,
        "gamma": 0.015,
        "vega": 0.45,
        "theta": -0.06,
        "undPrice": 200.0,
    }
    fields.update(kwargs)
    return OptionComputation(**fields)


def bar(date: date_type = FIXED_TIME, close: float = 100.0, **kwargs: Any) -> BarData:
    """One OHLCV bar (a ``date`` for daily and longer bars, a ``datetime`` otherwise)."""
    fields: dict[str, Any] = {
        "date": date,
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": 1000.0,
        "average": close,
        "barCount": 10,
    }
    fields.update(kwargs)
    return BarData(**fields)


def position(
    account: str = PAPER_ACCOUNT,
    contract: Contract | None = None,
    qty: float = 10,
    avg_cost: float = 95,
) -> Position:
    """A position row."""
    return Position(account, contract or stock(), qty, avg_cost)


def portfolio_item(
    account: str = PAPER_ACCOUNT, contract: Contract | None = None, qty: float = 10, **kwargs: Any
) -> PortfolioItem:
    """A portfolio row with market value and P&L."""
    fields: dict[str, Any] = {
        "contract": contract or stock(),
        "position": qty,
        "marketPrice": 100.0,
        "marketValue": 100.0 * qty,
        "averageCost": 95.0,
        "unrealizedPNL": 5.0 * qty,
        "realizedPNL": 0.0,
        "account": account,
    }
    fields.update(kwargs)
    return PortfolioItem(**fields)


def account_value(
    tag: str = "NetLiquidation",
    value: str = "100000",
    account: str = PAPER_ACCOUNT,
    currency: str = "USD",
) -> AccountValue:
    """An account value row."""
    return AccountValue(account, tag, value, currency, "")


def execution(account: str = PAPER_ACCOUNT, **kwargs: Any) -> Execution:
    """An execution (fill details)."""
    fields: dict[str, Any] = {
        "execId": "0001f4e8.0001.01.01",
        "time": FIXED_TIME,
        "acctNumber": account,
        "exchange": "NASDAQ",
        "side": "BOT",
        "shares": 10.0,
        "price": 100.0,
        "permId": 123456789,
        "clientId": 80,
        "orderId": 7,
        "cumQty": 10.0,
        "avgPrice": 100.0,
    }
    fields.update(kwargs)
    return Execution(**fields)


def commission_report(**kwargs: Any) -> CommissionReport:
    """A commission report matching :func:`execution`."""
    fields: dict[str, Any] = {
        "execId": "0001f4e8.0001.01.01",
        "commission": 1.0,
        "currency": "USD",
        "realizedPNL": 0.0,
    }
    fields.update(kwargs)
    return CommissionReport(**fields)


def fill(account: str = PAPER_ACCOUNT, contract: Contract | None = None) -> Fill:
    """A fill: contract, execution, commission and time."""
    return Fill(contract or stock(), execution(account), commission_report(), FIXED_TIME)


def order_state(**kwargs: Any) -> OrderState:
    """A what-if style order state."""
    fields: dict[str, Any] = {
        "status": "PreSubmitted",
        "initMarginChange": "1000",
        "maintMarginChange": "800",
        "equityWithLoanChange": "-5",
        "commission": 1.0,
        "minCommission": 1.0,
        "maxCommission": 1.0,
        "commissionCurrency": "USD",
        "warningText": "",
    }
    fields.update(kwargs)
    return OrderState(**fields)


def trade(
    account: str = PAPER_ACCOUNT,
    contract: Contract | None = None,
    order: Order | None = None,
    status: str = "Submitted",
) -> Trade:
    """A trade with a limit order stamped to ``account``."""
    the_order = order or Order(
        orderId=7, action="BUY", totalQuantity=10, orderType="LMT", lmtPrice=99.0, account=account
    )
    return Trade(
        contract=contract or stock(),
        order=the_order,
        orderStatus=OrderStatus(orderId=the_order.orderId, status=status, remaining=10.0),
    )


def scan_data(rank: int = 0, contract: Contract | None = None) -> ScanData:
    """One scanner result row."""
    return ScanData(rank, contract_details(contract), "", "", "", "")


def historical_news(headline: str = "Company announces results", **kwargs: Any) -> HistoricalNews:
    """One historical headline."""
    fields: dict[str, Any] = {
        "time": FIXED_TIME,
        "providerCode": "BRFG",
        "articleId": "BRFG$12345",
        "headline": headline,
    }
    fields.update(kwargs)
    return HistoricalNews(**fields)


def news_article(text: str = "Full article text.", article_type: int = 0) -> NewsArticle:
    """A news article body (type 0 is text, 1 is base64 PDF)."""
    return NewsArticle(articleType=article_type, articleText=text)
