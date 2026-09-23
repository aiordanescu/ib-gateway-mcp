"""OrdersService against the autospecced fake IB: previews, submit, modify, cancel, exercise.

:class:`OrderBook` stands in for ib_async's order bookkeeping (``placeOrder``,
``cancelOrder``, the trade cache) and for IBKR's answers, which arrive on the event loop
right after a call, the way ``orderStatus`` messages do.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from ib_async import ComboLeg, Contract, Fill, Order, OrderStatus, Position, Trade
from ib_async.objects import PriceIncrement, TradeLogEntry
from ib_async.util import UNSET_DOUBLE
from ib_async.wrapper import RequestError
from pydantic import ValidationError

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    AccountNotAllowedError,
    CircuitOpenError,
    ConfigurationError,
    ConfirmationUnavailableError,
    IbApiError,
    InvalidRequestError,
    LiveTradingDisabledError,
    NotConnectedError,
    NotFoundError,
    OrderLimitError,
    RateLimitError,
    RequestTimeoutError,
    TokenExpiredError,
    TokenNotFoundError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.common import ContractSpec
from ib_gateway_mcp.models.orders import (
    AdaptiveAlgo,
    ArrivalPxAlgo,
    BracketSpec,
    ClosePxAlgo,
    ComboOrderLegSpec,
    ComboSpec,
    ExerciseSpec,
    ModifySpec,
    OcaSpec,
    OrderSpec,
    PctVolAlgo,
    SoftDollarTierRef,
    TwapAlgo,
    VwapAlgo,
    invalid_request_on,
)
from ib_gateway_mcp.safety import AUDIT_LOGGER_NAME, SafetyRails
from ib_gateway_mcp.services.orders import OrdersService
from tests.fakes import (
    FIXED_TIME,
    LIVE_ACCOUNT,
    PAPER_ACCOUNT,
    FakeClock,
    commission_report,
    contract_details,
    execution,
    option,
    order_state,
    pending,
    raises,
    returns,
    stock,
    ticker,
)

OWN_CLIENT_ID = 80
OTHER_PAPER = "DU7654321"
UNMANAGED = "DU9999999"
AAPL = ContractSpec(symbol="AAPL")
READ_ONLY_TEXT = (
    "Error validating request.-'bN' : cause - The API interface is currently in Read-Only mode."
)


# --- fakes --------------------------------------------------------------------------------


class OrderBook:
    """ib_async's trade cache plus IBKR's replies, on top of the autospecced fake IB.

    ``reply`` runs (on the event loop, right after ``placeOrder``) for every order sent;
    ``cancel_reply`` likewise after ``cancelOrder``. Set either to None for silence.
    ``ibkr_status`` is what IBKR itself holds per order id: re-syncing the open orders
    (``reqOpenOrders``) re-sends that status, the way IBKR re-sends ``orderStatus``.
    """

    def __init__(self, ib: MagicMock, client_id: int = OWN_CLIENT_ID) -> None:
        self.ib = ib
        self.client_id = client_id
        self.trades: list[Trade] = []
        self.next_id = 100
        self.next_perm = 900_000
        self.reply: Callable[[Trade], None] | None = self.submitted
        self.cancel_reply: Callable[[Trade], None] | None = self.cancelled
        self.ibkr_status: dict[int, str] = {}
        ib.placeOrder.side_effect = self.place_order
        ib.cancelOrder.side_effect = self.cancel_order
        ib.trades.side_effect = lambda: list(self.trades)
        ib.openTrades.side_effect = lambda: [t for t in self.trades if not t.isDone()]
        ib.client.getReqId.side_effect = self.req_id
        ib.whatIfOrderAsync.side_effect = returns(order_state())
        ib.reqContractDetailsAsync.side_effect = returns([contract_details()])
        ib.reqTickersAsync.side_effect = returns([])
        ib.ticker.return_value = None
        ib.positions.return_value = []
        ib.reqOpenOrdersAsync.side_effect = self.own_open_orders
        ib.reqAllOpenOrdersAsync.side_effect = self.all_open_orders
        ib.reqCompletedOrdersAsync.side_effect = returns([])

    def req_id(self) -> int:
        self.next_id += 1
        return self.next_id

    # --- ib_async side ---------------------------------------------------------------

    def place_order(self, contract: Contract, order: Order) -> Trade:
        if order.orderId:
            trade = next(
                t
                for t in self.trades
                if t.order.orderId == order.orderId and t.order.clientId == self.client_id
            )
            trade.log.append(TradeLogEntry(FIXED_TIME, trade.orderStatus.status, "Modify"))
        else:
            order.orderId = self.req_id()
            order.clientId = self.client_id
            trade = Trade(
                contract=contract,
                order=order,
                orderStatus=OrderStatus(orderId=order.orderId, status="PendingSubmit"),
                fills=[],
                log=[TradeLogEntry(FIXED_TIME, "PendingSubmit")],
            )
            self.trades.append(trade)
        if self.reply is not None:
            asyncio.get_running_loop().call_soon(self.reply, trade)
        return trade

    def cancel_order(self, order: Order, manualCancelOrderTime: str = "") -> Trade | None:
        trade = next((t for t in self.trades if t.order is order), None)
        if trade is None:
            return None
        trade.orderStatus.status = OrderStatus.PendingCancel
        if self.cancel_reply is not None:
            asyncio.get_running_loop().call_soon(self.cancel_reply, trade)
        return trade

    async def own_open_orders(self) -> list[Trade]:
        for trade in self.trades:
            status = self.ibkr_status.get(trade.order.orderId)
            if status is not None and trade.orderStatus.status != status:
                self.set_status(trade, status)
        return [t for t in self.trades if not t.isDone() and t.order.clientId == self.client_id]

    async def all_open_orders(self) -> list[Trade]:
        return [t for t in self.trades if not t.isDone()]

    async def completed_orders(self) -> list[Trade]:
        """IBKR's completed orders: fresh copies, as ib_async builds them."""
        return [copy.deepcopy(t) for t in self.trades if t.isDone()]

    # --- IBKR side -------------------------------------------------------------------

    def set_status(
        self, trade: Trade, status: str, *, error: tuple[int, str] | None = None
    ) -> None:
        if error is not None:
            code, text = error
            message = f"Error {code}, reqId {trade.order.orderId}: {text}"
            trade.log.append(TradeLogEntry(FIXED_TIME, status, message, code))
        else:
            trade.log.append(TradeLogEntry(FIXED_TIME, status, ""))
        if not trade.order.permId:
            self.next_perm += 1
            trade.order.permId = self.next_perm
        trade.orderStatus.status = status
        trade.orderStatus.permId = trade.order.permId
        trade.orderStatus.remaining = float(trade.order.totalQuantity)
        trade.statusEvent.emit(trade)

    def submitted(self, trade: Trade) -> None:
        self.set_status(trade, OrderStatus.Submitted)

    def rejected(self, trade: Trade) -> None:
        self.set_status(
            trade, OrderStatus.Cancelled, error=(201, "Order rejected - reason: margin")
        )

    def read_only(self, trade: Trade) -> None:
        self.ib_error(trade, 321, READ_ONLY_TEXT, warning=True)  # no perm id: never placed

    def cancelled(self, trade: Trade) -> None:
        self.set_status(trade, OrderStatus.Cancelled, error=(202, "Order Canceled - reason:"))

    def ib_error(self, trade: Trade, code: int, text: str, *, warning: bool = False) -> None:
        """What ib_async's ``wrapper.error`` does to a trade: a warning code sets
        ValidationError, an error code sets Cancelled (even for a rejected modification
        of an order that IBKR keeps working)."""
        status = OrderStatus.ValidationError if warning else OrderStatus.Cancelled
        kind = "Warning" if warning else "Error"
        message = f"{kind} {code}, reqId {trade.order.orderId}: {text}"
        trade.orderStatus.status = status
        trade.log.append(TradeLogEntry(FIXED_TIME, status, message, code))
        trade.statusEvent.emit(trade)

    def add(
        self,
        *,
        order_id: int,
        client_id: int = OWN_CLIENT_ID,
        account: str = PAPER_ACCOUNT,
        status: str = OrderStatus.Submitted,
        perm_id: int = 0,
        contract: Contract | None = None,
        **order_fields: Any,
    ) -> Trade:
        """Put an existing order (e.g. from an earlier session) into the cache."""
        fields: dict[str, Any] = {
            "action": "BUY",
            "totalQuantity": 10.0,
            "orderType": "LMT",
            "lmtPrice": 150.0,
            "tif": "DAY",
        }
        fields.update(order_fields)
        order = Order(
            orderId=order_id,
            clientId=client_id,
            permId=perm_id or 500_000 + order_id,
            account=account,
            **fields,
        )
        trade = Trade(
            contract=contract or stock(),
            order=order,
            orderStatus=OrderStatus(
                orderId=order_id, status=status, remaining=float(order.totalQuantity)
            ),
            fills=[],
            log=[TradeLogEntry(FIXED_TIME, status, "")],
        )
        self.trades.append(trade)
        return trade


def details_for(*contracts: Contract) -> Callable[..., Awaitable[list[Any]]]:
    """``reqContractDetailsAsync`` answering for the given contracts, error 200 otherwise."""

    async def answer(request: Contract) -> list[Any]:
        for known in contracts:
            if request.conId and request.conId == known.conId:
                return [contract_details(copy.copy(known))]
            if request.conId:
                continue
            if (
                request.symbol == known.symbol
                and request.secType == known.secType
                and (not request.strike or request.strike == known.strike)
                and (not request.right or request.right == known.right)
            ):
                return [contract_details(copy.copy(known))]
        raise RequestError(request.conId, 200, "No security definition has been found")

    return answer


def make_fill(execution_: Any, report: Any) -> Fill:
    """A fill of the default stock."""
    return Fill(stock(), execution_, report, FIXED_TIME)


def audit_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == AUDIT_LOGGER_NAME
    ]


def events_named(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [event for event in audit_events(caplog) if event["event"] == name]


def lmt(quantity: float = 10, price: float = 150.0, **fields: Any) -> OrderSpec:
    return OrderSpec(
        contract=fields.pop("contract", AAPL),
        action=fields.pop("action", "BUY"),
        quantity=quantity,
        order_type=fields.pop("order_type", "LMT"),
        limit_price=price,
        **fields,
    )


def mkt(quantity: float = 10, **fields: Any) -> OrderSpec:
    return OrderSpec(
        contract=fields.pop("contract", AAPL),
        action=fields.pop("action", "BUY"),
        quantity=quantity,
        order_type="MKT",
        **fields,
    )


# --- fixtures -----------------------------------------------------------------------------

MakeGateway = Callable[..., Awaitable[Gateway]]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def book(fake_ib: MagicMock) -> OrderBook:
    return OrderBook(fake_ib)


@pytest.fixture
async def make_gateway(
    settings_factory: Callable[..., Settings],
    fake_ib: MagicMock,
    clock: FakeClock,
    book: OrderBook,
) -> AsyncIterator[MakeGateway]:
    """Start gateways on the trading profile, with safety rails on the fake clock."""
    started: list[Gateway] = []

    async def make(**overrides: Any) -> Gateway:
        settings = settings_factory(**{"profile": "trading", **overrides})
        rails = SafetyRails.from_settings(settings, clock=clock.time, monotonic=clock.monotonic)
        gateway = Gateway(settings, ib_factory=lambda: fake_ib, safety=rails)
        await gateway.start()
        gateway.orders.status_wait = 0.5
        gateway.orders.exercise_wait = 0.05
        started.append(gateway)
        return gateway

    yield make
    for gateway in started:
        await gateway.stop()


@pytest.fixture
async def service(make_gateway: MakeGateway) -> OrdersService:
    return (await make_gateway()).orders


@pytest.fixture(autouse=True)
def _audit_logging(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)


# --- models -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"order_type": "LMT"}, "LMT orders need limit_price"),
        ({"order_type": "MKT", "limit_price": 1.0}, "MKT orders take no limit_price"),
        ({"order_type": "STP"}, "STP orders need aux_price"),
        ({"order_type": "STP LMT", "limit_price": 1.0}, "STP LMT orders need aux_price"),
        ({"order_type": "LMT", "limit_price": 1.0, "aux_price": 1.0}, "take no aux_price"),
        ({"order_type": "TRAIL"}, "exactly one of aux_price (trailing amount)"),
        (
            {"order_type": "TRAIL", "aux_price": 1.0, "trailing_percent": 2.0},
            "exactly one of aux_price",
        ),
        (
            {"order_type": "TRAIL LIMIT", "aux_price": 1.0, "trail_stop_price": 90.0},
            "limit_price and limit_price_offset",
        ),
        # IBKR rejects a TRAIL LIMIT without an initial stop (321 "Please enter a stop price").
        (
            {"order_type": "TRAIL LIMIT", "trailing_percent": 50.0, "limit_price_offset": 0.5},
            "TRAIL LIMIT orders need trail_stop_price",
        ),
        (
            {"order_type": "TRAIL LIMIT", "aux_price": 1.0, "limit_price": 1.0},
            "TRAIL LIMIT orders need trail_stop_price",
        ),
        ({"order_type": "LMT", "limit_price": 1.0, "trailing_percent": 1.0}, "only for TRAIL"),
        ({"order_type": "LMT", "limit_price": 1.0, "limit_price_offset": 1.0}, "only for TRAIL"),
        ({"order_type": "MIT"}, "MIT orders need aux_price"),
        ({"order_type": "LOC"}, "LOC orders need limit_price"),
        ({"order_type": "MOC", "tif": "GTC"}, "MOC orders need tif DAY"),
        ({"order_type": "MKT", "tif": "GTD"}, "tif GTD needs good_till_date"),
        (
            {"order_type": "MKT", "good_till_date": datetime(2026, 12, 18, tzinfo=ZoneInfo("UTC"))},
            "only used with tif GTD",
        ),
        ({"order_type": "STP", "aux_price": 1.0, "tif": "OPG"}, "OPG"),
        (
            {"order_type": "STP", "aux_price": 1.0, "algo": {"strategy": "Adaptive"}},
            "algos work with MKT or LMT",
        ),
        (
            {
                "order_type": "MKT",
                "contract": {
                    "sec_type": "BAG",
                    "symbol": "SPY",
                    "combo_legs": [
                        {"con_id": 1, "action": "BUY"},
                        {"con_id": 2, "action": "SELL"},
                    ],
                },
            },
            "preview_combo_order",
        ),
        ({"order_type": "PEG MKT", "limit_price": 1.0}, "PEG MKT orders take no limit_price"),
        (
            {"order_type": "LMT", "limit_price": 1.0, "display_size": 1},
            "display_size must be less than quantity",
        ),
        (
            {"order_type": "MKT", "quantity": 10, "display_size": 2, "hidden": True},
            "take no display_size",
        ),
        (
            {
                "order_type": "LMT",
                "limit_price": 1.0,
                "tif": "GTD",
                "good_till_date": "2026-12-18T10:00:00-05:00",
                "good_after_time": "2026-12-18T11:00:00-05:00",
            },
            "good_after_time must be before good_till_date",
        ),
        (
            {"order_type": "MKT", "model_code": "   "},
            "String should match pattern",
        ),
    ],
)
def test_order_spec_checks_prices_per_type(fields: dict[str, Any], problem: str) -> None:
    data: dict[str, Any] = {"contract": {"symbol": "AAPL"}, "action": "BUY", "quantity": 1}
    data.update(fields)
    with pytest.raises(ValidationError, match=re.escape(problem)):
        OrderSpec.model_validate(data)


@pytest.mark.parametrize(
    "fields",
    [
        {"order_type": "MKT"},
        {"order_type": "MOC"},
        {"order_type": "LMT", "limit_price": 1.5, "tif": "OPG"},
        {"order_type": "STP", "aux_price": 1.0},
        {"order_type": "STP LMT", "aux_price": 1.0, "limit_price": 1.1},
        {"order_type": "TRAIL", "trailing_percent": 2.5, "trail_stop_price": 90.0},
        {"order_type": "TRAIL", "aux_price": 1.0},
        {
            "order_type": "TRAIL LIMIT",
            "aux_price": 1.0,
            "trail_stop_price": 90.0,
            "limit_price_offset": 0.1,
        },
        {"order_type": "REL", "aux_price": 0.0, "limit_price": 100.0},
        {"order_type": "MIDPRICE"},
        {"order_type": "LIT", "aux_price": 1.0, "limit_price": 1.1},
        {
            "order_type": "LMT",
            "limit_price": 1.0,
            "tif": "GTD",
            "good_till_date": "2026-12-18T16:00:00-05:00",
        },
        {"order_type": "MKT", "algo": {"strategy": "Vwap", "max_pct_vol": 0.2}},
        {"order_type": "PEG MID", "aux_price": 0.01, "limit_price": 100.0},
        {"order_type": "PEG MID"},
        {"order_type": "PEG MKT", "aux_price": 0.05},
        {"order_type": "LMT", "limit_price": 1.0, "algo": {"strategy": "PctVol"}},
        {"order_type": "MKT", "algo": {"strategy": "ClosePx", "risk_aversion": "Passive"}},
        {"order_type": "LMT", "limit_price": 1.0, "display_size": 1, "all_or_none": False},
    ],
)
def test_order_spec_accepts_complete_orders(fields: dict[str, Any]) -> None:
    data: dict[str, Any] = {"contract": {"symbol": "AAPL"}, "action": "SELL", "quantity": 2}
    data.update(fields)
    assert OrderSpec.model_validate(data).order_type == fields["order_type"]


def test_order_spec_needs_a_time_zone_and_a_positive_quantity() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        lmt(tif="GTD", good_till_date=datetime(2026, 12, 18))
    with pytest.raises(ValidationError, match="greater than 0"):
        lmt(quantity=0)


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"take_profit_price": 140.0, "stop_loss_price": 160.0}, "stop_loss_price must be below"),
        ({"entry_price": 170.0}, "entry_price must lie between"),
        ({"entry_type": "MKT"}, "MKT entry takes no entry_price"),
        ({"entry_type": "STP LMT"}, "entry_stop_price is required"),
        ({"entry_price": None}, "LMT entry needs entry_price"),
        ({"tif": "GTD"}, "tif GTD needs good_till_date"),
    ],
)
def test_bracket_spec_checks_price_sides(fields: dict[str, Any], problem: str) -> None:
    data: dict[str, Any] = {
        "contract": AAPL,
        "action": "BUY",
        "quantity": 10,
        "entry_price": 150.0,
        "take_profit_price": 160.0,
        "stop_loss_price": 140.0,
    }
    data.update(fields)
    with pytest.raises(ValidationError, match=re.escape(problem)):
        BracketSpec.model_validate(data)


def test_bracket_spec_sell_side_is_mirrored() -> None:
    spec = BracketSpec(
        contract=AAPL,
        action="SELL",
        quantity=5,
        entry_price=150.0,
        take_profit_price=140.0,
        stop_loss_price=160.0,
    )
    assert spec.action == "SELL"


def test_invalid_request_on_reports_a_failed_spec_as_invalid_request() -> None:
    with (
        pytest.raises(InvalidRequestError, match=r"Invalid BracketSpec: .*must be below"),
        invalid_request_on(BracketSpec),
    ):
        BracketSpec(
            contract=AAPL,
            action="BUY",
            quantity=1,
            entry_price=10.0,
            take_profit_price=9.0,
            stop_loss_price=11.0,
        )


def test_algo_parameters_map_to_tag_values() -> None:
    assert AdaptiveAlgo(strategy="Adaptive", priority="Urgent").tag_values() == [
        ("adaptivePriority", "Urgent")
    ]
    assert dict(VwapAlgo(strategy="Vwap", max_pct_vol=0.25, no_take_liq=True).tag_values()) == {
        "maxPctVol": "0.25",
        "startTime": "",
        "endTime": "",
        "allowPastEndTime": "0",
        "noTakeLiq": "1",
    }
    assert dict(TwapAlgo(strategy="Twap", start_time="09:45:00 US/Eastern").tag_values())[
        "startTime"
    ] == ("09:45:00 US/Eastern")
    arrival = dict(ArrivalPxAlgo(strategy="ArrivalPx", risk_aversion="Passive").tag_values())
    assert arrival["riskAversion"] == "Passive"
    assert arrival["forceCompletion"] == "0"
    assert PctVolAlgo(strategy="PctVol", pct_vol=0.2, no_take_liq=True).tag_values() == [
        ("pctVol", "0.2"),
        ("startTime", ""),
        ("endTime", ""),
        ("noTakeLiq", "1"),
    ]
    assert ClosePxAlgo(strategy="ClosePx", force_completion=True).tag_values() == [
        ("maxPctVol", "0.1"),
        ("riskAversion", "Neutral"),
        ("startTime", ""),
        ("forceCompletion", "1"),
    ]


def test_modify_spec_needs_a_change() -> None:
    with pytest.raises(ValidationError, match="at least one field"):
        ModifySpec()
    with pytest.raises(ValidationError, match="at least one field"):
        ModifySpec(limit_price=None)
    assert ModifySpec(outside_rth=False).outside_rth is False


def test_combo_and_oca_specs_check_their_sizes() -> None:
    leg = ComboOrderLegSpec(contract=AAPL, action="BUY")
    with pytest.raises(ValidationError, match="at least 2"):
        ComboSpec(legs=[leg], action="BUY", quantity=1, limit_price=1.0)
    with pytest.raises(ValidationError, match="LMT combo orders need limit_price"):
        ComboSpec(legs=[leg, leg], action="BUY", quantity=1)
    with pytest.raises(ValidationError, match="at least 2"):
        OcaSpec(orders=[lmt()])


# --- preview_order --------------------------------------------------------------------------


async def test_preview_order_what_ifs_and_issues_a_token(
    service: OrdersService, fake_ib: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    preview = await service.preview_order(lmt())

    assert preview.kind == "order"
    assert preview.account == PAPER_ACCOUNT
    assert preview.is_paper is True
    assert preview.token
    assert preview.summary == "BUY 10 AAPL STK LMT 150.00 DAY"
    assert preview.what_if is not None
    assert preview.what_if.init_margin_change == 1000.0
    assert preview.what_if.maint_margin_change == 800.0
    assert preview.what_if.equity_with_loan_change == -5.0
    assert preview.what_if.commission == 1.0
    assert preview.what_if.commission_currency == "USD"
    assert preview.what_if.warning_text is None
    [line] = preview.orders
    assert line.contract.con_id == 265598
    assert line.limit_price == 150.0
    assert line.notional == 1500.0
    assert "Initial margin change: 1,000.00" in preview.details
    assert "Commission: 1.00 USD" in preview.details

    contract, order = fake_ib.whatIfOrderAsync.call_args.args
    assert contract.conId == 265598
    assert order.account == PAPER_ACCOUNT
    assert order.lmtPrice == 150.0
    assert order.transmit is True
    fake_ib.placeOrder.assert_not_called()
    [event] = events_named(caplog, "preview")
    assert event["kind"] == "order"
    assert event["token"] == preview.token[:6] + "…"


async def test_preview_what_if_unset_values_become_null(
    service: OrdersService, fake_ib: MagicMock
) -> None:
    fake_ib.whatIfOrderAsync.side_effect = returns(
        order_state(
            initMarginChange=str(UNSET_DOUBLE),
            maintMarginChange="",
            equityWithLoanChange="nan",
            commission=UNSET_DOUBLE,
            minCommission=1.0,
            maxCommission=2.5,
            warningText="Order size is large",
        )
    )
    preview = await service.preview_order(lmt())
    what_if = preview.what_if
    assert what_if is not None
    assert what_if.init_margin_change is None
    assert what_if.maint_margin_change is None
    assert what_if.equity_with_loan_change is None
    assert what_if.commission is None
    assert "Commission: 1.00 to 2.50 USD" in preview.details
    assert "IBKR warning: Order size is large" in preview.details
    json.dumps(preview.model_dump(mode="json"))  # no NaN leaks into the output


async def test_preview_refused_by_limits_sends_nothing(
    make_gateway: MakeGateway, fake_ib: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    service = (await make_gateway(max_quantity=5)).orders
    with pytest.raises(OrderLimitError, match="quantity 10 exceeds the maximum of 5"):
        await service.preview_order(lmt())
    fake_ib.whatIfOrderAsync.assert_not_called()
    [event] = events_named(caplog, "rejected")
    assert event["stage"] == "preview"
    assert event["code"] == "order_limit"


async def test_market_order_notional_uses_a_snapshot_price(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    service = (await make_gateway(max_notional=5000)).orders
    fake_ib.reqTickersAsync.side_effect = returns([ticker(stock(), last=100.0, ask=100.5)])
    preview = await service.preview_order(mkt(10))
    [line] = preview.orders
    assert line.reference_price == 100.5
    assert line.notional == pytest.approx(1005.0)

    with pytest.raises(OrderLimitError, match="exceeds the maximum of 5,000 USD"):
        await service.preview_order(mkt(100))


async def test_reference_price_comes_from_a_live_ticker(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    service = (await make_gateway(max_notional=5000)).orders
    fake_ib.ticker.return_value = ticker(
        stock(), last=101.0, bid=100.9, ask=101.1, time=datetime.now(UTC)
    )
    preview = await service.preview_order(mkt(10, action="SELL"))
    assert preview.orders[0].reference_price == 101.1
    fake_ib.reqTickersAsync.assert_not_called()


async def test_a_stale_cached_ticker_is_not_trusted(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    service = (await make_gateway(max_notional=5000)).orders
    fake_ib.ticker.return_value = ticker(stock(), last=1.0, ask=1.0, time=FIXED_TIME)
    fake_ib.reqTickersAsync.side_effect = returns([ticker(stock(), last=100.0, ask=100.5)])
    preview = await service.preview_order(mkt(10))
    assert preview.orders[0].reference_price == 100.5
    fake_ib.reqTickersAsync.assert_called_once()


async def test_buy_limit_needs_no_reference_price(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    service = (await make_gateway(max_notional=5000)).orders
    preview = await service.preview_order(lmt(10, 150.0))
    assert preview.orders[0].notional == 1500.0
    fake_ib.reqTickersAsync.assert_not_called()


async def test_no_reference_price_refuses_under_a_notional_limit(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    service = (await make_gateway(max_notional=5000)).orders
    fake_ib.reqTickersAsync.side_effect = raises(
        RequestError(9, 354, "Requested market data is not subscribed.")
    )
    with pytest.raises(OrderLimitError, match="no reference price was available"):
        await service.preview_order(mkt(1))


async def test_option_notional_uses_the_contract_multiplier(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    service = (await make_gateway(max_notional=500)).orders
    fake_ib.reqContractDetailsAsync.side_effect = details_for(option())
    spec = OrderSpec(
        contract=ContractSpec(
            symbol="AAPL",
            sec_type="OPT",
            last_trade_date_or_contract_month="20261218",
            strike=200,
            right="C",
        ),
        action="BUY",
        quantity=2,
        order_type="LMT",
        limit_price=5.0,
    )
    with pytest.raises(OrderLimitError, match=r"notional 1,000\.00 USD"):
        await service.preview_order(spec)

    service = (await make_gateway(max_notional=2000)).orders
    preview = await service.preview_order(spec)
    assert preview.orders[0].notional == 1000.0
    assert preview.summary == "BUY 2 AAPL OPT 20261218 200 C LMT 5.00 DAY"


async def test_what_if_rejection_is_an_ib_error(service: OrdersService, fake_ib: MagicMock) -> None:
    fake_ib.whatIfOrderAsync.side_effect = raises(
        RequestError(7, 201, "Order rejected - reason: insufficient margin")
    )
    with pytest.raises(IbApiError, match="201") as caught:
        await service.preview_order(lmt())
    assert caught.value.error_code == 201


async def test_what_if_on_a_read_only_api_fails_fast(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    service = (await make_gateway(request_timeout=5.0)).orders

    def read_only(contract: Contract, _order: Order) -> asyncio.Future[Any]:
        fake_ib.errorEvent.emit(42, 321, READ_ONLY_TEXT, contract)
        return asyncio.get_running_loop().create_future()

    fake_ib.whatIfOrderAsync.side_effect = read_only
    started = time.monotonic()
    with pytest.raises(IbApiError, match="Read-Only") as caught:
        await service.preview_order(lmt())
    assert caught.value.error_code == 321
    assert time.monotonic() - started < 2


async def test_a_321_for_another_request_does_not_end_the_what_if(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    service = (await make_gateway(request_timeout=0.3)).orders

    def other(_contract: Contract, _order: Order) -> asyncio.Future[Any]:
        fake_ib.errorEvent.emit(43, 321, "some other request", stock())
        return asyncio.get_running_loop().create_future()

    fake_ib.whatIfOrderAsync.side_effect = other
    with pytest.raises(RequestTimeoutError, match="what-if check"):
        await service.preview_order(lmt())


async def test_what_if_timeout(make_gateway: MakeGateway, fake_ib: MagicMock) -> None:
    service = (await make_gateway(request_timeout=0.2)).orders
    fake_ib.whatIfOrderAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match="Timed out"):
        await service.preview_order(lmt())


async def test_preview_unknown_contract(service: OrdersService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(
        RequestError(3, 200, "No security definition has been found")
    )
    with pytest.raises(NotFoundError, match="No contract matches"):
        await service.preview_order(lmt())


async def test_preview_account_outside_the_allowlist(
    service: OrdersService, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(AccountNotAllowedError):
        await service.preview_order(lmt(), account=UNMANAGED)
    [event] = events_named(caplog, "rejected")
    assert event["code"] == "account_not_allowed"


async def test_previews_need_trading(make_gateway: MakeGateway, fake_ib: MagicMock) -> None:
    service = (await make_gateway(profile="readonly")).orders
    with pytest.raises(ConfigurationError, match="Order tools are disabled"):
        await service.preview_order(lmt())
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_algo_and_good_till_date_reach_the_order(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    spec = lmt(
        tif="GTD",
        good_till_date=datetime(2026, 12, 18, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        algo=AdaptiveAlgo(strategy="Adaptive", priority="Patient"),
        outside_rth=True,
        order_ref="note",
    )
    preview = await service.preview_order(spec)
    [line] = preview.orders
    assert line.good_till_date == "20261218-21:00:00"
    assert line.algo_strategy == "Adaptive"
    assert line.algo_params == {"adaptivePriority": "Patient"}
    assert "until 20261218-21:00:00 UTC outside RTH algo Adaptive" in preview.summary

    await service.submit(preview.token)
    [(_contract, order)] = [call.args for call in fake_ib.placeOrder.call_args_list]
    assert order.goodTillDate == "20261218-21:00:00"
    assert order.tif == "GTD"
    assert order.outsideRth is True
    assert order.orderRef == "note"
    assert order.algoStrategy == "Adaptive"
    assert [(tag.tag, tag.value) for tag in order.algoParams] == [("adaptivePriority", "Patient")]
    assert book.trades[0].order is order


async def test_order_attributes_reach_the_order(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    spec = lmt(
        quantity=100,
        good_after_time=datetime(2026, 12, 18, 9, 45, tzinfo=ZoneInfo("America/New_York")),
        all_or_none=True,
        display_size=10,
        algo=PctVolAlgo(strategy="PctVol", pct_vol=0.05),
        model_code="Growth",
        soft_dollar_tier=SoftDollarTierRef(name="Research", value="R1"),
    )
    preview = await service.preview_order(spec)
    [line] = preview.orders
    assert line.good_after_time == "20261218-14:45:00"
    assert (line.all_or_none, line.hidden, line.display_size) == (True, False, 10)
    assert (line.model_code, line.soft_dollar_tier) == ("Growth", "Research")
    assert line.algo_params == {
        "pctVol": "0.05",
        "startTime": "",
        "endTime": "",
        "noTakeLiq": "0",
    }
    assert preview.summary == (
        "BUY 100 AAPL STK LMT 150.00 DAY from 20261218-14:45:00 UTC all-or-none display 10 "
        'algo PctVol model "Growth" soft dollar tier "Research"'
    )
    assert "Algo PctVol: pctVol=0.05, startTime=, endTime=, noTakeLiq=0" in preview.details
    _contract, what_if = fake_ib.whatIfOrderAsync.call_args.args
    assert what_if.modelCode == "Growth"

    await service.submit(preview.token)
    [(_contract, order)] = [call.args for call in fake_ib.placeOrder.call_args_list]
    assert order.account == PAPER_ACCOUNT  # model orders stay in the (allowed) account
    assert order.goodAfterTime == "20261218-14:45:00"
    assert (order.allOrNone, order.hidden, order.displaySize) == (True, False, 10)
    assert order.modelCode == "Growth"
    assert (order.softDollarTier.name, order.softDollarTier.val) == ("Research", "R1")
    assert order.algoStrategy == "PctVol"


async def test_hidden_peg_mid_order(service: OrdersService, fake_ib: MagicMock) -> None:
    spec = OrderSpec(
        contract=ContractSpec(symbol="AAPL", exchange="ISLAND"),
        action="SELL",
        quantity=5,
        order_type="PEG MID",
        aux_price=0.01,
        limit_price=149.5,
        hidden=True,
    )
    preview = await service.preview_order(spec)
    assert preview.summary == "SELL 5 AAPL STK PEG MID offset 0.01 limit 149.50 DAY hidden"
    await service.submit(preview.token)
    order = fake_ib.placeOrder.call_args.args[1]
    assert (order.orderType, order.auxPrice, order.lmtPrice) == ("PEG MID", 0.01, 149.5)
    assert order.hidden is True
    assert not order.softDollarTier  # none asked for: IB's empty tier


async def test_bracket_and_combo_carry_model_and_soft_dollar_tier(
    service: OrdersService, fake_ib: MagicMock
) -> None:
    tier = SoftDollarTierRef(name="Research", value="R1")
    bracket = await service.preview_bracket(
        BracketSpec(
            contract=AAPL,
            action="BUY",
            quantity=10,
            entry_price=150.0,
            take_profit_price=160.0,
            stop_loss_price=140.0,
            model_code="Growth",
            soft_dollar_tier=tier,
        )
    )
    await service.submit(bracket.token)
    orders = [call.args[1] for call in fake_ib.placeOrder.call_args_list]
    assert [o.modelCode for o in orders] == ["Growth"] * 3
    assert {o.softDollarTier.val for o in orders} == {"R1"}

    long_call, short_call, legs = _spread_legs()
    fake_ib.reqContractDetailsAsync.side_effect = details_for(long_call, short_call)
    combo = await service.preview_combo(
        ComboSpec(
            legs=legs,
            action="BUY",
            quantity=1,
            limit_price=1.0,
            model_code="Growth",
            soft_dollar_tier=tier,
        )
    )
    assert combo.orders[0].model_code == "Growth"
    await service.submit(combo.token)
    placed = fake_ib.placeOrder.call_args.args[1]
    assert (placed.modelCode, placed.softDollarTier.name) == ("Growth", "Research")


@pytest.mark.parametrize(
    ("fields", "summary"),
    [
        (
            {"order_type": "TRAIL", "trailing_percent": 2.5, "trail_stop_price": 140.0},
            "SELL 5 AAPL STK TRAIL trail 2.5% stop 140.00 DAY",
        ),
        (
            {
                "order_type": "TRAIL LIMIT",
                "aux_price": 1.0,
                "trail_stop_price": 140.0,
                "limit_price_offset": 0.25,
            },
            "SELL 5 AAPL STK TRAIL LIMIT trail 1.00 stop 140.00 limit offset 0.25 DAY",
        ),
        (
            {
                "order_type": "TRAIL LIMIT",
                "aux_price": 1.0,
                "trail_stop_price": 140.0,
                "limit_price": 139.5,
            },
            "SELL 5 AAPL STK TRAIL LIMIT trail 1.00 stop 140.00 limit 139.50 DAY",
        ),
        (
            {"order_type": "STP LMT", "aux_price": 149.0, "limit_price": 148.5},
            "SELL 5 AAPL STK STP LMT stop 149.00 limit 148.50 DAY",
        ),
        (
            {"order_type": "REL", "aux_price": 0.01, "limit_price": 150.0},
            "SELL 5 AAPL STK REL offset 0.01 limit 150.00 DAY",
        ),
        (
            {"order_type": "LIT", "aux_price": 151.0, "limit_price": 150.995},
            "SELL 5 AAPL STK LIT trigger 151.00 limit 150.995 DAY",
        ),
        ({"order_type": "MOC"}, "SELL 5 AAPL STK MOC DAY"),
    ],
)
async def test_order_descriptions(
    service: OrdersService, fake_ib: MagicMock, fields: dict[str, Any], summary: str
) -> None:
    spec = OrderSpec.model_validate(
        {"contract": {"symbol": "AAPL"}, "action": "SELL", "quantity": 5, **fields}
    )
    preview = await service.preview_order(spec)
    assert preview.summary == summary
    _contract, order = fake_ib.whatIfOrderAsync.call_args.args
    assert order.orderType == fields["order_type"]


# --- submit ---------------------------------------------------------------------------------


async def test_submit_places_exactly_the_previewed_order(
    service: OrdersService, fake_ib: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    preview = await service.preview_order(lmt())
    result = await service.submit(preview.token)

    [(contract, order)] = [call.args for call in fake_ib.placeOrder.call_args_list]
    assert contract.conId == 265598
    assert order.account == PAPER_ACCOUNT
    assert (order.action, order.totalQuantity, order.orderType) == ("BUY", 10.0, "LMT")
    assert order.lmtPrice == 150.0
    assert order.auxPrice == UNSET_DOUBLE
    assert result.accepted is True
    assert result.status == "Submitted"
    assert result.order_id == 101
    assert result.order_ids == [101]
    assert result.perm_id == 900_001
    assert result.remaining == 10.0
    assert result.filled == 0.0
    assert result.avg_fill_price is None
    assert result.orders[0].placed_by_this_server is True
    assert result.messages == []
    assert service.safety.breaker.consecutive_rejections == 0
    names = [event["event"] for event in audit_events(caplog)]
    assert names == ["preview", "submit", "submit_result"]
    assert events_named(caplog, "submit_result")[0]["order_ids"] == [101]


async def test_a_token_submits_once(service: OrdersService, fake_ib: MagicMock) -> None:
    preview = await service.preview_order(lmt())
    await service.submit(preview.token)
    with pytest.raises(TokenNotFoundError, match="already used"):
        await service.submit(preview.token)
    assert fake_ib.placeOrder.call_count == 1


async def test_an_expired_token_is_refused(
    service: OrdersService, fake_ib: MagicMock, clock: FakeClock
) -> None:
    preview = await service.preview_order(lmt())
    clock.advance(service.settings.token_ttl + 1)
    with pytest.raises(TokenExpiredError):
        await service.submit(preview.token)
    fake_ib.placeOrder.assert_not_called()


async def test_submit_is_rate_limited(make_gateway: MakeGateway, fake_ib: MagicMock) -> None:
    service = (await make_gateway(max_orders_per_minute=1)).orders
    first = await service.preview_order(lmt())
    second = await service.preview_order(lmt())
    await service.submit(first.token)
    with pytest.raises(RateLimitError, match="at most 1 orders"):
        await service.submit(second.token)
    assert fake_ib.placeOrder.call_count == 1


async def test_rejections_open_the_circuit_breaker(
    make_gateway: MakeGateway,
    fake_ib: MagicMock,
    book: OrderBook,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = (await make_gateway(breaker_rejects=2)).orders
    book.reply = book.rejected
    for _ in range(2):
        preview = await service.preview_order(lmt())
        result = await service.submit(preview.token)
        assert result.accepted is False
        assert result.status == "Cancelled"
        assert any("Order rejected - reason: margin" in m for m in result.messages)
    assert service.safety.breaker.is_open
    assert events_named(caplog, "circuit_open")

    preview = await service.preview_order(lmt())
    with pytest.raises(CircuitOpenError, match="Stop placing orders"):
        await service.submit(preview.token)
    assert fake_ib.placeOrder.call_count == 2


async def test_a_success_resets_the_rejection_count(
    make_gateway: MakeGateway, book: OrderBook
) -> None:
    service = (await make_gateway(breaker_rejects=3)).orders
    book.reply = book.rejected
    await service.submit((await service.preview_order(lmt())).token)
    assert service.safety.breaker.consecutive_rejections == 1
    book.reply = book.submitted
    await service.submit((await service.preview_order(lmt())).token)
    assert service.safety.breaker.consecutive_rejections == 0


async def test_warning_321_on_a_new_order_is_a_rejection(
    service: OrdersService, book: OrderBook
) -> None:
    book.reply = book.read_only
    result = await service.submit((await service.preview_order(lmt())).token)
    assert result.accepted is False
    assert result.status == "Inactive"  # ib_async leaves it ValidationError, a 'working' state
    assert "Read-Only" in result.messages[0]
    assert book.trades[0].isDone()
    with pytest.raises(NotFoundError, match="no working orders"):
        await service.preview_cancel_all()  # the never-placed order is not listed


async def test_ibkr_silence_is_reported(service: OrdersService, book: OrderBook) -> None:
    service.status_wait = 0.05
    book.reply = None
    result = await service.submit((await service.preview_order(lmt())).token)
    assert result.accepted is True
    assert result.status == "PendingSubmit"
    assert "no status within 0.05 s" in result.messages[-1]


async def test_submit_rechecks_the_limits(
    service: OrdersService,
    fake_ib: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    preview = await service.preview_order(lmt())

    def tightened(_summary: object) -> None:
        raise OrderLimitError("Order refused by the server's order limits: tightened.")

    monkeypatch.setattr(service.safety.policy, "check", tightened)
    with pytest.raises(OrderLimitError, match="tightened"):
        await service.submit(preview.token)
    fake_ib.placeOrder.assert_not_called()
    [event] = events_named(caplog, "rejected")
    assert event["stage"] == "submit"


async def test_submit_needs_trading(
    make_gateway: MakeGateway, fake_ib: MagicMock, book: OrderBook
) -> None:
    service = (await make_gateway(profile="readonly")).orders
    with pytest.raises(ConfigurationError):
        await service.submit("any-token")
    with pytest.raises(ConfigurationError):
        await service.cancel(1)
    fake_ib.placeOrder.assert_not_called()


async def test_live_orders_need_a_human(make_gateway: MakeGateway, fake_ib: MagicMock) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    service = (await make_gateway(allow_live=True)).orders
    preview = await service.preview_order(lmt())
    assert preview.is_paper is False
    assert any("Live (real-money) account" in warning for warning in preview.warnings)
    request = service.confirmation_request(preview.token)
    assert request.is_paper is False
    assert request.account == LIVE_ACCOUNT

    with pytest.raises(ConfirmationUnavailableError, match="Nothing was sent"):
        await service.submit(preview.token)
    fake_ib.placeOrder.assert_not_called()

    confirmed = await service.preview_order(lmt())
    result = await service.submit(confirmed.token, human_confirmed=True)
    assert result.accepted is True
    assert fake_ib.placeOrder.call_args.args[1].account == LIVE_ACCOUNT


async def test_live_confirm_off_places_without_a_human(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    service = (await make_gateway(allow_live=True, live_confirm=False)).orders
    result = await service.submit((await service.preview_order(lmt())).token)
    assert result.accepted is True


async def test_live_without_allow_live_is_refused(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    service = (await make_gateway()).orders
    with pytest.raises(LiveTradingDisabledError):
        await service.preview_order(lmt())


async def test_confirmation_request_peeks(service: OrdersService, fake_ib: MagicMock) -> None:
    preview = await service.preview_order(lmt())
    first = service.confirmation_request(preview.token)
    assert service.confirmation_request(preview.token) == first
    assert first.action == preview.summary
    assert first.details == preview.details
    assert first.is_paper is True
    await service.submit(preview.token)
    assert fake_ib.placeOrder.call_count == 1


async def test_discard_burns_the_token(
    service: OrdersService, caplog: pytest.LogCaptureFixture
) -> None:
    preview = await service.preview_order(lmt())
    service.discard(preview.token, reason="declined")
    service.discard(preview.token, reason="again")  # already gone: no error
    with pytest.raises(TokenNotFoundError):
        await service.submit(preview.token)
    rejected = events_named(caplog, "rejected")
    assert rejected[0]["stage"] == "confirmation"
    assert rejected[0]["reason"] == "declined"


async def test_connection_loss_while_placing_cancels_what_was_placed(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    preview = await service.preview_bracket(
        BracketSpec(
            contract=AAPL,
            action="BUY",
            quantity=1,
            entry_price=150.0,
            take_profit_price=160.0,
            stop_loss_price=140.0,
        )
    )
    placed: list[Order] = []

    def flaky(contract: Contract, order: Order) -> Trade:
        if placed:
            raise ConnectionError("Not connected")
        placed.append(order)
        return book.place_order(contract, order)

    fake_ib.placeOrder.side_effect = flaky
    with pytest.raises(NotConnectedError, match="get_open_orders"):
        await service.submit(preview.token)
    fake_ib.cancelOrder.assert_called_once_with(placed[0])


# --- bracket, OCA, combo ------------------------------------------------------------------


async def test_bracket_transmits_on_the_last_order(
    service: OrdersService, fake_ib: MagicMock
) -> None:
    preview = await service.preview_bracket(
        BracketSpec(
            contract=AAPL,
            action="BUY",
            quantity=10,
            entry_price=150.0,
            take_profit_price=160.0,
            stop_loss_price=140.0,
            tif="GTC",
        )
    )
    assert preview.kind == "bracket"
    assert [line.role for line in preview.orders] == ["entry", "take_profit", "stop_loss"]
    assert preview.summary.startswith("BRACKET BUY 10 AAPL STK LMT 150.00 GTC")
    assert fake_ib.whatIfOrderAsync.call_count == 1
    assert preview.what_if is not None
    assert preview.orders[1].what_if is None
    assert "Entry: BUY 10 AAPL STK LMT 150.00 GTC" in preview.details

    result = await service.submit(preview.token)
    orders = [call.args[1] for call in fake_ib.placeOrder.call_args_list]
    assert [o.transmit for o in orders] == [False, False, True]
    assert [o.action for o in orders] == ["BUY", "SELL", "SELL"]
    assert [o.orderType for o in orders] == ["LMT", "LMT", "STP"]
    assert orders[1].lmtPrice == 160.0
    assert orders[2].auxPrice == 140.0
    assert [o.parentId for o in orders] == [0, orders[0].orderId, orders[0].orderId]
    assert {o.tif for o in orders} == {"GTC"}
    assert result.kind == "bracket"
    assert result.order_ids == [101, 102, 103]
    assert [o.role for o in result.orders] == ["entry", "take_profit", "stop_loss"]
    assert result.orders[1].parent_id == 101


async def test_bracket_with_stop_limit_entry(service: OrdersService, fake_ib: MagicMock) -> None:
    await service.preview_bracket(
        BracketSpec(
            contract=AAPL,
            action="SELL",
            quantity=1,
            entry_type="STP LMT",
            entry_price=99.0,
            entry_stop_price=99.5,
            take_profit_price=90.0,
            stop_loss_price=105.0,
        )
    )
    _contract, entry = fake_ib.whatIfOrderAsync.call_args.args
    assert (entry.orderType, entry.lmtPrice, entry.auxPrice) == ("STP LMT", 99.0, 99.5)
    assert entry.parentId == 0
    assert entry.transmit is True  # the what-if copy is a standalone order


async def test_oca_group_shares_one_group(service: OrdersService, fake_ib: MagicMock) -> None:
    msft = stock("MSFT", 272093)
    fake_ib.reqContractDetailsAsync.side_effect = details_for(stock(), msft)
    preview = await service.preview_oca(
        OcaSpec(
            orders=[lmt(), lmt(5, 300.0, contract=ContractSpec(symbol="MSFT"))],
            oca_type=2,
        )
    )
    assert preview.kind == "oca"
    assert fake_ib.whatIfOrderAsync.call_count == 2
    assert all(line.what_if is not None for line in preview.orders)
    group = preview.orders[0].oca_group
    assert group
    assert group.startswith("mcp-oca-")
    assert preview.orders[1].oca_group == group
    assert "a fill reduces the others" in preview.summary

    result = await service.submit(preview.token)
    orders = [call.args[1] for call in fake_ib.placeOrder.call_args_list]
    assert [(o.ocaGroup, o.ocaType, o.transmit) for o in orders] == [(group, 2, True)] * 2
    assert [call.args[0].symbol for call in fake_ib.placeOrder.call_args_list] == ["AAPL", "MSFT"]
    assert result.order_ids == [101, 102]


def _spread_legs() -> tuple[Contract, Contract, list[ComboOrderLegSpec]]:
    long_call = option(strike=200.0, con_id=700001)
    short_call = option(strike=210.0, con_id=700002)

    def leg(strike: float, action: str) -> ComboOrderLegSpec:
        return ComboOrderLegSpec(
            contract=ContractSpec(
                symbol="AAPL",
                sec_type="OPT",
                last_trade_date_or_contract_month="20261218",
                strike=strike,
                right="C",
            ),
            action=action,  # type: ignore[arg-type]
        )

    return long_call, short_call, [leg(200.0, "BUY"), leg(210.0, "SELL")]


async def test_combo_builds_a_bag_from_qualified_legs(
    service: OrdersService, fake_ib: MagicMock
) -> None:
    long_call, short_call, legs = _spread_legs()
    fake_ib.reqContractDetailsAsync.side_effect = details_for(long_call, short_call)
    preview = await service.preview_combo(
        ComboSpec(legs=legs, action="BUY", quantity=2, limit_price=-1.25, non_guaranteed=True)
    )
    bag, order = fake_ib.whatIfOrderAsync.call_args.args
    assert bag.secType == "BAG"
    assert bag.symbol == "AAPL"
    assert bag.currency == "USD"
    assert [(leg.conId, leg.ratio, leg.action) for leg in bag.comboLegs] == [
        (700001, 1, "BUY"),
        (700002, 1, "SELL"),
    ]
    assert order.lmtPrice == -1.25
    assert [(tag.tag, tag.value) for tag in order.smartComboRoutingParams] == [
        ("NonGuaranteed", "1")
    ]
    assert preview.kind == "combo"
    assert "combo [BUY 1 AAPL OPT 20261218 200 C + SELL 1 AAPL OPT 20261218 210 C]" in (
        preview.summary
    )
    assert "non-guaranteed" in preview.summary
    assert preview.orders[0].contract.combo_legs[1].con_id == 700002

    await service.submit(preview.token)
    placed_bag, placed_order = fake_ib.placeOrder.call_args.args
    assert placed_bag.secType == "BAG"
    assert [leg.conId for leg in placed_bag.comboLegs] == [700001, 700002]
    assert placed_order.lmtPrice == -1.25


async def test_combo_notional_sums_the_legs(make_gateway: MakeGateway, fake_ib: MagicMock) -> None:
    service = (await make_gateway(max_notional=1000)).orders
    long_call, short_call, legs = _spread_legs()
    fake_ib.reqContractDetailsAsync.side_effect = details_for(long_call, short_call)

    async def snapshot(*contracts: Contract, regulatorySnapshot: bool = False) -> list[Any]:
        prices = {700001: 3.0, 700002: 1.5}
        return [ticker(c, last=prices[c.conId], bid=math.nan, ask=math.nan) for c in contracts]

    fake_ib.reqTickersAsync.side_effect = snapshot
    preview = await service.preview_combo(
        ComboSpec(legs=legs, action="BUY", quantity=2, limit_price=1.5)
    )
    # 2 x 3.00 x 100 + 2 x 1.50 x 100
    assert preview.orders[0].notional == 900.0

    with pytest.raises(OrderLimitError, match=r"notional 1,350\.00 USD"):
        await service.preview_combo(ComboSpec(legs=legs, action="BUY", quantity=3, limit_price=1.5))


async def test_combo_legs_must_share_a_currency(service: OrdersService, fake_ib: MagicMock) -> None:
    sap = stock("SAP", 14204)
    sap.currency = "EUR"
    fake_ib.reqContractDetailsAsync.side_effect = details_for(stock(), sap)
    legs = [
        ComboOrderLegSpec(contract=AAPL, action="BUY"),
        ComboOrderLegSpec(contract=ContractSpec(symbol="SAP", currency="EUR"), action="SELL"),
    ]
    with pytest.raises(InvalidRequestError, match="one currency"):
        await service.preview_combo(ComboSpec(legs=legs, action="BUY", quantity=1, limit_price=1))


async def test_combo_legs_must_differ(service: OrdersService) -> None:
    legs = [
        ComboOrderLegSpec(contract=AAPL, action="BUY"),
        ComboOrderLegSpec(contract=AAPL, action="SELL"),
    ]
    with pytest.raises(InvalidRequestError, match="different contract"):
        await service.preview_combo(ComboSpec(legs=legs, action="BUY", quantity=1, limit_price=1))


# --- modify -------------------------------------------------------------------------------


async def test_modify_changes_the_working_order_in_place(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook, caplog: pytest.LogCaptureFixture
) -> None:
    trade = book.add(order_id=55)
    preview = await service.preview_modify(55, ModifySpec(limit_price=151.0, quantity=12))
    assert preview.kind == "modify"
    assert preview.orders[0].order_id == 55
    assert preview.summary == (
        "MODIFY order 55: BUY 10 AAPL STK LMT 150.00 DAY -> BUY 12 AAPL STK LMT 151.00 DAY"
    )
    assert trade.order.lmtPrice == 150.0  # the preview does not touch the live order
    _contract, what_if_order = fake_ib.whatIfOrderAsync.call_args.args
    assert what_if_order.lmtPrice == 151.0
    assert what_if_order is not trade.order

    result = await service.submit(preview.token)
    contract, order = fake_ib.placeOrder.call_args.args
    assert order is trade.order
    assert contract is trade.contract
    assert (order.lmtPrice, order.totalQuantity, order.transmit) == (151.0, 12, True)
    assert result.kind == "modify"
    assert result.accepted is True
    assert result.order_id == 55
    assert events_named(caplog, "modify")


async def test_modify_refusals(service: OrdersService, book: OrderBook) -> None:
    with pytest.raises(NotFoundError, match="No order 99 placed by this server"):
        await service.preview_modify(99, ModifySpec(limit_price=1.0))

    book.add(order_id=56, client_id=7)
    with pytest.raises(InvalidRequestError, match="placed by API client 7"):
        await service.preview_modify(56, ModifySpec(limit_price=1.0))

    book.add(order_id=57, status=OrderStatus.Filled)
    with pytest.raises(InvalidRequestError, match="Filled"):
        await service.preview_modify(57, ModifySpec(limit_price=1.0))

    book.add(order_id=58, orderType="MKT", lmtPrice=UNSET_DOUBLE)
    with pytest.raises(InvalidRequestError, match="MKT orders have no limit price"):
        await service.preview_modify(58, ModifySpec(limit_price=1.0))
    with pytest.raises(InvalidRequestError, match="tif GTD needs good_till_date"):
        await service.preview_modify(58, ModifySpec(tif="GTD"))


async def test_modify_is_refused_when_the_order_changed(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    trade = book.add(order_id=60)
    preview = await service.preview_modify(60, ModifySpec(limit_price=151.0))
    trade.order.totalQuantity = 5  # e.g. changed from TWS meanwhile
    with pytest.raises(InvalidRequestError, match="changed since the preview"):
        await service.submit(preview.token)
    fake_ib.placeOrder.assert_not_called()


async def test_modify_of_an_order_filled_meanwhile(service: OrdersService, book: OrderBook) -> None:
    trade = book.add(order_id=61)
    preview = await service.preview_modify(61, ModifySpec(limit_price=151.0))
    trade.orderStatus.status = OrderStatus.Filled
    with pytest.raises(InvalidRequestError, match="no longer be modified"):
        await service.submit(preview.token)


async def test_modify_to_gtd_formats_the_date(service: OrdersService, book: OrderBook) -> None:
    book.add(order_id=62)
    preview = await service.preview_modify(
        62,
        ModifySpec(tif="GTD", good_till_date=datetime(2026, 12, 18, 21, 0, tzinfo=ZoneInfo("UTC"))),
    )
    assert preview.orders[0].good_till_date == "20261218-21:00:00"
    assert preview.orders[0].tif == "GTD"


async def test_modify_a_trailing_order(service: OrdersService, book: OrderBook) -> None:
    book.add(
        order_id=63,
        action="SELL",
        orderType="TRAIL",
        lmtPrice=UNSET_DOUBLE,
        trailingPercent=2.0,
    )
    preview = await service.preview_modify(63, ModifySpec(aux_price=1.5, outside_rth=True))
    [line] = preview.orders
    assert (line.aux_price, line.trailing_percent, line.outside_rth) == (1.5, None, True)
    back = await service.preview_modify(
        63, ModifySpec(trailing_percent=3.0, trail_stop_price=140.0, tif="GTC")
    )
    assert back.orders[0].trailing_percent == 3.0
    assert back.orders[0].trail_stop_price == 140.0
    assert back.orders[0].tif == "GTC"
    book.add(order_id=64)
    with pytest.raises(InvalidRequestError, match="only apply to trailing orders"):
        await service.preview_modify(64, ModifySpec(trailing_percent=1.0))


# --- exercise -----------------------------------------------------------------------------

OPTION_SPEC = ContractSpec(
    symbol="AAPL",
    sec_type="OPT",
    last_trade_date_or_contract_month="20261218",
    strike=200,
    right="C",
)


async def test_exercise_flow(service: OrdersService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = details_for(option())
    fake_ib.positions.return_value = [Position(PAPER_ACCOUNT, option(), 2.0, 500.0)]
    preview = await service.preview_exercise(
        ExerciseSpec(contract=OPTION_SPEC, action="exercise", quantity=2)
    )
    assert preview.kind == "exercise"
    assert preview.summary == "EXERCISE 2 AAPL OPT 20261218 200 C (x100)"
    assert "Exercising buys 200 AAPL at 200.00 USD." in preview.details
    assert preview.warnings == []
    assert preview.orders[0].notional == 40_000.0
    fake_ib.whatIfOrderAsync.assert_not_called()

    result = await service.submit(preview.token)
    req_id, contract, action, quantity, account, override = (
        fake_ib.client.exerciseOptions.call_args.args
    )
    assert (req_id, action, quantity, account, override) == (101, 1, 2, PAPER_ACCOUNT, 0)
    assert contract.conId == 700001
    assert result.kind == "exercise"
    assert result.status == "Sent"
    assert result.accepted is True


async def test_exercise_error_is_a_rejection(service: OrdersService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = details_for(option())

    def refuse(req_id: int, *_args: Any) -> None:
        fake_ib.errorEvent.emit(req_id, 322, "Error processing request: no position", None)

    fake_ib.client.exerciseOptions.side_effect = refuse
    preview = await service.preview_exercise(
        ExerciseSpec(contract=OPTION_SPEC, action="lapse", quantity=1, override=True)
    )
    assert any("No long position" in warning for warning in preview.warnings)
    assert "with override" in preview.summary
    result = await service.submit(preview.token)
    assert result.accepted is False
    assert result.status == "Rejected"
    assert result.messages == ["IB error 322: Error processing request: no position"]
    assert fake_ib.client.exerciseOptions.call_args.args[2] == 2  # lapse
    assert service.safety.breaker.consecutive_rejections == 1


async def test_exercise_needs_an_option(service: OrdersService) -> None:
    with pytest.raises(InvalidRequestError, match="Only options"):
        await service.preview_exercise(ExerciseSpec(contract=AAPL, action="exercise", quantity=1))


# --- cancel-all, cancel, status -----------------------------------------------------------


async def test_cancel_all_this_client(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    mine = [book.add(order_id=70), book.add(order_id=71, action="SELL")]
    book.add(order_id=72, client_id=7)
    book.add(order_id=73, account=OTHER_PAPER)
    book.add(order_id=74, status=OrderStatus.Filled)
    preview = await service.preview_cancel_all()
    assert preview.kind == "cancel_all"
    assert [line.order_id for line in preview.orders] == [70, 71]
    assert preview.summary.startswith("CANCEL 2 working order(s) placed by this server")

    result = await service.submit(preview.token)
    assert [call.args[0] for call in fake_ib.cancelOrder.call_args_list] == [
        trade.order for trade in mine
    ]
    assert result.status == "Cancelled"
    assert result.order_ids == [70, 71]
    fake_ib.reqGlobalCancel.assert_not_called()


async def test_cancel_all_skips_orders_that_finished(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    done, working = book.add(order_id=75), book.add(order_id=76)
    preview = await service.preview_cancel_all()
    done.orderStatus.status = OrderStatus.Filled
    result = await service.submit(preview.token)
    assert fake_ib.cancelOrder.call_args.args[0] is working.order
    assert "Order 75 was skipped: it is Filled." in result.messages


async def test_cancel_all_with_nothing_to_cancel(service: OrdersService) -> None:
    with pytest.raises(NotFoundError, match="nothing to cancel"):
        await service.preview_cancel_all()


async def test_global_cancel_needs_every_account_allowed(
    make_gateway: MakeGateway, fake_ib: MagicMock, book: OrderBook
) -> None:
    fake_ib.managedAccounts.return_value = [PAPER_ACCOUNT, OTHER_PAPER]
    service = (await make_gateway(ib_account=PAPER_ACCOUNT)).orders
    book.add(order_id=80)
    with pytest.raises(AccountNotAllowedError, match="2 managed, 1 allowed"):
        await service.preview_cancel_all(scope="global")


async def test_global_cancel_needs_the_operator(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    book.add(order_id=80)
    with pytest.raises(ConfigurationError, match="IBKR_MCP_ALLOW_GLOBAL_CANCEL"):
        await service.preview_cancel_all(scope="global")
    fake_ib.reqAllOpenOrdersAsync.assert_not_called()


async def test_global_cancel(
    make_gateway: MakeGateway, fake_ib: MagicMock, book: OrderBook
) -> None:
    service = (await make_gateway(allow_global_cancel=True)).orders
    trades = [book.add(order_id=81), book.add(order_id=82, client_id=7)]
    preview = await service.preview_cancel_all(scope="global")
    assert [line.order_id for line in preview.orders] == [81, 82]
    assert preview.summary.startswith("CANCEL ALL 2 working orders on this login")

    def global_cancel() -> None:
        for trade in trades:
            asyncio.get_running_loop().call_soon(book.cancelled, trade)

    fake_ib.reqGlobalCancel.side_effect = global_cancel
    # The other client's order reaches this client only through IBKR's answers.
    fake_ib.reqCompletedOrdersAsync.side_effect = lambda *_: book.completed_orders()
    result = await service.submit(preview.token)
    fake_ib.reqGlobalCancel.assert_called_once_with()
    fake_ib.cancelOrder.assert_not_called()
    assert result.status == "Cancelled"
    assert [order.status for order in result.orders] == ["Cancelled", "Cancelled"]


async def test_cancel_all_works_while_the_breaker_is_open(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    book.add(order_id=83)
    for _ in range(service.settings.breaker_rejects):
        service.safety.breaker.record_rejection("test")
    assert service.safety.breaker.is_open
    result = await service.submit((await service.preview_cancel_all()).token)
    assert result.status == "Cancelled"


async def test_cancel_order(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook, caplog: pytest.LogCaptureFixture
) -> None:
    trade = book.add(order_id=90)
    result = await service.cancel(90)
    fake_ib.cancelOrder.assert_called_once_with(trade.order)
    assert result.kind == "cancel"
    assert result.status == "Cancelled"
    assert result.accepted is True
    assert result.order_ids == [90]
    [event] = events_named(caplog, "cancel")
    assert event["order_id"] == 90


async def test_cancel_order_unconfirmed(service: OrdersService, book: OrderBook) -> None:
    service.status_wait = 0.05
    book.cancel_reply = None
    book.add(order_id=91)
    result = await service.cancel(91)
    assert result.status == "PendingCancel"
    assert "has not confirmed the cancellation" in result.messages[-1]


async def test_cancel_declined_is_audited_and_sends_nothing(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook, caplog: pytest.LogCaptureFixture
) -> None:
    book.add(order_id=92)
    service.cancel_declined(92, reason="The human declined the cancellation.")
    [event] = events_named(caplog, "rejected")
    assert event["stage"] == "confirmation"
    assert event["kind"] == "cancel"
    assert event["order_id"] == 92
    assert event["reason"] == "The human declined the cancellation."
    fake_ib.cancelOrder.assert_not_called()


async def test_cancel_order_refusals(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    with pytest.raises(NotFoundError):
        await service.cancel(92)
    fake_ib.reqOpenOrdersAsync.assert_called()
    book.add(order_id=93, client_id=7)
    with pytest.raises(InvalidRequestError, match="placed by API client 7"):
        await service.cancel(93)
    book.add(order_id=94, status=OrderStatus.Cancelled)
    with pytest.raises(InvalidRequestError, match="already Cancelled"):
        await service.cancel(94)
    book.add(order_id=95, account=UNMANAGED)
    with pytest.raises(AccountNotAllowedError):
        await service.cancel(95)
    fake_ib.cancelOrder.assert_not_called()


async def test_cancel_order_found_after_refreshing_open_orders(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    trade = book.add(order_id=96)
    book.trades.remove(trade)  # not in the cache until the open orders are loaded

    async def load_open_orders() -> list[Trade]:
        book.trades.append(trade)
        return [trade]

    fake_ib.reqOpenOrdersAsync.side_effect = load_open_orders
    result = await service.cancel(96)
    assert fake_ib.cancelOrder.call_args.args[0] is trade.order
    assert result.order_id == 96


async def test_order_status_by_order_id(service: OrdersService, book: OrderBook) -> None:
    trade = book.add(order_id=100, status=OrderStatus.Filled)
    trade.orderStatus.filled = 10.0
    trade.orderStatus.remaining = 0.0
    trade.orderStatus.avgFillPrice = 149.5
    trade.orderStatus.lastFillPrice = math.nan
    trade.fills.append(
        make_fill(
            execution(orderId=100, shares=10.0, price=149.5), commission_report(commission=1.2)
        )
    )
    trade.fills.append(
        make_fill(execution(execId="0002", shares=0.0), commission_report(execId=""))
    )
    trade.log.append(TradeLogEntry(FIXED_TIME, "Filled", "Fill 10.0@149.5"))

    status = await service.order_status(order_id=100)
    assert status.status == "Filled"
    assert (status.filled, status.remaining, status.avg_fill_price) == (10.0, 0.0, 149.5)
    assert status.last_fill_price is None
    assert status.placed_by_this_server is True
    assert status.fills[0].commission == 1.2
    assert status.fills[0].price == 149.5
    assert status.fills[1].commission is None
    assert status.log[-1].message == "Fill 10.0@149.5"
    assert status.log[0].error_code is None
    assert status.account == PAPER_ACCOUNT


async def test_order_status_by_perm_id_asks_for_all_open_orders(
    service: OrdersService, fake_ib: MagicMock
) -> None:
    other = OrderBook(MagicMock()).add(order_id=0, client_id=0, perm_id=4242)
    fake_ib.reqAllOpenOrdersAsync.side_effect = returns([other])
    status = await service.order_status(perm_id=4242)
    assert status.perm_id == 4242
    assert status.order_id is None
    assert status.placed_by_this_server is False


async def test_order_status_falls_back_to_completed_orders(
    service: OrdersService, fake_ib: MagicMock
) -> None:
    done = OrderBook(MagicMock()).add(order_id=5, perm_id=4343, status=OrderStatus.Cancelled)
    fake_ib.reqCompletedOrdersAsync.side_effect = returns([done])
    status = await service.order_status(perm_id=4343)
    assert status.status == "Cancelled"
    assert fake_ib.reqCompletedOrdersAsync.call_args.args == (False,)


async def test_order_status_hides_other_accounts(service: OrdersService, book: OrderBook) -> None:
    book.add(order_id=101, account=UNMANAGED)
    with pytest.raises(NotFoundError, match="allowed account"):
        await service.order_status(order_id=101)


async def test_order_status_needs_exactly_one_id(service: OrdersService) -> None:
    with pytest.raises(InvalidRequestError, match="exactly one"):
        await service.order_status()
    with pytest.raises(InvalidRequestError, match="exactly one"):
        await service.order_status(order_id=1, perm_id=2)


# --- review findings: safety regressions --------------------------------------------------


def _bag(*con_ids: int) -> Contract:
    bag = Contract(secType="BAG", symbol="AAPL", currency="USD", exchange="SMART")
    bag.comboLegs = [
        ComboLeg(conId=con_id, ratio=1, action=action, exchange="SMART")
        for con_id, action in zip(con_ids, ("BUY", "SELL"), strict=False)
    ]
    return bag


async def test_combo_with_an_unpriced_leg_is_refused_under_a_notional_limit(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    """A cheap net BUY limit must not stand in for the legs' gross value."""
    service = (await make_gateway(max_notional=10_000)).orders
    ford, nvda = stock("F", 9599491), stock("NVDA", 4815747)
    fake_ib.reqContractDetailsAsync.side_effect = details_for(ford, nvda)

    async def snapshot(*contracts: Contract, regulatorySnapshot: bool = False) -> list[Any]:
        return [ticker(c, last=12.0) for c in contracts if c.conId == ford.conId]  # NVDA: none

    fake_ib.reqTickersAsync.side_effect = snapshot
    legs = [
        ComboOrderLegSpec(contract=ContractSpec(symbol="F"), action="BUY"),
        ComboOrderLegSpec(contract=ContractSpec(symbol="NVDA"), action="SELL"),
    ]
    spec = ComboSpec(
        legs=legs, action="BUY", quantity=10_000, limit_price=0.01, non_guaranteed=True
    )
    with pytest.raises(OrderLimitError, match=r"combo leg NVDA \(STK\) has no reference price"):
        await service.preview_combo(spec)
    fake_ib.whatIfOrderAsync.assert_not_called()


async def test_a_rejected_modification_keeps_the_original_order(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    """ib_async marks the trade Cancelled on the error; IBKR keeps the order working."""
    trade = book.add(order_id=55)
    book.ibkr_status[55] = OrderStatus.Submitted  # what IBKR still holds
    preview = await service.preview_modify(55, ModifySpec(limit_price=151.0))
    book.reply = lambda t: book.ib_error(t, 201, "Order rejected - reason: price")
    result = await service.submit(preview.token)

    assert result.accepted is False
    assert result.status == "Submitted"
    assert any("Order rejected - reason: price" in m for m in result.messages)
    assert any("original order is still working" in m for m in result.messages)
    assert trade.order.lmtPrice == 150.0  # the rejected terms are not left in the cache
    fake_ib.reqOpenOrdersAsync.assert_called()
    assert service.safety.breaker.consecutive_rejections == 1

    cancelled = await service.cancel(55)
    assert fake_ib.cancelOrder.call_args.args[0] is trade.order
    assert cancelled.status == "Cancelled"


@pytest.mark.parametrize("code", [105, 110, 329])
async def test_a_modification_refused_with_a_warning_code_is_not_accepted(
    service: OrdersService, book: OrderBook, code: int
) -> None:
    trade = book.add(order_id=56)
    book.ibkr_status[56] = OrderStatus.Submitted
    preview = await service.preview_modify(56, ModifySpec(limit_price=151.0, quantity=12))
    book.reply = lambda t: book.ib_error(t, code, "Order modify failed", warning=True)
    result = await service.submit(preview.token)
    assert result.accepted is False
    assert result.status == "Submitted"
    assert (trade.order.lmtPrice, trade.order.totalQuantity) == (150.0, 10.0)


async def test_a_rejected_modification_without_a_resync_says_so(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    book.add(order_id=57)
    preview = await service.preview_modify(57, ModifySpec(limit_price=151.0))
    book.reply = lambda t: book.ib_error(t, 201, "Order rejected - reason: price")
    fake_ib.reqOpenOrdersAsync.side_effect = raises(ConnectionError("Not connected"))
    result = await service.submit(preview.token)
    assert result.accepted is False
    assert any("could not be re-checked" in m and "get_open_orders" in m for m in result.messages)


async def test_cancel_resyncs_a_trade_an_error_marked_cancelled(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    trade = book.add(order_id=58)
    book.ib_error(trade, 201, "Order rejected - reason: modify")  # e.g. from an earlier session
    book.ibkr_status[58] = OrderStatus.Submitted
    status = await service.order_status(order_id=58)
    assert status.status == "Submitted"
    result = await service.cancel(58)
    assert fake_ib.cancelOrder.call_args.args[0] is trade.order
    assert result.status == "Cancelled"

    gone = book.add(order_id=59)
    book.ib_error(gone, 201, "Order rejected - reason: margin")  # really gone at IBKR
    with pytest.raises(InvalidRequestError, match="already Cancelled"):
        await service.cancel(59)


async def test_modify_of_a_presubmitted_stop_is_acknowledged_by_the_echo(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    """Only an openOrder echo arrives when the order status itself does not change."""
    book.add(
        order_id=65,
        status=OrderStatus.PreSubmitted,
        action="SELL",
        orderType="STP",
        lmtPrice=UNSET_DOUBLE,
        auxPrice=140.0,
    )
    service.safety.breaker.record_rejection("earlier")
    service.status_wait = 3.0
    preview = await service.preview_modify(65, ModifySpec(aux_price=141.0))
    book.reply = fake_ib.openOrderEvent.emit
    started = time.monotonic()
    result = await service.submit(preview.token)
    assert time.monotonic() - started < 1.0
    assert result.accepted is True
    assert result.status == "PreSubmitted"
    assert not any("no status" in m for m in result.messages)
    assert service.safety.breaker.consecutive_rejections == 0


async def test_modification_rules_match_new_orders(service: OrdersService, book: OrderBook) -> None:
    book.add(
        order_id=66,
        action="SELL",
        orderType="TRAIL LIMIT",
        lmtPrice=UNSET_DOUBLE,
        auxPrice=1.0,
        lmtPriceOffset=0.25,
    )
    preview = await service.preview_modify(66, ModifySpec(limit_price=139.0))
    [line] = preview.orders
    assert (line.limit_price, line.limit_price_offset) == (139.0, None)
    with pytest.raises(InvalidRequestError, match="not both"):
        await service.preview_modify(66, ModifySpec(aux_price=1.5, trailing_percent=2.0))
    book.add(order_id=67, action="SELL", orderType="STP", lmtPrice=UNSET_DOUBLE, auxPrice=140.0)
    with pytest.raises(InvalidRequestError, match="STP orders need aux_price"):
        await service.preview_modify(67, ModifySpec(aux_price=0))


async def test_modify_is_refused_after_a_partial_fill(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    trade = book.add(order_id=68)
    preview = await service.preview_modify(68, ModifySpec(quantity=5))
    trade.orderStatus.filled = 3.0
    trade.orderStatus.remaining = 7.0
    with pytest.raises(InvalidRequestError, match="has filled 3 since the preview"):
        await service.submit(preview.token)
    fake_ib.placeOrder.assert_not_called()


async def test_modify_of_a_combo_qualifies_its_legs(
    make_gateway: MakeGateway, fake_ib: MagicMock, book: OrderBook
) -> None:
    long_call, short_call, _legs = _spread_legs()
    fake_ib.reqContractDetailsAsync.side_effect = details_for(long_call, short_call)
    service = (await make_gateway()).orders
    book.add(order_id=77, contract=_bag(700001, 700002), lmtPrice=1.0)
    preview = await service.preview_modify(77, ModifySpec(limit_price=1.1))
    assert preview.summary.startswith("MODIFY order 77")
    assert preview.orders[0].limit_price == 1.1

    capped = (await make_gateway(max_notional=100_000)).orders
    with pytest.raises(OrderLimitError, match="has no reference price"):
        await capped.preview_modify(77, ModifySpec(limit_price=1.1))

    book.add(order_id=78, contract=_bag(), lmtPrice=1.0)
    with pytest.raises(InvalidRequestError, match="legs this session does not know"):
        await service.preview_modify(78, ModifySpec(limit_price=1.1))


async def test_orders_without_an_account_are_refused(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    book.add(order_id=43, account="")
    with pytest.raises(AccountNotAllowedError, match="carries no account"):
        await service.cancel(43)
    with pytest.raises(AccountNotAllowedError, match="carries no account"):
        await service.preview_modify(43, ModifySpec(limit_price=1.0))
    fake_ib.cancelOrder.assert_not_called()


async def test_an_action_takes_one_rate_slot_per_order(
    make_gateway: MakeGateway, fake_ib: MagicMock, clock: FakeClock
) -> None:
    service = (await make_gateway(max_orders_per_minute=5)).orders
    bracket = BracketSpec(
        contract=AAPL,
        action="BUY",
        quantity=1,
        entry_price=150.0,
        take_profit_price=160.0,
        stop_loss_price=140.0,
    )
    await service.submit((await service.preview_bracket(bracket)).token)
    assert service.safety.rate_limiter.remaining() == 2
    second = await service.preview_bracket(bracket)
    with pytest.raises(RateLimitError, match="Room for the 3 orders"):
        await service.submit(second.token)
    assert fake_ib.placeOrder.call_count == 3

    clock.advance(61)  # the refused token is still valid: retry it after the wait
    result = await service.submit(second.token)
    assert result.accepted is True
    assert fake_ib.placeOrder.call_count == 6


async def test_an_oca_group_larger_than_the_rate_limit_is_refused(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    service = (await make_gateway(max_orders_per_minute=2)).orders
    preview = await service.preview_oca(OcaSpec(orders=[lmt(), lmt(price=149.0), lmt(price=148.0)]))
    with pytest.raises(RateLimitError, match="places 3 orders"):
        await service.submit(preview.token)
    fake_ib.placeOrder.assert_not_called()


async def test_a_rate_limited_token_is_not_burned(
    make_gateway: MakeGateway, fake_ib: MagicMock, clock: FakeClock
) -> None:
    service = (await make_gateway(max_orders_per_minute=1)).orders
    first = await service.preview_order(lmt())
    second = await service.preview_order(lmt())
    await service.submit(first.token)
    with pytest.raises(RateLimitError, match="retry after that"):
        await service.submit(second.token)
    clock.advance(61)
    assert (await service.submit(second.token)).accepted is True
    assert fake_ib.placeOrder.call_count == 2


async def test_precheck_refuses_without_side_effects(
    service: OrdersService, caplog: pytest.LogCaptureFixture
) -> None:
    preview = await service.preview_order(lmt())
    service.precheck(preview.token)  # fine: nothing taken
    assert service.safety.rate_limiter.remaining() == service.settings.max_orders_per_minute
    for _ in range(service.settings.breaker_rejects):
        service.safety.breaker.record_rejection("test")
    with pytest.raises(CircuitOpenError):
        service.precheck(preview.token)
    service.confirmation_request(preview.token)  # the token is still there
    [event] = events_named(caplog, "rejected")
    assert (event["stage"], event["code"]) == ("precheck", "circuit_open")


async def test_exercise_is_limited_by_what_it_delivers(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    """With only options allowed, exercising a call must not buy the stock."""
    fake_ib.reqContractDetailsAsync.side_effect = details_for(option())
    service = (await make_gateway(allowed_sec_types=["OPT"])).orders
    with pytest.raises(OrderLimitError, match="security type STK is not allowed"):
        await service.preview_exercise(
            ExerciseSpec(contract=OPTION_SPEC, action="exercise", quantity=1)
        )
    lapse = await service.preview_exercise(
        ExerciseSpec(contract=OPTION_SPEC, action="lapse", quantity=1)
    )
    assert lapse.kind == "exercise"

    both = (await make_gateway(allowed_sec_types=["OPT", "STK"], max_quantity=150)).orders
    with pytest.raises(OrderLimitError, match="quantity 200 exceeds the maximum of 150"):
        await both.preview_exercise(
            ExerciseSpec(contract=OPTION_SPEC, action="exercise", quantity=2)
        )


async def test_index_options_deliver_cash_not_the_index(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    spx = option("SPX", strike=6000.0, con_id=700100)
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [contract_details(spx, underSecType="IND")]
    )
    service = (await make_gateway(allowed_sec_types=["OPT"])).orders
    preview = await service.preview_exercise(
        ExerciseSpec(contract=ContractSpec(con_id=700100), action="exercise", quantity=1)
    )
    assert preview.summary.startswith("EXERCISE 1 SPX OPT")


async def test_exercise_by_con_id_alone(service: OrdersService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = details_for(option(), stock())
    preview = await service.preview_exercise(
        ExerciseSpec(contract=ContractSpec(con_id=700001), action="exercise", quantity=1)
    )
    assert preview.summary == "EXERCISE 1 AAPL OPT 20261218 200 C (x100)"
    with pytest.raises(InvalidRequestError, match=r"not STK \(AAPL STK\)"):
        await service.preview_exercise(
            ExerciseSpec(contract=ContractSpec(con_id=265598), action="exercise", quantity=1)
        )


async def test_live_cancel_needs_a_human(make_gateway: MakeGateway, fake_ib: MagicMock) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    service = (await make_gateway(allow_live=True)).orders
    book = OrderBook(fake_ib)
    book.add(
        order_id=50,
        account=LIVE_ACCOUNT,
        action="SELL",
        orderType="STP",
        lmtPrice=UNSET_DOUBLE,
        auxPrice=140.0,
        parentId=49,
    )
    request = await service.cancel_confirmation_request(50)
    assert request.is_paper is False
    assert request.account == LIVE_ACCOUNT
    assert request.action == "CANCEL order 50: SELL 10 AAPL STK STP stop 140.00 DAY"
    assert any("attached to order 49" in line for line in request.details)

    with pytest.raises(ConfirmationUnavailableError, match="Nothing was sent"):
        await service.cancel(50)
    fake_ib.cancelOrder.assert_not_called()
    result = await service.cancel(50, human_confirmed=True)
    assert result.status == "Cancelled"


async def test_bracket_prices_must_fit_the_tick_grid(
    service: OrdersService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [contract_details(marketRuleIds="26,26,26")]
    )
    fake_ib.reqMarketRuleAsync.side_effect = returns(
        [PriceIncrement(0.0, 0.0001), PriceIncrement(1.0, 0.01)]
    )
    with pytest.raises(InvalidRequestError, match="error 110") as caught:
        await service.preview_bracket(
            BracketSpec(
                contract=AAPL,
                action="BUY",
                quantity=1,
                entry_price=150.0,
                take_profit_price=160.005,
                stop_loss_price=140.0,
            )
        )
    assert (
        "Take profit limit price 160.005 is not a multiple of the price increment 0.01 at "
        "that price (e.g. 160.00 or 160.01)"
    ) in str(caught.value)
    fake_ib.whatIfOrderAsync.assert_not_called()

    penny = await service.preview_order(lmt(price=0.5001))  # below 1.00 the step is 0.0001
    assert penny.orders[0].limit_price == 0.5001
    with pytest.raises(InvalidRequestError, match=r"Order limit price 1\.0001"):
        await service.preview_order(lmt(price=1.0001))
    fake_ib.reqMarketRuleAsync.assert_called_once_with(26)  # cached for the session


async def test_modified_prices_must_fit_the_tick_grid(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [contract_details(marketRuleIds="26,26,26")]
    )
    fake_ib.reqMarketRuleAsync.side_effect = returns([PriceIncrement(0.0, 0.01)])
    book.add(order_id=69)
    with pytest.raises(InvalidRequestError, match=r"Modified order limit price 150\.005"):
        await service.preview_modify(69, ModifySpec(limit_price=150.005))
    assert (await service.preview_modify(69, ModifySpec(quantity=5))).kind == "modify"


async def test_a_rejected_bracket_exit_cancels_the_rest(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    """No entry may stay working without its take profit."""

    def reply(trade: Trade) -> None:
        if trade.order.orderId == 102:  # the take profit
            book.ib_error(trade, 110, "The price does not conform to the minimum price variation")
        else:
            book.submitted(trade)

    def cancel_family(trade: Trade) -> None:  # IBKR cancels the children with the parent
        for member in [trade, *(t for t in book.trades if t.order.parentId == trade.order.orderId)]:
            if not member.isDone():
                book.cancelled(member)

    book.reply = reply
    book.cancel_reply = cancel_family
    preview = await service.preview_bracket(
        BracketSpec(
            contract=AAPL,
            action="BUY",
            quantity=1,
            entry_price=150.0,
            take_profit_price=160.0,
            stop_loss_price=140.0,
        )
    )
    result = await service.submit(preview.token)
    entry = book.trades[0]
    fake_ib.cancelOrder.assert_called_once_with(entry.order)
    assert result.accepted is False
    assert [order.status for order in result.orders] == ["Cancelled"] * 3
    assert any(
        "the rest of the bracket (order 101, order 103) was cancelled" in m for m in result.messages
    )
    assert service.safety.breaker.consecutive_rejections == 1


async def test_a_rejected_exit_after_the_entry_filled_keeps_the_other_exit(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    def reply(trade: Trade) -> None:
        if trade.order.orderId == 101:
            book.set_status(trade, OrderStatus.Filled)
            trade.orderStatus.filled, trade.orderStatus.remaining = 1.0, 0.0
        elif trade.order.orderId == 102:
            book.ib_error(trade, 201, "Order rejected - reason: exit")
        else:
            book.submitted(trade)

    book.reply = reply
    preview = await service.preview_bracket(
        BracketSpec(
            contract=AAPL,
            action="BUY",
            quantity=1,
            entry_type="MKT",
            take_profit_price=160.0,
            stop_loss_price=140.0,
        )
    )
    result = await service.submit(preview.token)
    fake_ib.cancelOrder.assert_not_called()
    assert any("after the entry filled 1" in m and "order 103" in m for m in result.messages)


async def test_connection_loss_while_placing_names_what_was_placed(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook, caplog: pytest.LogCaptureFixture
) -> None:
    preview = await service.preview_oca(OcaSpec(orders=[lmt(), lmt(price=149.0)]))

    def flaky(contract: Contract, order: Order) -> Trade:
        if book.trades:
            raise ConnectionError("Not connected")
        return book.place_order(contract, order)

    fake_ib.placeOrder.side_effect = flaky
    fake_ib.cancelOrder.side_effect = ConnectionError("Not connected")
    with pytest.raises(NotConnectedError) as caught:
        await service.submit(preview.token)
    message = str(caught.value)
    assert "Could not cancel (no connection): order 101 (oca member, transmitted" in message
    assert "get_open_orders" in message
    ibkr = next(e for e in events_named(caplog, "rejected") if e["stage"] == "ibkr")
    assert ibkr["order_ids"] == [101]


async def test_order_status_finds_a_completed_order_by_its_remembered_perm_id(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    """After a reconnect ib_async's cache is empty and completed orders carry no order id."""
    placed = await service.submit((await service.preview_order(lmt())).token)
    assert placed.perm_id is not None
    book.trades.clear()
    completed = Trade(
        contract=stock(),
        order=Order(
            permId=placed.perm_id,
            account=PAPER_ACCOUNT,
            action="BUY",
            totalQuantity=10.0,
            orderType="LMT",
            lmtPrice=150.0,
        ),
        orderStatus=OrderStatus(status=OrderStatus.Filled),
        fills=[],
        log=[],
    )
    fake_ib.reqCompletedOrdersAsync.side_effect = returns([completed])
    status = await service.order_status(order_id=101)
    assert (status.order_id, status.perm_id, status.status) == (101, placed.perm_id, "Filled")
    assert status.placed_by_this_server is True


async def test_a_new_order_refused_with_321_is_not_cancelled_later(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    trade = book.add(order_id=79, status=OrderStatus.ValidationError)
    trade.order.permId = 0
    trade.log.append(TradeLogEntry(FIXED_TIME, OrderStatus.ValidationError, READ_ONLY_TEXT, 321))
    with pytest.raises(NotFoundError, match="no working orders"):
        await service.preview_cancel_all()
    with pytest.raises(InvalidRequestError, match="nothing to cancel"):
        await service.cancel(79)
    fake_ib.cancelOrder.assert_not_called()


async def test_cancel_all_reports_fills_and_errors(service: OrdersService, book: OrderBook) -> None:
    book.add(order_id=70)
    book.add(order_id=71)
    preview = await service.preview_cancel_all()

    def reply(trade: Trade) -> None:
        if trade.order.orderId == 70:
            book.set_status(trade, OrderStatus.Filled)
        else:
            book.cancelled(trade)

    book.cancel_reply = reply
    result = await service.submit(preview.token)
    assert result.accepted is True
    assert result.status == "PartlyCancelled"
    assert [order.status for order in result.orders] == ["Filled", "Cancelled"]
    assert "Order 70 filled before it could be cancelled." in result.messages

    book.add(order_id=72)
    preview = await service.preview_cancel_all()
    book.cancel_reply = lambda t: book.ib_error(
        t, 10147, "OrderId 72 that needs to be cancelled is not found."
    )
    result = await service.submit(preview.token)
    assert result.accepted is False
    assert any("10147" in m for m in result.messages)


async def test_cancel_all_with_every_order_gone(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    trade = book.add(order_id=73)
    preview = await service.preview_cancel_all()
    trade.orderStatus.status = OrderStatus.Filled
    result = await service.submit(preview.token)
    assert result.status == "NothingCancelled"
    fake_ib.cancelOrder.assert_not_called()


async def test_global_cancel_on_a_login_with_a_live_account_is_live(
    make_gateway: MakeGateway, fake_ib: MagicMock, book: OrderBook
) -> None:
    """The preview's paper account does not make a global cancel paper."""
    fake_ib.managedAccounts.return_value = [PAPER_ACCOUNT, LIVE_ACCOUNT]
    service = (
        await make_gateway(
            ib_account=PAPER_ACCOUNT,
            accounts_allowlist=[PAPER_ACCOUNT, LIVE_ACCOUNT],
            allow_live=True,
            allow_global_cancel=True,
        )
    ).orders
    trades = [book.add(order_id=84), book.add(order_id=85, account=LIVE_ACCOUNT)]
    preview = await service.preview_cancel_all(account=PAPER_ACCOUNT, scope="global")
    assert preview.is_paper is False
    request = service.confirmation_request(preview.token)
    assert (request.is_paper, request.account) == (False, LIVE_ACCOUNT)

    with pytest.raises(ConfirmationUnavailableError):
        await service.submit(preview.token)
    fake_ib.reqGlobalCancel.assert_not_called()

    def global_cancel() -> None:  # IBKR answers at once, so no status wait runs out
        for trade in trades:
            asyncio.get_running_loop().call_soon(book.cancelled, trade)

    fake_ib.reqGlobalCancel.side_effect = global_cancel
    fake_ib.reqCompletedOrdersAsync.side_effect = lambda *_: book.completed_orders()
    await service.submit(preview.token, human_confirmed=True)
    fake_ib.reqGlobalCancel.assert_called_once_with()


# --- review findings: confirmation text, stale caches, gates ---------------------------------


async def test_other_clients_orders_are_never_read_from_the_cache(
    service: OrdersService, fake_ib: MagicMock, book: OrderBook
) -> None:
    """ib_async never updates another client's cached order after a reqAllOpenOrders."""
    stale = book.add(order_id=0, client_id=7, perm_id=4444)  # cached as Submitted
    fake_ib.reqAllOpenOrdersAsync.side_effect = returns([])  # IBKR: no longer open
    filled = copy.deepcopy(stale)
    filled.orderStatus.status = OrderStatus.Filled
    fake_ib.reqCompletedOrdersAsync.side_effect = returns([filled])
    status = await service.order_status(perm_id=4444)
    assert status.status == "Filled"
    fake_ib.reqAllOpenOrdersAsync.assert_called_once()


async def test_a_global_cancel_preview_lists_only_what_ibkr_still_has(
    make_gateway: MakeGateway, fake_ib: MagicMock, book: OrderBook
) -> None:
    service = (await make_gateway(allow_global_cancel=True)).orders
    book.add(order_id=81)
    phantom = book.add(order_id=0, client_id=7, perm_id=4545)  # filled long ago, per IBKR
    fake_ib.reqAllOpenOrdersAsync.side_effect = returns([])
    preview = await service.preview_cancel_all(scope="global")
    assert [line.order_id for line in preview.orders] == [81]
    assert phantom.orderStatus.status == OrderStatus.Submitted  # still stale in the cache


async def test_a_global_cancel_reports_other_orders_from_fresh_answers(
    make_gateway: MakeGateway, fake_ib: MagicMock, book: OrderBook
) -> None:
    service = (await make_gateway(allow_global_cancel=True)).orders
    service.status_wait = 0.05
    other = book.add(order_id=0, client_id=7, perm_id=4646)
    preview = await service.preview_cancel_all(scope="global")
    result = await service.submit(preview.token)  # IBKR still lists it: not cancelled yet
    assert result.status == "PendingCancel"
    assert [out.perm_id for out in result.orders] == [4646]

    preview = await service.preview_cancel_all(scope="global")
    book.trades.remove(other)  # gone from IBKR's open orders, and not among completed
    result = await service.submit(preview.token)
    assert result.status == "NothingCancelled"
    assert any("perm_id 4646 is no longer open" in m for m in result.messages)


async def test_an_fa_token_is_refused_before_anything_is_used(
    service: OrdersService, fake_ib: MagicMock
) -> None:
    token = service.safety.previews.issue({"xml": "<x/>"}, PAPER_ACCOUNT, "replace_fa").token
    remaining = service.safety.rate_limiter.remaining()
    for attempt in (
        lambda: service.submit(token),
        lambda: asyncio.to_thread(service.precheck, token),
        lambda: asyncio.to_thread(service.confirmation_request, token),
    ):
        with pytest.raises(InvalidRequestError, match="apply_fa_config"):
            await attempt()
    assert service.safety.previews.peek(token).kind == "replace_fa"  # not consumed
    assert service.safety.rate_limiter.remaining() == remaining
    fake_ib.placeOrder.assert_not_called()


async def test_trading_gate_refusals_are_audited(
    make_gateway: MakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    service = (await make_gateway(profile="readonly")).orders
    with pytest.raises(ConfigurationError):
        await service.preview_order(lmt())
    with pytest.raises(ConfigurationError):
        await service.submit("any-token")
    with pytest.raises(ConfigurationError):
        await service.cancel(1)
    stages = [(e["stage"], e["code"]) for e in events_named(caplog, "rejected")]
    assert stages == [
        ("preview", "configuration_error"),
        ("submit", "configuration_error"),
        ("cancel", "configuration_error"),
    ]


async def test_previews_are_rate_limited(make_gateway: MakeGateway, fake_ib: MagicMock) -> None:
    service = (await make_gateway(max_previews_per_minute=2)).orders
    await service.preview_order(lmt())
    await service.preview_order(lmt())
    with pytest.raises(RateLimitError, match="Preview rate limit reached: at most 2 previews"):
        await service.preview_order(lmt())
    assert fake_ib.whatIfOrderAsync.call_count == 2


async def test_bonds_are_refused_under_a_notional_limit(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    bond = Contract(secType="BOND", conId=777, symbol="T 4 1/4 11/15/34", currency="USD")
    bond.exchange = "SMART"
    fake_ib.reqContractDetailsAsync.side_effect = details_for(bond)
    service = (await make_gateway(max_notional=10_000)).orders
    spec = lmt(100, 99.5, contract=ContractSpec(con_id=777))
    with pytest.raises(OrderLimitError, match="percent of face value"):
        await service.preview_order(spec)
    fake_ib.whatIfOrderAsync.assert_not_called()


async def test_the_currency_allowlist(make_gateway: MakeGateway, fake_ib: MagicMock) -> None:
    vod = stock("VOD", con_id=888)
    vod.currency = "GBP"
    fake_ib.reqContractDetailsAsync.side_effect = details_for(vod)
    service = (await make_gateway(max_notional=10_000, allowed_currencies=["USD"])).orders
    with pytest.raises(OrderLimitError, match="currency GBP is not allowed"):
        await service.preview_order(lmt(10, 100.0, contract=ContractSpec(con_id=888)))


async def test_a_delayed_reference_price_is_called_out(
    make_gateway: MakeGateway, fake_ib: MagicMock
) -> None:
    service = (await make_gateway(max_notional=5000)).orders
    delayed = ticker(stock(), last=100.0, ask=100.5)
    delayed.marketDataType = 3
    fake_ib.reqTickersAsync.side_effect = returns([delayed])
    preview = await service.preview_order(mkt(10))
    note = "the notional check used a delayed price 100.50 for AAPL STK, not a live price"
    assert any(note in warning for warning in preview.warnings)
    assert any(note in line for line in preview.details)  # shown to a confirming human

    close_only = ticker(stock(), close=99.0, last=math.nan, bid=math.nan, ask=math.nan)
    fake_ib.reqTickersAsync.side_effect = returns([close_only])
    preview = await service.preview_order(mkt(10))
    assert any("used a previous close 99.00 for AAPL" in warning for warning in preview.warnings)


@pytest.mark.parametrize(
    "fields",
    [
        {"model_code": "Growth\n(dry run only, no real money)"},
        {"model_code": "Growth; approve"},
        {"soft_dollar_tier": {"name": "Research" + chr(0x2028) + "dry run", "value": "R1"}},
        {"order_ref": "note\ttab"},
    ],
)
def test_model_supplied_text_must_stay_on_one_line(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        lmt(**fields)


async def test_model_supplied_text_is_quoted_for_the_human(
    service: OrdersService, fake_ib: MagicMock
) -> None:
    preview = await service.preview_order(
        lmt(
            model_code="dry run only - no real money",
            soft_dollar_tier=SoftDollarTierRef(name="IBKR sandbox", value="R1"),
        )
    )
    request = service.confirmation_request(preview.token)
    assert 'model "dry run only - no real money"' in request.action
    assert 'soft dollar tier "IBKR sandbox"' in request.action
