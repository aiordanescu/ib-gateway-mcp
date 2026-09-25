"""AccountService against the autospecced fake IB: values, positions, P&L, fills, orders."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, call

import pytest
from ib_async import (
    AccountValue,
    CommissionReport,
    ExecutionFilter,
    Fill,
    Order,
    OrderState,
    OrderStatus,
    PnL,
    PnLSingle,
    Trade,
)
from ib_async.util import UNSET_DOUBLE
from ib_async.wrapper import RequestError

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    AccountNotAllowedError,
    IbApiError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RequestTimeoutError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.common import ComboLegSpec, ContractSpec
from ib_gateway_mcp.services._ibtime import parse_ib_time
from ib_gateway_mcp.services.account import AccountService
from tests.fakes import (
    FIXED_TIME,
    PAPER_ACCOUNT,
    account_value,
    commission_report,
    contract_details,
    emit_error,
    execution,
    option,
    pending,
    portfolio_item,
    position,
    raises,
    returns,
    stock,
    trade,
)

OTHER_PAPER = "DU7654321"
"""A second placeholder paper account."""
UNMANAGED = "DU9999999"
"""An account outside the allowlist."""
OWN_CLIENT_ID = 80
FAST_TIMEOUT = 0.2
"""IB_REQUEST_TIMEOUT for these tests, so timeout cases stay quick."""
READ_ONLY_TEXT = (
    "Error validating request.-'bN' : cause - The API interface is currently in Read-Only mode."
)
"""IBKR's text for error 321 when the gateway's API is read-only."""


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(request_timeout=FAST_TIMEOUT)


@pytest.fixture
def service(gateway: Gateway) -> AccountService:
    service = gateway.account
    service.pnl_wait = 0.2
    return service


@pytest.fixture
async def multi_gateway(
    settings_factory: Callable[..., Settings], fake_ib: MagicMock
) -> AsyncIterator[Gateway]:
    """A gateway whose login manages two paper accounts, both allowed, default PAPER_ACCOUNT."""
    fake_ib.managedAccounts.return_value = [PAPER_ACCOUNT, OTHER_PAPER]
    settings = settings_factory(
        ib_account=PAPER_ACCOUNT,
        accounts_allowlist=[PAPER_ACCOUNT, OTHER_PAPER],
        request_timeout=FAST_TIMEOUT,
    )
    gw = Gateway(settings, ib_factory=lambda: fake_ib)
    await gw.start()
    try:
        yield gw
    finally:
        await gw.stop()


class RequestFutures:
    """Stands in for ib_async's per-request futures (``wrapper.startReq``/``_endReq``)."""

    def __init__(self, ib: MagicMock, req_id: int = 42) -> None:
        self.futures: dict[Any, asyncio.Future[Any]] = {}
        ib.client.getReqId.return_value = req_id
        ib.wrapper.startReq.side_effect = self.start
        ib.wrapper._endReq.side_effect = self.end

    def start(self, key: Any, contract: Any = None, container: Any = None) -> asyncio.Future[Any]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.futures[key] = future
        return future

    def end(self, key: Any, result: Any = None, success: bool = True) -> None:
        future = self.futures.pop(key, None)
        if future is None or future.done():
            return
        if success:
            future.set_result([] if result is None else result)
        else:
            future.set_exception(result)


def soon(action: Callable[[], None]) -> None:
    """Run ``action`` on the next loop iteration, like a gateway answer arriving."""
    asyncio.get_running_loop().call_soon(action)


def summary_rows(account: str = PAPER_ACCOUNT) -> list[AccountValue]:
    return [
        AccountValue(account, "NetLiquidation", "100000.5", "USD", ""),
        AccountValue(account, "BuyingPower", "400000", "USD", ""),
        AccountValue(account, "Cushion", "0.95", "", ""),
        AccountValue(account, "DayTradesRemaining", "-1", "", ""),
        AccountValue(account, "AccountType", "INDIVIDUAL", "", ""),
        AccountValue(account, "CashBalance", "5000", "BASE", ""),
        AccountValue(account, "CashBalance", "4000", "USD", ""),
        AccountValue(account, "ExcessLiquidity", "nan", "USD", ""),
    ]


# --- account summary ---------------------------------------------------------------------


async def test_account_summary_headline_and_rows(
    service: AccountService, fake_ib: MagicMock
) -> None:
    rows = [*summary_rows(), *summary_rows(UNMANAGED)]
    fake_ib.accountSummaryAsync.side_effect = returns(rows)
    summary = await service.account_summary()
    fake_ib.accountSummaryAsync.assert_called_once_with(PAPER_ACCOUNT)
    assert summary.account == PAPER_ACCOUNT
    assert summary.base_currency == "USD"
    assert summary.net_liquidation == 100000.5
    assert summary.buying_power == 400000
    assert summary.cushion == 0.95
    assert summary.day_trades_remaining == -1
    assert summary.excess_liquidity is None  # NaN
    assert summary.total_cash_value is None  # not sent
    assert {row.account for row in summary.values} == {PAPER_ACCOUNT}
    assert len(summary.values) == 8
    account_type = next(row for row in summary.values if row.tag == "AccountType")
    assert (account_type.value, account_type.amount, account_type.currency) == (
        "INDIVIDUAL",
        None,
        None,
    )
    assert [row.tag for row in summary.values][:2] == ["AccountType", "BuyingPower"]


async def test_account_summary_filters_tags_case_insensitively(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.accountSummaryAsync.side_effect = returns(summary_rows())
    summary = await service.account_summary(tags=["cashbalance", " NetLiquidation "])
    assert sorted((row.tag, row.currency) for row in summary.values) == [
        ("CashBalance", "BASE"),
        ("CashBalance", "USD"),
        ("NetLiquidation", "USD"),
    ]
    assert summary.buying_power == 400000  # headline fields stay filled


async def test_account_summary_rejects_unknown_tags(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.accountSummaryAsync.side_effect = returns(summary_rows())
    with pytest.raises(
        InvalidRequestError, match=r"Unknown tag\(s\).*NetLiq\b.*Available:.*Cushion"
    ):
        await service.account_summary(tags=["NetLiq"])


async def test_account_summary_without_rows_is_not_found(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.accountSummaryAsync.side_effect = returns(summary_rows(UNMANAGED))
    with pytest.raises(NotFoundError, match=r"no account summary for DU1234567"):
        await service.account_summary()


async def test_account_summary_refuses_accounts_outside_the_allowlist(
    service: AccountService, fake_ib: MagicMock
) -> None:
    with pytest.raises(AccountNotAllowedError):
        await service.account_summary(UNMANAGED)
    fake_ib.accountSummaryAsync.assert_not_called()


async def test_account_summary_maps_errors_and_timeouts(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.accountSummaryAsync.side_effect = raises(RequestError(9, 322, "Duplicate id"))
    with pytest.raises(IbApiError, match="322") as info:
        await service.account_summary()
    assert info.value.error_code == 322
    fake_ib.accountSummaryAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match="account summary"):
        await service.account_summary()


# --- account values ------------------------------------------------------------------------


async def test_account_values_from_the_cache(service: AccountService, fake_ib: MagicMock) -> None:
    fake_ib.accountValues.return_value = [
        account_value("NetLiquidation", "100000"),
        account_value("CashBalance", "5000", currency="BASE"),
        account_value("CashBalance", "4000"),
        AccountValue(PAPER_ACCOUNT, "NetLiquidation", "1", "USD", "MODEL1"),
        account_value("NetLiquidation", "7", account=UNMANAGED),
    ]
    result = await service.account_values()
    fake_ib.accountValues.assert_called_with(PAPER_ACCOUNT)
    assert [(v.tag, v.currency, v.amount) for v in result.values] == [
        ("CashBalance", "BASE", 5000),
        ("CashBalance", "USD", 4000),
        ("NetLiquidation", "USD", 100000),
    ]
    assert (result.total, result.truncated, result.model_code) == (3, False, None)
    fake_ib.reqAccountUpdatesMultiAsync.assert_not_called()


async def test_account_values_filters_and_limits(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.accountValues.return_value = [
        account_value(f"Tag{i:02d}", str(i), currency="USD" if i % 2 else "EUR") for i in range(10)
    ]
    usd = await service.account_values(currency="usd", limit=2)
    assert [v.tag for v in usd.values] == ["Tag01", "Tag03"]
    assert (usd.total, usd.truncated) == (5, True)
    tagged = await service.account_values(tags=["tag04"])
    assert [(v.tag, v.currency) for v in tagged.values] == [("Tag04", "EUR")]
    with pytest.raises(InvalidRequestError, match="Unknown tag"):
        await service.account_values(tags=["Nope"])


async def test_account_values_opens_updates_when_the_cache_is_empty(
    service: AccountService, fake_ib: MagicMock
) -> None:
    cache: list[AccountValue] = []
    fake_ib.accountValues.side_effect = lambda _account: list(cache)

    async def open_updates(account: str) -> None:
        cache.append(account_value())

    fake_ib.reqAccountUpdatesMultiAsync.side_effect = open_updates
    result = await service.account_values()
    fake_ib.reqAccountUpdatesMultiAsync.assert_called_once_with(PAPER_ACCOUNT)
    assert [v.tag for v in result.values] == ["NetLiquidation"]


async def test_account_values_not_found_when_nothing_arrives(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.accountValues.return_value = []
    fake_ib.reqAccountUpdatesMultiAsync.side_effect = returns(None)
    with pytest.raises(NotFoundError, match="no account values for account DU1234567"):
        await service.account_values()
    # The standing subscription is open; asking again must not open a second one.
    with pytest.raises(NotFoundError):
        await service.account_values()
    fake_ib.reqAccountUpdatesMultiAsync.assert_called_once_with(PAPER_ACCOUNT)


async def test_account_values_concurrent_callers_open_updates_once(
    service: AccountService, fake_ib: MagicMock
) -> None:
    cache: list[AccountValue] = []
    fake_ib.accountValues.side_effect = lambda _account: list(cache)

    async def open_updates(account: str) -> None:
        await asyncio.sleep(0.01)
        cache.append(account_value())

    fake_ib.reqAccountUpdatesMultiAsync.side_effect = open_updates
    first, second = await asyncio.gather(service.account_values(), service.account_values())
    fake_ib.reqAccountUpdatesMultiAsync.assert_called_once_with(PAPER_ACCOUNT)
    assert [v.tag for v in first.values] == [v.tag for v in second.values] == ["NetLiquidation"]


async def test_account_values_retry_after_a_rejected_open(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.accountValues.return_value = []
    fake_ib.reqAccountUpdatesMultiAsync.side_effect = raises(RequestError(5, 10090, "Denied"))
    with pytest.raises(IbApiError, match="10090"):
        await service.account_values()
    fake_ib.reqAccountUpdatesMultiAsync.side_effect = returns(None)
    with pytest.raises(NotFoundError):
        await service.account_values()
    assert fake_ib.reqAccountUpdatesMultiAsync.call_count == 2


async def test_account_values_for_a_model_code(service: AccountService, fake_ib: MagicMock) -> None:
    RequestFutures(fake_ib)

    def deliver(req_id: int, account: str, model_code: str, ledger: bool) -> None:
        def run() -> None:
            emit = fake_ib.accountValueEvent.emit
            emit(AccountValue(PAPER_ACCOUNT, "NetLiquidation", "5000", "USD", model_code))
            emit(AccountValue(PAPER_ACCOUNT, "NetLiquidation", "9", "USD", "OTHER"))
            emit(AccountValue(UNMANAGED, "NetLiquidation", "8", "USD", model_code))
            fake_ib.wrapper._endReq(req_id)

        soon(run)

    fake_ib.client.reqAccountUpdatesMulti.side_effect = deliver
    result = await service.account_values(model_code="MODEL1")
    fake_ib.client.reqAccountUpdatesMulti.assert_called_once_with(
        42, PAPER_ACCOUNT, "MODEL1", False
    )
    fake_ib.client.cancelAccountUpdatesMulti.assert_called_once_with(42)
    assert [(v.account, v.amount, v.model_code) for v in result.values] == [
        (PAPER_ACCOUNT, 5000, "MODEL1")
    ]
    assert result.model_code == "MODEL1"


async def test_account_values_for_an_unknown_model_code(
    service: AccountService, fake_ib: MagicMock
) -> None:
    RequestFutures(fake_ib)
    fake_ib.client.reqAccountUpdatesMulti.side_effect = lambda req_id, *_: soon(
        lambda: fake_ib.wrapper._endReq(req_id)
    )
    with pytest.raises(NotFoundError, match="model NOPE in account DU1234567"):
        await service.account_values(model_code="NOPE")
    fake_ib.client.cancelAccountUpdatesMulti.assert_called_once_with(42)


async def test_account_values_model_code_rejections(
    service: AccountService, fake_ib: MagicMock
) -> None:
    futures = RequestFutures(fake_ib)

    def reject(req_id: int, *_: Any) -> None:
        error = RequestError(req_id, 10090, "Model code not found")
        soon(lambda: futures.end(req_id, error, success=False))

    fake_ib.client.reqAccountUpdatesMulti.side_effect = reject
    with pytest.raises(IbApiError, match="10090"):
        await service.account_values(model_code="BAD")
    # A 321 (a warning to ib_async, which never ends the request) fails at once too.
    fake_ib.client.reqAccountUpdatesMulti.side_effect = lambda req_id, *_: soon(
        lambda: fake_ib.errorEvent.emit(req_id, 321, "Error validating request", None)
    )
    with pytest.raises(IbApiError, match="321"):
        await service.account_values(model_code="BAD")
    assert fake_ib.client.cancelAccountUpdatesMulti.call_count == 2


async def test_account_values_model_code_when_disconnected(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.client.getReqId.side_effect = ConnectionError("Not connected")
    with pytest.raises(NotConnectedError, match="get_health"):
        await service.account_values(model_code="MODEL1")


# --- positions ---------------------------------------------------------------------------------


async def test_positions_from_the_cache(service: AccountService, fake_ib: MagicMock) -> None:
    held = position(qty=10, avg_cost=95.5)
    held.contract.exchange = ""
    fake_ib.positions.return_value = [
        held,
        position(contract=option(), qty=-2, avg_cost=math.nan),
        position(contract=stock("MSFT", 272093), qty=0),
        position(account=UNMANAGED, qty=5),
    ]
    result = await service.positions()
    assert result.account == PAPER_ACCOUNT
    assert [(p.contract.symbol, p.position, p.avg_cost) for p in result.positions] == [
        ("AAPL", 10, 95.5),
        ("AAPL", -2, None),
    ]
    assert result.positions[0].contract.exchange is None
    assert result.positions[1].contract.sec_type == "OPT"
    fake_ib.reqPositionsAsync.assert_not_called()


async def test_positions_refresh_an_empty_cache(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.positions.return_value = []
    fake_ib.reqPositionsAsync.side_effect = returns([position(qty=3)])
    result = await service.positions()
    fake_ib.reqPositionsAsync.assert_called_once_with()
    assert [p.position for p in result.positions] == [3]


async def test_positions_empty_account(service: AccountService, fake_ib: MagicMock) -> None:
    fake_ib.positions.return_value = [position(account=UNMANAGED)]
    result = await service.positions()
    assert result.positions == []


async def test_positions_for_a_model_code(service: AccountService, fake_ib: MagicMock) -> None:
    RequestFutures(fake_ib, req_id=7)
    original = fake_ib.wrapper.positionMulti

    def deliver(req_id: int, account: str, model_code: str) -> None:
        def run() -> None:
            wrapper = fake_ib.wrapper
            wrapper.positionMulti(req_id, PAPER_ACCOUNT, model_code, stock(), 10.0, 95.0)
            wrapper.positionMulti(req_id, UNMANAGED, model_code, stock(), 5.0, 90.0)
            wrapper.positionMulti(req_id, PAPER_ACCOUNT, model_code, option(), 0.0, 0.0)
            wrapper.positionMulti(req_id + 1, PAPER_ACCOUNT, model_code, stock(), 1.0, 1.0)
            wrapper.positionMultiEnd(req_id)

        soon(run)

    fake_ib.client.reqPositionsMulti.side_effect = deliver
    result = await service.positions(model_code="MODEL1")
    fake_ib.client.reqPositionsMulti.assert_called_once_with(7, PAPER_ACCOUNT, "MODEL1")
    fake_ib.client.cancelPositionsMulti.assert_called_once_with(7)
    assert [(p.account, p.position, p.avg_cost, p.model_code) for p in result.positions] == [
        (PAPER_ACCOUNT, 10, 95, "MODEL1")
    ]
    assert result.model_code == "MODEL1"
    assert fake_ib.wrapper.positionMulti is original  # the hook is gone


async def test_positions_for_a_model_code_keep_only_the_requested_account(
    multi_gateway: Gateway, fake_ib: MagicMock
) -> None:
    RequestFutures(fake_ib, req_id=7)

    def deliver(req_id: int, account: str, model_code: str) -> None:
        def run() -> None:
            wrapper = fake_ib.wrapper
            wrapper.positionMulti(req_id, OTHER_PAPER, model_code, stock(), 5.0, 90.0)
            wrapper.positionMulti(req_id, PAPER_ACCOUNT, model_code, stock(), 10.0, 95.0)
            wrapper.positionMultiEnd(req_id)

        soon(run)

    fake_ib.client.reqPositionsMulti.side_effect = deliver
    result = await multi_gateway.account.positions(PAPER_ACCOUNT, model_code="MODEL1")
    assert [(p.account, p.position) for p in result.positions] == [(PAPER_ACCOUNT, 10)]


async def test_positions_for_a_model_code_time_out_and_cancel(
    service: AccountService, fake_ib: MagicMock
) -> None:
    RequestFutures(fake_ib, req_id=7)
    with pytest.raises(RequestTimeoutError, match="positions of model MODEL1"):
        await service.positions(model_code="MODEL1")
    fake_ib.client.cancelPositionsMulti.assert_called_once_with(7)


# --- portfolio ---------------------------------------------------------------------------------


async def test_portfolio_reads_the_streaming_account(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.portfolio.return_value = [
        portfolio_item(qty=10),
        portfolio_item(contract=option(), qty=1, marketPrice=math.nan, unrealizedPNL=UNSET_DOUBLE),
    ]
    result = await service.portfolio()
    fake_ib.portfolio.assert_called_once_with(PAPER_ACCOUNT)
    fake_ib.reqAccountUpdatesAsync.assert_not_called()
    first, second = result.items
    assert (first.position, first.market_price, first.market_value) == (10, 100.0, 1000.0)
    assert (first.unrealized_pnl, first.realized_pnl) == (50.0, 0.0)
    assert (second.market_price, second.unrealized_pnl) == (None, None)


async def test_portfolio_switches_accounts_and_back(
    multi_gateway: Gateway, fake_ib: MagicMock
) -> None:
    service = multi_gateway.account

    async def account_updates(account: str) -> None:
        if account == OTHER_PAPER:
            emit = fake_ib.updatePortfolioEvent.emit
            emit(portfolio_item(account=OTHER_PAPER, qty=4))
            emit(portfolio_item(account=OTHER_PAPER, contract=option(), qty=0))
            emit(portfolio_item(account=PAPER_ACCOUNT, qty=99))

    fake_ib.reqAccountUpdatesAsync.side_effect = account_updates
    result = await service.portfolio(OTHER_PAPER)
    assert fake_ib.reqAccountUpdatesAsync.call_args_list == [
        call(OTHER_PAPER),
        call(PAPER_ACCOUNT),
    ]
    assert [(i.account, i.position) for i in result.items] == [(OTHER_PAPER, 4)]
    fake_ib.portfolio.assert_not_called()

    # Back on the default account, the live cache answers.
    fake_ib.portfolio.return_value = [portfolio_item(qty=1)]
    home = await service.portfolio()
    assert [i.position for i in home.items] == [1]
    assert fake_ib.reqAccountUpdatesAsync.call_count == 2


async def test_portfolio_survives_a_failed_switch_back(
    multi_gateway: Gateway, fake_ib: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    service = multi_gateway.account

    async def account_updates(account: str) -> None:
        if account == PAPER_ACCOUNT:
            raise RequestError(-1, 504, "Not connected")

    fake_ib.reqAccountUpdatesAsync.side_effect = account_updates
    with caplog.at_level(logging.WARNING):
        result = await service.portfolio(OTHER_PAPER)
    assert result.items == []
    assert "switch account updates back" in caplog.text
    # The streaming account is unknown now, so the default account is re-subscribed.
    fake_ib.reqAccountUpdatesAsync.side_effect = returns(None)
    fake_ib.reqAccountUpdatesAsync.reset_mock()
    await service.portfolio()
    fake_ib.reqAccountUpdatesAsync.assert_called_once_with(PAPER_ACCOUNT)


async def test_portfolio_without_a_default_account_does_not_switch_back(
    settings_factory: Callable[..., Settings], fake_ib: MagicMock
) -> None:
    fake_ib.managedAccounts.return_value = [PAPER_ACCOUNT, OTHER_PAPER]
    settings = settings_factory(
        accounts_allowlist=[PAPER_ACCOUNT, OTHER_PAPER], request_timeout=FAST_TIMEOUT
    )
    async with Gateway(settings, ib_factory=lambda: fake_ib) as gw:
        fake_ib.reqAccountUpdatesAsync.side_effect = returns(None)
        await gw.account.portfolio(OTHER_PAPER)
        fake_ib.reqAccountUpdatesAsync.assert_called_once_with(OTHER_PAPER)
        fake_ib.portfolio.return_value = []
        await gw.account.portfolio(OTHER_PAPER)  # still streaming: the cache answers
        fake_ib.reqAccountUpdatesAsync.assert_called_once_with(OTHER_PAPER)
        fake_ib.portfolio.assert_called_once_with(OTHER_PAPER)


async def test_portfolio_timeout(multi_gateway: Gateway, fake_ib: MagicMock) -> None:
    fake_ib.reqAccountUpdatesAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match=f"account updates for {OTHER_PAPER}"):
        await multi_gateway.account.portfolio(OTHER_PAPER)


# --- P&L ---------------------------------------------------------------------------------------


def pnl_feed(ib: MagicMock, **values: float) -> None:
    """Make ``reqPnL`` answer with one update carrying ``values``."""

    def req_pnl(account: str, modelCode: str = "") -> PnL:
        entry = PnL(account, modelCode)

        def update() -> None:
            for name, value in values.items():
                setattr(entry, name, value)
            ib.pnlEvent.emit(PnL(account, "OTHER", 1.0))  # another model: ignored
            ib.pnlEvent.emit(entry)

        soon(update)
        return entry

    ib.reqPnL.side_effect = req_pnl


async def test_pnl_waits_for_the_first_update_and_cancels(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.pnl.return_value = []
    pnl_feed(fake_ib, dailyPnL=125.5, unrealizedPnL=UNSET_DOUBLE, realizedPnL=-20.0)
    result = await service.pnl()
    fake_ib.reqPnL.assert_called_once_with(PAPER_ACCOUNT, "")
    fake_ib.cancelPnL.assert_called_once_with(PAPER_ACCOUNT, "")
    assert (result.daily_pnl, result.unrealized_pnl, result.realized_pnl) == (125.5, None, -20.0)
    assert result.model_code is None
    assert result.as_of.tzinfo is not None


async def test_pnl_for_a_model_code(service: AccountService, fake_ib: MagicMock) -> None:
    fake_ib.pnl.return_value = []
    pnl_feed(fake_ib, dailyPnL=1.0)
    result = await service.pnl(model_code="MODEL1")
    fake_ib.reqPnL.assert_called_once_with(PAPER_ACCOUNT, "MODEL1")
    fake_ib.cancelPnL.assert_called_once_with(PAPER_ACCOUNT, "MODEL1")
    assert result.model_code == "MODEL1"


async def test_pnl_reuses_an_open_subscription(service: AccountService, fake_ib: MagicMock) -> None:
    fake_ib.pnl.return_value = [PnL(PAPER_ACCOUNT, "", 5.0, 6.0, 7.0)]
    result = await service.pnl()
    assert (result.daily_pnl, result.unrealized_pnl, result.realized_pnl) == (5.0, 6.0, 7.0)
    fake_ib.reqPnL.assert_not_called()
    fake_ib.cancelPnL.assert_not_called()


async def test_pnl_waits_on_an_open_subscription_without_values(
    service: AccountService, fake_ib: MagicMock
) -> None:
    entry = PnL(PAPER_ACCOUNT, "")
    fake_ib.pnl.return_value = [entry]

    def update() -> None:
        entry.dailyPnL = 3.0
        fake_ib.pnlEvent.emit(entry)

    soon(update)
    result = await service.pnl()
    assert result.daily_pnl == 3.0
    fake_ib.reqPnL.assert_not_called()
    fake_ib.cancelPnL.assert_not_called()


async def test_pnl_timeout_explains_and_cancels(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.pnl.return_value = []
    fake_ib.reqPnL.return_value = PnL(PAPER_ACCOUNT, "")
    with pytest.raises(RequestTimeoutError, match=r"P&L of account DU1234567.*try again"):
        await service.pnl()
    fake_ib.cancelPnL.assert_called_once_with(PAPER_ACCOUNT, "")


async def test_pnl_error_on_its_request_id(service: AccountService, fake_ib: MagicMock) -> None:
    fake_ib.pnl.return_value = []
    fake_ib.wrapper.pnlKey2ReqId = {(PAPER_ACCOUNT, ""): 55}

    def req_pnl(account: str, modelCode: str = "") -> PnL:
        soon(lambda: fake_ib.errorEvent.emit(2104, 2104, "Market data farm OK", None))
        soon(lambda: fake_ib.errorEvent.emit(55, 321, "Error validating request", None))
        return PnL(account, modelCode)

    fake_ib.reqPnL.side_effect = req_pnl
    with pytest.raises(IbApiError, match="321") as info:
        await service.pnl()
    assert info.value.req_id == 55
    fake_ib.cancelPnL.assert_called_once_with(PAPER_ACCOUNT, "")


async def test_pnl_when_the_connection_drops(service: AccountService, fake_ib: MagicMock) -> None:
    fake_ib.pnl.return_value = []

    def req_pnl(account: str, modelCode: str = "") -> PnL:
        soon(fake_ib.disconnectedEvent.emit)
        return PnL(account, modelCode)

    fake_ib.reqPnL.side_effect = req_pnl
    with pytest.raises(NotConnectedError, match="P&L of account"):
        await service.pnl()


async def test_pnl_send_failure_is_not_connected(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.pnl.return_value = []
    fake_ib.reqPnL.side_effect = ConnectionError("Not connected")
    with pytest.raises(NotConnectedError):
        await service.pnl()


# --- position P&L ------------------------------------------------------------------------------


def pnl_single_feed(ib: MagicMock, **values: float) -> None:
    def req_pnl_single(account: str, modelCode: str, conId: int) -> PnLSingle:
        entry = PnLSingle(account, modelCode, conId)

        def update() -> None:
            for name, value in values.items():
                setattr(entry, name, value)
            ib.pnlSingleEvent.emit(PnLSingle(account, modelCode, conId + 1, 9.0))
            ib.pnlSingleEvent.emit(entry)

        soon(update)
        return entry

    ib.reqPnLSingle.side_effect = req_pnl_single


async def test_position_pnl(service: AccountService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.positions.return_value = [position(qty=10)]
    fake_ib.pnlSingle.return_value = []
    pnl_single_feed(
        fake_ib, position=10, dailyPnL=12.0, unrealizedPnL=50.0, realizedPnL=0.0, value=1000.0
    )
    result = await service.position_pnl(ContractSpec(symbol="AAPL"))
    fake_ib.reqPnLSingle.assert_called_once_with(PAPER_ACCOUNT, "", 265598)
    fake_ib.cancelPnLSingle.assert_called_once_with(PAPER_ACCOUNT, "", 265598)
    assert result.contract.con_id == 265598
    assert (result.position, result.daily_pnl, result.market_value) == (10, 12.0, 1000.0)


async def test_position_pnl_of_a_position_closed_today(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.positions.return_value = []
    fake_ib.pnlSingle.return_value = []
    pnl_single_feed(fake_ib, position=0, realizedPnL=42.0, dailyPnL=UNSET_DOUBLE)
    result = await service.position_pnl(ContractSpec(con_id=265598))
    assert (result.position, result.realized_pnl, result.daily_pnl) == (0, 42.0, None)


async def test_position_pnl_without_a_position_is_not_found(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.positions.return_value = []
    fake_ib.pnlSingle.return_value = []
    pnl_single_feed(
        fake_ib, position=0, dailyPnL=UNSET_DOUBLE, unrealizedPnL=UNSET_DOUBLE, value=UNSET_DOUBLE
    )
    with pytest.raises(NotFoundError, match="no position and no P&L today in AAPL"):
        await service.position_pnl(ContractSpec(symbol="AAPL"))
    fake_ib.cancelPnLSingle.assert_called_once()


async def test_position_pnl_timeout_without_a_position_is_not_found(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.positions.return_value = []
    fake_ib.pnlSingle.return_value = []
    fake_ib.reqPnLSingle.return_value = PnLSingle(PAPER_ACCOUNT, "", 265598)
    with pytest.raises(NotFoundError, match="no position and no P&L today in AAPL"):
        await service.position_pnl(ContractSpec(symbol="AAPL"))
    fake_ib.cancelPnLSingle.assert_called_once_with(PAPER_ACCOUNT, "", 265598)


async def test_position_pnl_timeout_on_a_held_position_is_a_timeout(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.positions.return_value = [position(qty=10)]
    fake_ib.pnlSingle.return_value = []
    fake_ib.reqPnLSingle.return_value = PnLSingle(PAPER_ACCOUNT, "", 265598)
    with pytest.raises(RequestTimeoutError, match="try again"):
        await service.position_pnl(ContractSpec(symbol="AAPL"))
    fake_ib.cancelPnLSingle.assert_called_once_with(PAPER_ACCOUNT, "", 265598)


async def test_position_pnl_reuses_an_open_subscription(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.positions.return_value = [position()]
    fake_ib.pnlSingle.return_value = [
        PnLSingle(PAPER_ACCOUNT, "", 265598, 1.0, 2.0, 3.0, 10, 1000.0)
    ]
    result = await service.position_pnl(ContractSpec(con_id=265598))
    assert (result.daily_pnl, result.market_value) == (1.0, 1000.0)
    fake_ib.reqPnLSingle.assert_not_called()
    fake_ib.cancelPnLSingle.assert_not_called()


async def test_position_pnl_refuses_combos(service: AccountService, fake_ib: MagicMock) -> None:
    combo = ContractSpec(
        symbol="SPY",
        sec_type="BAG",
        combo_legs=[ComboLegSpec(con_id=1, action="BUY"), ComboLegSpec(con_id=2, action="SELL")],
    )
    with pytest.raises(InvalidRequestError, match="each leg by its con_id"):
        await service.position_pnl(combo)
    fake_ib.reqPnLSingle.assert_not_called()


async def test_position_pnl_unknown_contract(service: AccountService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(
        RequestError(3, 200, "No security definition has been found")
    )
    with pytest.raises(NotFoundError, match="No contract matches NOPE"):
        await service.position_pnl(ContractSpec(symbol="NOPE"))
    fake_ib.reqPnLSingle.assert_not_called()


# --- executions --------------------------------------------------------------------------------


def make_fill(
    exec_id: str,
    *,
    account: str = PAPER_ACCOUNT,
    minutes: int = 0,
    side: str = "BOT",
    report: CommissionReport | None = None,
    contract: Any = None,
    **fields: Any,
) -> Fill:
    the_execution = execution(
        account, execId=exec_id, side=side, time=FIXED_TIME + timedelta(minutes=minutes), **fields
    )
    return Fill(
        contract or stock(),
        the_execution,
        report if report is not None else CommissionReport(),
        the_execution.time,
    )


async def test_executions_newest_first_with_commissions(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fills = [
        make_fill("e1", minutes=0, report=commission_report(execId="e1", commission=1.25)),
        make_fill("e2", minutes=5, side="SLD", price=math.nan),
        make_fill("e3", account=UNMANAGED, minutes=9),
    ]
    fake_ib.reqExecutionsAsync.side_effect = returns(fills)
    # e2 came back without its report (ib_async quirk); the cached fill has it.
    fake_ib.fills.return_value = [
        make_fill("e2", report=commission_report(execId="e2", commission=2.0, realizedPNL=15.0))
    ]
    result = await service.executions(symbol="aapl", sec_type="stk")
    sent = fake_ib.reqExecutionsAsync.call_args.args[0]
    assert sent == ExecutionFilter(acctCode=PAPER_ACCOUNT, symbol="AAPL", secType="STK")
    assert [(e.exec_id, e.side, e.commission) for e in result.executions] == [
        ("e2", "SELL", 2.0),
        ("e1", "BUY", 1.25),
    ]
    newest = result.executions[0]
    assert (newest.realized_pnl, newest.price, newest.account) == (15.0, None, PAPER_ACCOUNT)
    assert newest.time == FIXED_TIME + timedelta(minutes=5)
    assert (newest.order_id, newest.perm_id, newest.client_id) == (7, 123456789, 80)
    assert (result.total, result.truncated) == (2, False)


async def test_executions_without_a_commission_report(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fake_ib.reqExecutionsAsync.side_effect = returns([make_fill("e1")])
    fake_ib.fills.return_value = []
    result = await service.executions()
    row = result.executions[0]
    assert (row.commission, row.realized_pnl, row.commission_currency) == (None, None, None)


async def test_executions_filters_and_limit(service: AccountService, fake_ib: MagicMock) -> None:
    fills = [
        make_fill("e1", minutes=0),
        make_fill("e2", minutes=10, side="SLD"),
        make_fill("e3", minutes=20),
        make_fill("e4", minutes=30, contract=stock("MSFT", 272093)),
        make_fill("e3", minutes=20),  # repeated: counted once
    ]
    fake_ib.reqExecutionsAsync.side_effect = returns(fills)
    fake_ib.fills.return_value = []
    buys = await service.executions(side="BUY")
    assert [e.exec_id for e in buys.executions] == ["e4", "e3", "e1"]
    since = (FIXED_TIME + timedelta(minutes=10)).replace(tzinfo=None)  # naive = UTC
    recent = await service.executions(since=since, symbol="AAPL")
    assert [e.exec_id for e in recent.executions] == ["e3", "e2"]
    limited = await service.executions(limit=1)
    assert [e.exec_id for e in limited.executions] == ["e4"]
    assert (limited.total, limited.truncated) == (4, True)


async def test_executions_empty_and_errors(service: AccountService, fake_ib: MagicMock) -> None:
    fake_ib.reqExecutionsAsync.side_effect = returns([])
    fake_ib.fills.return_value = []
    empty = await service.executions()
    assert (empty.executions, empty.total) == ([], 0)
    fake_ib.reqExecutionsAsync.side_effect = raises(RequestError(4, 321, "read-only"))
    with pytest.raises(IbApiError, match="321"):
        await service.executions()


# --- open orders -------------------------------------------------------------------------------


def working(
    order_id: int,
    *,
    client_id: int = OWN_CLIENT_ID,
    perm_id: int = 0,
    account: str = PAPER_ACCOUNT,
    status: str = "Submitted",
    **order_fields: Any,
) -> Trade:
    fields: dict[str, Any] = {
        "orderId": order_id,
        "clientId": client_id,
        "permId": perm_id or 1000 + order_id,
        "action": "BUY",
        "totalQuantity": 10,
        "orderType": "LMT",
        "lmtPrice": 99.0,
        "tif": "DAY",
        "account": account,
    }
    fields.update(order_fields)
    the_trade = trade(account=account, order=Order(**fields), status=status)
    the_trade.orderStatus.clientId = client_id
    return the_trade


async def test_open_orders_from_every_client(service: AccountService, fake_ib: MagicMock) -> None:
    own = working(7)
    own.orderStatus.filled, own.orderStatus.remaining = 4.0, 6.0
    other_client = working(3, client_id=5)
    manual = working(0, client_id=0, perm_id=555, orderType="STP", lmtPrice=UNSET_DOUBLE)
    manual.order.auxPrice = 95.0
    fake_ib.reqAllOpenOrdersAsync.side_effect = returns(
        [
            own,
            other_client,
            manual,
            own,  # reported twice
            working(8, status="Filled"),
            working(9, account=UNMANAGED),
        ]
    )
    result = await service.open_orders()
    fake_ib.reqAllOpenOrdersAsync.assert_called_once_with()
    assert result.include_other_clients is True
    assert [(o.order_id, o.client_id, o.modifiable) for o in result.orders] == [
        (7, OWN_CLIENT_ID, True),
        (3, 5, False),
        (None, 0, False),
    ]
    first, _, stop = result.orders
    assert (first.filled, first.remaining, first.limit_price, first.tif) == (4.0, 6.0, 99.0, "DAY")
    assert (stop.order_type, stop.limit_price, stop.aux_price, stop.perm_id) == (
        "STP",
        None,
        95.0,
        555,
    )


async def test_open_orders_of_this_client_only(service: AccountService, fake_ib: MagicMock) -> None:
    fake_ib.reqOpenOrdersAsync.side_effect = returns([working(7), working(3, client_id=5)])
    result = await service.open_orders(include_other_clients=False)
    fake_ib.reqOpenOrdersAsync.assert_called()
    fake_ib.reqAllOpenOrdersAsync.assert_not_called()
    assert [o.order_id for o in result.orders] == [7]


async def test_open_orders_before_a_status_message(
    service: AccountService, fake_ib: MagicMock
) -> None:
    fresh = working(3, client_id=5, filledQuantity=2.0)
    fresh.orderStatus.remaining = 0.0
    fake_ib.reqAllOpenOrdersAsync.side_effect = returns([fresh])
    order = (await service.open_orders()).orders[0]
    assert (order.filled, order.remaining) == (2.0, 8.0)


async def test_open_orders_timeout(service: AccountService, fake_ib: MagicMock) -> None:
    fake_ib.reqAllOpenOrdersAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match="open orders"):
        await service.open_orders()
    # ib_async's pending "openOrders" request is dropped, so its result list cannot
    # swallow later openOrder updates.
    fake_ib.wrapper._endReq.assert_called_with("openOrders")


# --- a read-only API ---------------------------------------------------------------------------


def refused_as_read_only(ib: MagicMock) -> Callable[..., Any]:
    """Side effect for an order-list request a read-only API refuses: 321, reqId -1, no end."""

    async def side_effect(*_args: Any, **_kwargs: Any) -> Any:
        soon(lambda: emit_error(ib, 321, READ_ONLY_TEXT))
        return await asyncio.get_running_loop().create_future()

    return side_effect


def learn_read_only(gateway: Gateway, ib: MagicMock) -> None:
    """Let the connection see a read-only 321, as after an earlier refused request."""
    emit_error(ib, 321, READ_ONLY_TEXT)
    assert gateway.connection.health().api_read_only


async def test_open_orders_of_this_client_on_a_read_only_api_read_every_client(
    service: AccountService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    learn_read_only(gateway, fake_ib)
    fake_ib.reqOpenOrdersAsync.reset_mock()
    fake_ib.reqAllOpenOrdersAsync.side_effect = returns([working(7), working(3, client_id=5)])
    result = await service.open_orders(include_other_clients=False)
    fake_ib.reqOpenOrdersAsync.assert_not_called()
    fake_ib.reqAllOpenOrdersAsync.assert_called_once_with()
    assert [(o.order_id, o.modifiable) for o in result.orders] == [(7, True)]
    assert result.include_other_clients is False
    assert result.note is not None
    assert "read-only" in result.note
    assert f"client id {OWN_CLIENT_ID}" in result.note


async def test_open_orders_of_this_client_fall_back_when_the_api_refuses(
    service: AccountService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    fake_ib.reqOpenOrdersAsync.side_effect = refused_as_read_only(fake_ib)
    fake_ib.reqAllOpenOrdersAsync.side_effect = returns([working(7), working(3, client_id=5)])
    result = await service.open_orders(include_other_clients=False)
    assert [o.order_id for o in result.orders] == [7]
    assert result.note is not None
    assert "reqOpenOrders" in result.note
    fake_ib.wrapper._endReq.assert_any_call("openOrders")  # the refused request is dropped
    assert gateway.connection.health().api_read_only


async def test_open_orders_refused_by_a_read_only_api_fail_at_once(
    settings_factory: Callable[..., Settings], fake_ib: MagicMock
) -> None:
    async with Gateway(
        settings_factory(request_timeout=30.0), ib_factory=lambda: fake_ib
    ) as gateway:
        service = gateway.account
        service.read_only_grace = 0.05
        fake_ib.reqAllOpenOrdersAsync.side_effect = refused_as_read_only(fake_ib)
        started = asyncio.get_running_loop().time()
        with pytest.raises(IbApiError) as caught:
            await service.open_orders()
        assert asyncio.get_running_loop().time() - started < 1.0  # not the 30 s timeout
        # With the API known to be read-only, this client's orders fail the same way.
        with pytest.raises(IbApiError, match="321"):
            await service.open_orders(include_other_clients=False)
    error = caught.value
    assert (error.error_code, error.req_id) == (321, -1)
    assert "Read-Only mode (open orders)" in str(error)
    assert "READ_ONLY_API=no" in str(error)
    assert "get_health" in str(error)


async def test_every_clients_open_orders_get_a_grace_after_a_321(
    service: AccountService, fake_ib: MagicMock
) -> None:
    """A 321 without a request id may be another request's: the answer still counts."""

    async def refused_elsewhere() -> list[Trade]:
        emit_error(fake_ib, 321, READ_ONLY_TEXT)
        await asyncio.sleep(0.05)
        return [working(7)]

    service.read_only_grace = 1.0
    fake_ib.reqAllOpenOrdersAsync.side_effect = refused_elsewhere
    result = await service.open_orders()
    assert [o.order_id for o in result.orders] == [7]
    assert result.note is None


async def test_order_reads_ignore_other_errors(service: AccountService, fake_ib: MagicMock) -> None:
    """Only a read-only 321 without a request id ends an order-list request."""
    listeners = len(fake_ib.errorEvent)

    async def answered() -> list[Trade]:
        emit_error(fake_ib, 321, READ_ONLY_TEXT, req_id=12)  # a what-if order's refusal
        emit_error(fake_ib, 321, "Error validating request: something else")
        emit_error(fake_ib, 2104, "Market data farm connection is OK:usfarm")
        await asyncio.sleep(0)
        return [working(7)]

    fake_ib.reqOpenOrdersAsync.side_effect = answered
    result = await service.open_orders(include_other_clients=False)
    assert [o.order_id for o in result.orders] == [7]
    assert result.note is None
    assert len(fake_ib.errorEvent) == listeners  # ours is gone again


# --- completed orders --------------------------------------------------------------------------


def completed(
    perm_id: int, completed_time: str, *, account: str = PAPER_ACCOUNT, status: str = "Filled"
) -> tuple[Trade, OrderState]:
    order = Order(
        permId=perm_id,
        action="SELL",
        totalQuantity=5,
        orderType="MKT",
        lmtPrice=0.0,
        account=account,
        filledQuantity=5.0,
    )
    state = OrderState(status=status, completedTime=completed_time, completedStatus=status)
    the_trade = Trade(stock(), order, OrderStatus(orderId=0, status=status))
    return the_trade, state


async def test_completed_orders_newest_first_with_completion_details(
    service: AccountService, fake_ib: MagicMock
) -> None:
    rows = [
        completed(1, "20260102 09:30:00 America/New_York"),
        completed(2, "20260102 10:00:00 EST", status="Cancelled"),
        completed(3, "garbled"),
        completed(4, "20260102 11:00:00 America/New_York", account=UNMANAGED),
    ]
    original = fake_ib.wrapper.completedOrder

    async def req_completed(api_only: bool) -> list[Trade]:
        for the_trade, state in rows:
            fake_ib.wrapper.completedOrder(the_trade.contract, the_trade.order, state)
        return [the_trade for the_trade, _ in rows]

    fake_ib.reqCompletedOrdersAsync.side_effect = req_completed
    result = await service.completed_orders(api_only=True)
    fake_ib.reqCompletedOrdersAsync.assert_called_with(True)
    assert [(o.perm_id, o.status) for o in result.orders] == [
        (2, "Cancelled"),
        (1, "Filled"),
        (3, "Filled"),
    ]
    cancelled, filled, garbled = result.orders
    assert cancelled.completed_at == datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    assert cancelled.completed_status == "Cancelled"
    assert filled.completed_at == datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    assert (garbled.completed_time, garbled.completed_at) == ("garbled", None)
    assert (filled.filled, filled.limit_price, filled.order_type) == (5.0, None, "MKT")
    # The hook passed every order on to ib_async's own handler, and is gone again.
    assert original.call_count == 4
    assert fake_ib.wrapper.completedOrder is original


async def test_completed_orders_limit(service: AccountService, fake_ib: MagicMock) -> None:
    rows = [completed(i, f"20260102 1{i}:00:00 UTC")[0] for i in range(1, 5)]
    fake_ib.reqCompletedOrdersAsync.side_effect = returns(rows)
    result = await service.completed_orders(limit=2)
    assert [o.perm_id for o in result.orders] == [1, 2, 3, 4][:2]  # no times captured
    assert (result.total, result.truncated) == (4, True)


async def test_completed_orders_error(service: AccountService, fake_ib: MagicMock) -> None:
    fake_ib.reqCompletedOrdersAsync.side_effect = raises(RequestError(-1, 10197, "No data"))
    with pytest.raises(IbApiError, match="10197"):
        await service.completed_orders()


async def test_completed_orders_refused_by_a_read_only_api_fail_at_once(
    settings_factory: Callable[..., Settings], fake_ib: MagicMock
) -> None:
    async with Gateway(
        settings_factory(request_timeout=30.0), ib_factory=lambda: fake_ib
    ) as gateway:
        original = fake_ib.wrapper.completedOrder
        fake_ib.reqCompletedOrdersAsync.side_effect = refused_as_read_only(fake_ib)
        started = asyncio.get_running_loop().time()
        with pytest.raises(IbApiError) as caught:
            await gateway.account.completed_orders()
        assert asyncio.get_running_loop().time() - started < 1.0  # not the 30 s timeout
    error = caught.value
    assert (error.error_code, error.req_id) == (321, -1)
    assert "Read-Only mode (completed orders)" in str(error)
    assert "READ_ONLY_API=no" in str(error)
    assert "get_executions" in str(error)
    fake_ib.wrapper._endReq.assert_any_call("completedOrders")
    assert fake_ib.wrapper.completedOrder is original  # the hook is gone


async def test_completed_orders_on_a_known_read_only_api_are_refused_without_asking(
    service: AccountService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    learn_read_only(gateway, fake_ib)
    fake_ib.reqCompletedOrdersAsync.reset_mock()
    with pytest.raises(IbApiError) as caught:
        await service.completed_orders()
    fake_ib.reqCompletedOrdersAsync.assert_not_called()
    error = caught.value
    assert error.error_code == 321
    assert "reported earlier this session (completed orders)" in str(error)
    assert "READ_ONLY_API=no" in str(error)


# --- helpers -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("20260102 09:30:00 America/New_York", datetime(2026, 1, 2, 14, 30, tzinfo=UTC)),
        ("20260702 09:30:00 EDT", datetime(2026, 7, 2, 13, 30, tzinfo=UTC)),
        ("20260102-15:30:00", datetime(2026, 1, 2, 15, 30, tzinfo=UTC)),
        ("20260102 15:30:00", None),
        ("20260102 15:30:00 Mars/Olympus", None),
        ("20261302 15:30:00 UTC", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_ib_time(text: str | None, expected: datetime | None) -> None:
    assert parse_ib_time(text) == expected
