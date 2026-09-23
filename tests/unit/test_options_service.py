"""OptionsService against the autospecced fake IB: calculators and chain-slice quotes."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Iterable
from typing import Any
from unittest.mock import MagicMock

import pytest
from ib_async import Contract, ContractDetails, OptionChain, Ticker
from ib_async.client import Client
from ib_async.wrapper import RequestError

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    AmbiguousContractError,
    IbApiError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RequestTimeoutError,
    SubscriptionLimitError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.common import ContractSpec
from ib_gateway_mcp.services import options as options_module
from ib_gateway_mcp.services.options import (
    MAX_VOLATILITY,
    QUOTES_LIMIT_MAX,
    OptionsService,
    _select_strikes,
)
from tests.fakes import (
    contract_details,
    future,
    go_offline,
    option,
    option_computation,
    raises,
    returns,
    stock,
    ticker,
)

EXPIRY = "20261218"
STRIKES = [180.0, 190.0, 195.0, 200.0, 205.0, 210.0, 220.0]
AAPL_OPTION = ContractSpec(
    symbol="AAPL",
    sec_type="OPT",
    last_trade_date_or_contract_month=EXPIRY,
    strike=200,
    right="C",
)


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    """Short request timeout so hanging requests fail fast."""
    return settings_factory(request_timeout=0.2)


@pytest.fixture
def service(gateway: Gateway) -> OptionsService:
    return gateway.options


def chain(
    exchange: str = "SMART",
    trading_class: str = "AAPL",
    expirations: Iterable[str] = ("20261120", EXPIRY, "20270115"),
    strikes: Iterable[float] = STRIKES,
    multiplier: str = "100",
) -> OptionChain:
    """A reqSecDefOptParams row; the decoder leaves underlyingConId as a string."""
    return OptionChain(
        exchange,
        "265598",  # type: ignore[arg-type]
        trading_class,
        multiplier,
        list(expirations),
        list(strikes)[::-1],  # IBKR does not sort them
    )


def option_con_id(strike: float, right: str) -> int:
    return 700000 + int(strike * 100) * 2 + (right == "P")


class Market:
    """Drives ``fake_ib`` as an option market: an underlying, its chains, listed options and
    snapshot quotes. Requests are answered per contract, the way IBKR would."""

    def __init__(
        self,
        fake_ib: MagicMock,
        *,
        underlying: Contract | None = None,
        chains: list[OptionChain] | None = None,
    ) -> None:
        self.underlying = underlying or stock()
        self.chains = chains if chains is not None else [chain()]
        self.unlisted: set[tuple[float, str]] = set()
        self.ambiguous: set[tuple[float, str]] = set()
        self.quote_errors: dict[int, BaseException] = {}
        self.hanging: set[int] = set()
        self.underlying_quote: dict[str, Any] = {"bid": 201.0, "ask": 202.0, "last": 201.2}
        self.option_quote: dict[str, Any] = {}
        self.quoted: list[Contract] = []
        fake_ib.reqContractDetailsAsync.side_effect = self.details
        fake_ib.reqSecDefOptParamsAsync.side_effect = returns(self.chains)
        fake_ib.reqTickersAsync.side_effect = self.tickers

    async def details(self, contract: Contract) -> list[ContractDetails]:
        if contract.secType not in {"OPT", "FOP"}:
            return [contract_details(self.underlying)]
        key = (contract.strike, contract.right)
        if key in self.unlisted:
            raise RequestError(1, 200, "No security definition has been found for the request")
        rows = [contract_details(self._option(contract, option_con_id(*key)))]
        if key in self.ambiguous:
            rows.append(contract_details(self._option(contract, option_con_id(*key) + 1)))
        return rows

    @staticmethod
    def _option(request: Contract, con_id: int) -> Contract:
        return Contract(
            secType=request.secType,
            conId=con_id,
            symbol=request.symbol,
            lastTradeDateOrContractMonth=request.lastTradeDateOrContractMonth,
            strike=request.strike,
            right=request.right,
            multiplier=request.multiplier,
            exchange=request.exchange,
            currency=request.currency,
            tradingClass=request.tradingClass,
        )

    async def tickers(self, *contracts: Contract, regulatorySnapshot: bool = False) -> list[Ticker]:
        result: list[Ticker] = []
        for contract in contracts:
            self.quoted.append(contract)
            if contract.conId in self.quote_errors:
                raise self.quote_errors[contract.conId]
            if contract.conId in self.hanging:
                await asyncio.get_running_loop().create_future()
            if contract.secType in {"OPT", "FOP"}:
                fields: dict[str, Any] = {
                    "bid": 4.9,
                    "ask": 5.1,
                    "last": 5.0,
                    "modelGreeks": option_computation(undPrice=201.5),
                    **self.option_quote,
                }
            else:
                fields = {"last": math.nan, "close": 199.0, **self.underlying_quote}
            result.append(ticker(contract, **fields))
        return result

    def option_calls(self) -> list[tuple[float, str]]:
        return [(c.strike, c.right) for c in self.quoted if c.secType in {"OPT", "FOP"}]


@pytest.fixture
def market(fake_ib: MagicMock) -> Market:
    return Market(fake_ib)


def leg_keys(result: Any) -> list[tuple[float | None, str | None]]:
    return [(leg.contract.strike, leg.contract.right) for leg in result.legs]


# --- calculator plumbing ----------------------------------------------------------------------

CALC_REQ_ID = 42


def answer_calculations(fake_ib: MagicMock, answer: object | None) -> None:
    """Answer option calculations the way ib_async's wrapper does.

    The service sends the request with ``client.send`` and awaits the future
    ``wrapper.startReq`` returned: ``tickOptionComputation`` resolves it with ``answer``,
    a request error fails it (``answer`` is an exception), and None never answers.
    """
    fake_ib.client.getReqId.return_value = CALC_REQ_ID
    futures: list[asyncio.Future[Any]] = []

    def start_req(_key: object, _contract: object = None, _container: object = None) -> Any:
        futures.append(asyncio.get_running_loop().create_future())
        return futures[-1]

    def send(*_fields: object, makeEmpty: bool = True) -> None:
        if isinstance(answer, BaseException):
            futures[-1].set_exception(answer)
        elif answer is not None:
            futures[-1].set_result(answer)

    fake_ib.wrapper.startReq.side_effect = start_req
    fake_ib.client.send.side_effect = send


def on_the_wire(*fields: object) -> list[str]:
    """The fields ib_async's ``Client.send`` encodes ``fields`` into, as the gateway reads them."""
    client = MagicMock(spec=Client)
    client.isConnected.return_value = True
    Client.send(client, *fields)
    message: str = client.sendMsg.call_args.args[0]
    return message.split("\0")[:-1]


# The official client's layout (ibapi 10.45 EClient.calculateImpliedVolatility and
# calculateOptionPrice): message id, version 3, request id, the 12 contract fields, the two
# inputs, then ONE misc-options string ("tag=value;" pairs, empty here) with no count.
AAPL_OPTION_FIELDS = [
    "700001", "AAPL", "OPT", "20261218", "200.0", "C", "100", "SMART", "", "USD", "", "",
]  # fmt: skip


Calculator = tuple[Callable[..., Any], int, dict[str, float], str]
"""A calculator case: service method, TWS message id, inputs, ``ib.client`` cancel method."""


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            (
                OptionsService.implied_volatility,
                54,
                {"option_price": 12.5, "underlying_price": 200.25},
                "cancelCalculateImpliedVolatility",
            ),
            id="implied_volatility",
        ),
        pytest.param(
            (
                OptionsService.option_price,
                55,
                {"volatility": 0.25, "underlying_price": 200.25},
                "cancelCalculateOptionPrice",
            ),
            id="option_price",
        ),
    ],
)
async def test_calculators_send_the_official_wire_layout(
    service: OptionsService, fake_ib: MagicMock, case: Calculator
) -> None:
    calculate, message, inputs, cancel = case
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    answer_calculations(fake_ib, option_computation())
    await calculate(service, AAPL_OPTION, **inputs)
    value, underlying = inputs.values()
    fake_ib.client.send.assert_called_once_with(
        message, 3, CALC_REQ_ID, option(), value, underlying, ""
    )
    assert on_the_wire(*fake_ib.client.send.call_args.args) == [
        str(message), "3", str(CALC_REQ_ID), *AAPL_OPTION_FIELDS, str(value), "200.25", "",
    ]  # fmt: skip
    fake_ib.wrapper.startReq.assert_called_once_with(CALC_REQ_ID, option())
    getattr(fake_ib.client, cancel).assert_called_once_with(CALC_REQ_ID)
    # Not ib_async's calculate*Async: they put the gateway-rejected tag count on the wire.
    fake_ib.calculateImpliedVolatilityAsync.assert_not_called()
    fake_ib.calculateOptionPriceAsync.assert_not_called()


def test_ib_async_calculators_still_send_a_tag_count() -> None:
    """Why the service bypasses ib_async: its encoding adds a count before the options
    string, which IB Gateway 10.45 reads as the options (error 320, "Please use
    'Key=Value' format for Misc Options"). Once this fails, ib_async fixed it."""
    client = MagicMock(spec=Client)
    Client.calculateImpliedVolatility(client, CALC_REQ_ID, option(), 12.5, 200.25, [])
    Client.calculateOptionPrice(client, CALC_REQ_ID, option(), 0.25, 200.25, [])
    assert [sent.args[-2:] for sent in client.send.call_args_list] == [(0, []), (0, [])]


# --- implied volatility -----------------------------------------------------------------------


async def test_implied_volatility(service: OptionsService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    answer_calculations(fake_ib, option_computation())
    result = await service.implied_volatility(AAPL_OPTION, option_price=12.5, underlying_price=200)
    sent = fake_ib.client.send.call_args
    assert sent.args[3].conId == 700001
    assert sent.args[4:] == (12.5, 200, "")
    assert result.implied_vol == 0.25
    assert result.greeks.delta == 0.52
    assert result.greeks.theta == -0.06
    assert result.contract.con_id == 700001
    assert (result.option_price, result.underlying_price) == (12.5, 200)


async def test_implied_volatility_by_con_id(service: OptionsService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    answer_calculations(fake_ib, option_computation())
    result = await service.implied_volatility(
        ContractSpec(con_id=700001), option_price=12.5, underlying_price=200
    )
    assert result.contract.sec_type == "OPT"
    # Only the id is sent: the STK default must not contradict it.
    assert fake_ib.reqContractDetailsAsync.call_args.args[0].secType == ""


async def test_calculators_refuse_a_con_id_that_is_not_an_option(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    with pytest.raises(InvalidRequestError, match="not an option"):
        await service.implied_volatility(
            ContractSpec(con_id=265598), option_price=1, underlying_price=200
        )
    fake_ib.client.send.assert_not_called()


async def test_calculators_refuse_a_non_option_spec_before_asking(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match="OPT or FOP"):
        await service.option_price(ContractSpec(symbol="AAPL"), volatility=0.2, underlying_price=1)
    fake_ib.reqContractDetailsAsync.assert_not_called()


@pytest.mark.parametrize(
    ("option_price", "underlying_price"),
    [(0, 200), (-1, 200), (5, 0), (math.nan, 200), (5, math.inf)],
)
async def test_implied_volatility_rejects_bad_prices(
    service: OptionsService, fake_ib: MagicMock, option_price: float, underlying_price: float
) -> None:
    with pytest.raises(InvalidRequestError, match="positive"):
        await service.implied_volatility(
            AAPL_OPTION, option_price=option_price, underlying_price=underlying_price
        )
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_calculation_that_never_answers_times_out(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    answer_calculations(fake_ib, None)
    with pytest.raises(RequestTimeoutError, match="theoretical price") as caught:
        await service.option_price(AAPL_OPTION, volatility=0.25, underlying_price=200)
    assert "market data permissions" in str(caught.value)
    # Cancelled at IBKR and forgotten by ib_async's wrapper.
    fake_ib.client.cancelCalculateOptionPrice.assert_called_once_with(CALC_REQ_ID)
    fake_ib.wrapper._endReq.assert_called_once_with(CALC_REQ_ID)


async def test_calculation_waits_at_most_calculation_timeout(
    settings_factory: Callable[..., Settings],
    fake_ib: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(options_module, "CALCULATION_TIMEOUT", 0.05)
    gateway = Gateway(settings_factory(request_timeout=30), ib_factory=lambda: fake_ib)
    await gateway.start()
    try:
        fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
        answer_calculations(fake_ib, None)
        with pytest.raises(RequestTimeoutError, match=r"after 0\.05s"):
            await gateway.options.implied_volatility(
                AAPL_OPTION, option_price=12.5, underlying_price=200
            )
    finally:
        await gateway.stop()


async def test_calculation_error_maps_to_ib_api_error(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    answer_calculations(
        fake_ib,
        RequestError(9, 10089, "Requested market data requires additional subscription for API"),
    )
    with pytest.raises(IbApiError) as caught:
        await service.implied_volatility(AAPL_OPTION, option_price=12.5, underlying_price=200)
    assert caught.value.error_code == 10089
    assert "OPRA" in str(caught.value)  # the permission hint is added


async def test_calculation_error_without_a_hint_is_unchanged(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    answer_calculations(fake_ib, RequestError(9, 322, "Server error"))
    with pytest.raises(IbApiError) as caught:
        await service.option_price(AAPL_OPTION, volatility=0.25, underlying_price=200)
    assert (caught.value.error_code, caught.value.error_message) == (322, "Server error")


async def test_calculators_report_unknown_and_ambiguous_options(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(RequestError(1, 200, "No security"))
    with pytest.raises(NotFoundError, match="get_option_chain"):
        await service.option_price(AAPL_OPTION, volatility=0.25, underlying_price=200)
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [contract_details(option()), contract_details(option(con_id=700002, tradingClass="X"))]
    )
    with pytest.raises(AmbiguousContractError):
        await service.option_price(AAPL_OPTION, volatility=0.25, underlying_price=200)
    fake_ib.client.send.assert_not_called()


async def test_implied_volatility_not_computed(service: OptionsService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    # ib_async turns IBKR's -1 "not computed" into None, and passes vega/theta -2 through.
    answer_calculations(
        fake_ib, option_computation(impliedVol=None, delta=None, vega=-2.0, theta=-2.0)
    )
    with pytest.raises(InvalidRequestError, match="intrinsic"):
        await service.implied_volatility(AAPL_OPTION, option_price=0.01, underlying_price=200)


async def test_calculation_with_no_values_at_all(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    answer_calculations(
        fake_ib,
        option_computation(
            impliedVol=math.nan,
            delta=None,
            optPrice=None,
            pvDividend=None,
            gamma=math.nan,
            vega=-2.0,
            theta=-2.0,
            undPrice=None,
        ),
    )
    with pytest.raises(InvalidRequestError, match="no values"):
        await service.option_price(AAPL_OPTION, volatility=0.25, underlying_price=200)


async def test_calculation_resolved_without_a_computation(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    # With RaiseRequestErrors off, ib_async ends a failed request with its empty results.
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    answer_calculations(fake_ib, [])
    with pytest.raises(InvalidRequestError, match="no values"):
        await service.implied_volatility(AAPL_OPTION, option_price=12.5, underlying_price=200)


# --- option price -----------------------------------------------------------------------------


async def test_option_price(service: OptionsService, fake_ib: MagicMock) -> None:
    fop = Contract(
        secType="FOP",
        conId=900001,
        symbol="ES",
        lastTradeDateOrContractMonth="20261218",
        strike=6000,
        right="P",
        multiplier="50",
        exchange="CME",
        currency="USD",
        tradingClass="ES",
    )
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(fop)])
    answer_calculations(
        fake_ib, option_computation(optPrice=101.25, gamma=math.nan, vega=-2.0, delta=-0.4)
    )
    spec = ContractSpec(
        symbol="ES",
        sec_type="FOP",
        exchange="CME",
        last_trade_date_or_contract_month="20261218",
        strike=6000,
        right="P",
    )
    result = await service.option_price(spec, volatility=0.18, underlying_price=6050)
    assert fake_ib.client.send.call_args.args[4:] == (0.18, 6050, "")
    assert result.option_price == 101.25
    assert result.volatility == 0.18
    assert result.contract.sec_type == "FOP"
    assert result.greeks.delta == -0.4
    assert result.greeks.gamma is None  # NaN
    assert result.greeks.vega is None  # IBKR's "not computed" marker


async def test_option_price_rejects_percent_volatility(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match="decimal"):
        await service.option_price(
            AAPL_OPTION, volatility=MAX_VOLATILITY + 15, underlying_price=200
        )
    with pytest.raises(InvalidRequestError, match="positive"):
        await service.option_price(AAPL_OPTION, volatility=0, underlying_price=200)
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_option_price_not_computed(service: OptionsService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    answer_calculations(fake_ib, option_computation(optPrice=None))
    with pytest.raises(InvalidRequestError, match="no price"):
        await service.option_price(AAPL_OPTION, volatility=0.25, underlying_price=200)


# --- option quotes: strike selection ----------------------------------------------------------


async def test_quotes_around_the_money(
    service: OptionsService, fake_ib: MagicMock, market: Market
) -> None:
    result = await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY)
    fake_ib.reqSecDefOptParamsAsync.assert_called_once_with("AAPL", "", "STK", 265598)
    # Underlying mid 201.5: the five nearest strikes, both rights, sorted.
    assert leg_keys(result) == [
        (strike, right) for strike in (190.0, 195.0, 200.0, 205.0, 210.0) for right in "CP"
    ]
    assert result.underlying_price == 201.5
    assert result.underlying_quote is not None
    assert result.underlying_quote.bid == 201.0
    assert result.underlying.con_id == 265598
    assert (result.expiration, result.exchange, result.trading_class) == (EXPIRY, "SMART", "AAPL")
    assert result.multiplier == "100"
    assert result.market_data_type == "live"
    assert (result.total, result.truncated, result.skipped) == (10, False, [])
    leg = result.legs[0]
    assert leg.contract.con_id == option_con_id(190.0, "C")
    assert leg.contract.sec_type == "OPT"
    assert leg.contract.last_trade_date_or_contract_month == EXPIRY
    assert (leg.bid, leg.ask, leg.last) == (4.9, 5.1, 5.0)
    assert leg.greeks is not None
    assert leg.greeks.delta == 0.52
    assert leg.greeks.und_price == 201.5
    # Legs are qualified with the chain's class, multiplier and exchange.
    sent = [
        call.args[0]
        for call in fake_ib.reqContractDetailsAsync.call_args_list
        if call.args[0].secType == "OPT"
    ]
    assert {(c.tradingClass, c.multiplier, c.exchange, c.currency) for c in sent} == {
        ("AAPL", "100", "SMART", "USD")
    }


async def test_quotes_around_the_money_falls_back_to_last_then_close(
    service: OptionsService, market: Market
) -> None:
    market.underlying_quote = {"bid": -1.0, "bidSize": 0.0, "ask": math.nan, "last": 207.0}
    result = await service.option_quotes(
        ContractSpec(symbol="AAPL"), EXPIRY, strikes_around_atm=1, right="C"
    )
    assert result.underlying_price == 207.0
    assert leg_keys(result) == [(205.0, "C")]
    market.underlying_quote = {"bid": math.nan, "ask": math.nan, "last": math.nan}
    result = await service.option_quotes(
        ContractSpec(symbol="AAPL"), EXPIRY, strikes_around_atm=1, right="C"
    )
    assert result.underlying_price == 199.0  # the close
    assert leg_keys(result) == [(200.0, "C")]


async def test_quotes_without_an_underlying_price(service: OptionsService, market: Market) -> None:
    market.underlying_quote = {"bid": math.nan, "ask": math.nan, "last": math.nan, "close": -1.0}
    with pytest.raises(NotFoundError, match="strike_min and strike_max"):
        await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY)
    assert market.option_calls() == []


async def test_quotes_for_a_strike_range(service: OptionsService, market: Market) -> None:
    result = await service.option_quotes(
        ContractSpec(symbol="AAPL"), EXPIRY, right="P", strike_min=195, strike_max=205
    )
    assert leg_keys(result) == [(195.0, "P"), (200.0, "P"), (205.0, "P")]
    # No underlying snapshot is needed for a range.
    assert all(c.secType == "OPT" for c in market.quoted)
    assert result.underlying_quote is None
    assert result.underlying_price is None


async def test_quotes_for_an_open_ended_range(service: OptionsService, market: Market) -> None:
    result = await service.option_quotes(
        ContractSpec(symbol="AAPL"), EXPIRY, right="C", strike_min=210
    )
    assert leg_keys(result) == [(210.0, "C"), (220.0, "C")]
    result = await service.option_quotes(
        ContractSpec(symbol="AAPL"), EXPIRY, right="C", strike_max=185
    )
    assert leg_keys(result) == [(180.0, "C")]


async def test_quotes_for_an_empty_range(service: OptionsService, market: Market) -> None:
    with pytest.raises(NotFoundError, match="run from 180 to 220"):
        await service.option_quotes(
            ContractSpec(symbol="AAPL"), EXPIRY, strike_min=300, strike_max=400
        )


async def test_range_truncation_keeps_the_lowest_strikes(
    service: OptionsService, market: Market
) -> None:
    result = await service.option_quotes(
        ContractSpec(symbol="AAPL"), EXPIRY, strike_min=190, strike_max=210, limit=4
    )
    assert leg_keys(result) == [(190.0, "C"), (190.0, "P"), (195.0, "C"), (195.0, "P")]
    assert (result.total, result.truncated) == (10, True)


async def test_atm_truncation_keeps_the_nearest_strikes(
    service: OptionsService, market: Market
) -> None:
    result = await service.option_quotes(
        ContractSpec(symbol="AAPL"), EXPIRY, strikes_around_atm=5, limit=3
    )
    # 200 is nearest to 201.5, then 205.
    assert leg_keys(result) == [(200.0, "C"), (200.0, "P"), (205.0, "C")]
    assert (result.total, result.truncated) == (10, True)


async def test_quote_limit_is_capped(service: OptionsService, fake_ib: MagicMock) -> None:
    strikes = [100.0 + 5 * i for i in range(30)]
    market = Market(fake_ib, chains=[chain(strikes=strikes)])
    result = await service.option_quotes(
        ContractSpec(symbol="AAPL"), EXPIRY, strike_min=1, limit=1000
    )
    assert len(result.legs) == QUOTES_LIMIT_MAX
    assert (result.total, result.truncated) == (60, True)
    assert len(market.option_calls()) == QUOTES_LIMIT_MAX


async def test_quotes_default_limit(service: OptionsService, fake_ib: MagicMock) -> None:
    Market(fake_ib, chains=[chain(strikes=[100.0 + 5 * i for i in range(30)])])
    result = await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY, strike_min=1)
    assert len(result.legs) == 20
    assert result.truncated is True


# --- option quotes: chain selection -----------------------------------------------------------


async def test_quotes_prefer_the_smart_chain(service: OptionsService, fake_ib: MagicMock) -> None:
    Market(fake_ib, chains=[chain("CBOE", strikes=[200.0]), chain("SMART")])
    result = await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY, strike_min=220)
    assert result.exchange == "SMART"


async def test_quotes_on_a_named_exchange(service: OptionsService, fake_ib: MagicMock) -> None:
    market = Market(fake_ib, chains=[chain("CBOE", strikes=[200.0]), chain("SMART")])
    result = await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY, exchange=" cboe ")
    assert result.exchange == "CBOE"
    assert {c.exchange for c in market.quoted if c.secType == "OPT"} == {"CBOE"}
    with pytest.raises(NotFoundError, match="listed on CBOE, SMART"):
        await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY, exchange="ISE")


async def test_quotes_need_an_exchange_when_smart_is_not_listed(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    Market(fake_ib, chains=[chain("CBOE"), chain("ISE")])
    with pytest.raises(InvalidRequestError, match="pass exchange"):
        await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY)


async def test_futures_options_use_the_future_and_its_exchange(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    es = future()
    market = Market(
        fake_ib,
        underlying=es,
        chains=[chain("CME", "ES", strikes=[5900.0, 6000.0, 6100.0], multiplier="50")],
    )
    result = await service.option_quotes(
        ContractSpec(
            symbol="ES", sec_type="FUT", exchange="CME", last_trade_date_or_contract_month="202612"
        ),
        EXPIRY,
        strike_min=6000,
        strike_max=6000,
    )
    fake_ib.reqSecDefOptParamsAsync.assert_called_once_with("ES", "CME", "FUT", 800001)
    assert result.exchange == "CME"
    assert result.multiplier == "50"
    assert [c.secType for c in market.quoted] == ["FOP", "FOP"]
    assert leg_keys(result) == [(6000.0, "C"), (6000.0, "P")]


async def test_futures_options_on_a_continuous_future(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    """A CONTFUT stands for its front month: FUT and its exchange go out, FOP legs come back."""
    es = future()
    es.secType = "CONTFUT"
    es.lastTradeDateOrContractMonth = ""
    market = Market(
        fake_ib,
        underlying=es,
        chains=[chain("CME", "ES", strikes=[5900.0, 6000.0, 6100.0], multiplier="50")],
    )
    result = await service.option_quotes(
        ContractSpec(symbol="ES", sec_type="CONTFUT", exchange="CME"),
        EXPIRY,
        strike_min=6000,
        strike_max=6000,
    )
    fake_ib.reqSecDefOptParamsAsync.assert_called_once_with("ES", "CME", "FUT", 800001)
    assert [c.secType for c in market.quoted] == ["FOP", "FOP"]
    assert leg_keys(result) == [(6000.0, "C"), (6000.0, "P")]


async def test_quotes_pick_the_class_named_like_the_underlying(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    spx = Contract(secType="IND", conId=416904, symbol="SPX", exchange="CBOE", currency="USD")
    chains = [chain(trading_class="SPXW"), chain(trading_class="SPX")]
    Market(fake_ib, underlying=spx, chains=chains)
    underlying = ContractSpec(symbol="SPX", sec_type="IND", exchange="CBOE")
    result = await service.option_quotes(underlying, EXPIRY, strike_min=200, strike_max=200)
    assert result.trading_class == "SPX"
    result = await service.option_quotes(
        underlying, EXPIRY, strike_min=200, strike_max=200, trading_class="spxw"
    )
    assert result.trading_class == "SPXW"
    assert {leg.contract.trading_class for leg in result.legs} == {"SPXW"}
    with pytest.raises(NotFoundError, match="classes listed: SPX, SPXW"):
        await service.option_quotes(underlying, EXPIRY, trading_class="XSP")


async def test_quotes_with_several_classes_and_no_preference(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    Market(fake_ib, chains=[chain(trading_class="AAPL1"), chain(trading_class="AAPL2")])
    with pytest.raises(InvalidRequestError, match="AAPL1, AAPL2"):
        await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY)


async def test_quotes_class_listing_only_other_expirations_is_ignored(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    Market(
        fake_ib,
        chains=[
            chain(trading_class="AAPL"),
            chain(trading_class="AAPL7", expirations=["20270115"]),
        ],
    )
    result = await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY, strike_min=220)
    assert result.trading_class == "AAPL"


async def test_quotes_for_an_unlisted_expiration(service: OptionsService, market: Market) -> None:
    with pytest.raises(NotFoundError, match="nearby: 20261120, 20261218, 20270115"):
        await service.option_quotes(ContractSpec(symbol="AAPL"), "20261204")


async def test_quotes_accept_dashed_dates(service: OptionsService, market: Market) -> None:
    result = await service.option_quotes(ContractSpec(symbol="AAPL"), "2026-12-18", strike_min=220)
    assert result.expiration == EXPIRY


@pytest.mark.parametrize("expiration", ["202612", "Dec 18", "2026121", ""])
async def test_quotes_reject_malformed_expirations(
    service: OptionsService, fake_ib: MagicMock, expiration: str
) -> None:
    with pytest.raises(InvalidRequestError, match="YYYYMMDD"):
        await service.option_quotes(ContractSpec(symbol="AAPL"), expiration)
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_quotes_for_an_underlying_without_options(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    Market(fake_ib, chains=[])
    with pytest.raises(NotFoundError, match="lists no options"):
        await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"strike_min": 210, "strike_max": 200}, "above strike_max"),
        ({"strike_min": 200, "strikes_around_atm": 3}, "not both"),
        ({"strikes_around_atm": 0}, "at least 1"),
        ({"strike_max": -5}, "positive"),
    ],
)
async def test_quotes_reject_bad_strike_arguments(
    service: OptionsService, fake_ib: MagicMock, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY, **kwargs)
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_quotes_refuse_an_option_as_underlying(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match="not OPT"):
        await service.option_quotes(AAPL_OPTION, EXPIRY)
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_quotes_refuse_an_option_given_by_con_id_as_underlying(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    with pytest.raises(InvalidRequestError, match=r"option or combo \(OPT\)"):
        await service.option_quotes(ContractSpec(con_id=700001), EXPIRY)
    fake_ib.reqSecDefOptParamsAsync.assert_not_called()


async def test_quotes_for_an_unknown_underlying(
    service: OptionsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(RequestError(1, 200, "No security"))
    with pytest.raises(NotFoundError):
        await service.option_quotes(ContractSpec(symbol="NOPE"), EXPIRY)
    fake_ib.reqSecDefOptParamsAsync.assert_not_called()


# --- option quotes: missing strikes and market data failures ----------------------------------


async def test_quotes_skip_strikes_not_listed_for_the_expiration(
    service: OptionsService, market: Market
) -> None:
    market.unlisted = {(205.0, "C"), (205.0, "P")}
    market.ambiguous = {(195.0, "P")}
    result = await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY, strikes_around_atm=3)
    assert leg_keys(result) == [(195.0, "C"), (200.0, "C"), (200.0, "P")]
    assert [(s.strike, s.right) for s in result.skipped] == [
        (195.0, "P"),
        (205.0, "C"),
        (205.0, "P"),
    ]
    assert "ambiguous: 2 contracts" in result.skipped[0].reason
    ambiguous_ids = (option_con_id(195.0, "P"), option_con_id(195.0, "P") + 1)
    assert f"con_id {ambiguous_ids[0]}, {ambiguous_ids[1]}" in result.skipped[0].reason
    assert result.skipped[1].reason == "not listed for this expiration"
    assert result.skipped[1].con_id is None
    assert result.total == 6


async def test_quotes_when_no_selected_strike_is_listed(
    service: OptionsService, market: Market
) -> None:
    market.unlisted = {(200.0, "C"), (205.0, "C")}
    with pytest.raises(NotFoundError, match=r"\(200, 205\) is listed"):
        await service.option_quotes(
            ContractSpec(symbol="AAPL"), EXPIRY, right="C", strike_min=200, strike_max=205
        )
    assert market.option_calls() == []


async def test_quotes_keep_the_legs_that_could_be_quoted(
    service: OptionsService, market: Market
) -> None:
    market.quote_errors[option_con_id(200.0, "P")] = RequestError(
        5, 10090, "Part of requested market data is not subscribed."
    )
    market.hanging.add(option_con_id(205.0, "C"))
    result = await service.option_quotes(
        ContractSpec(symbol="AAPL"), EXPIRY, strike_min=200, strike_max=205
    )
    assert leg_keys(result) == [(200.0, "C"), (205.0, "P")]
    skipped = {(s.strike, s.right): s for s in result.skipped}
    assert skipped[(200.0, "P")].con_id == option_con_id(200.0, "P")
    assert "IB error 10090" in skipped[(200.0, "P")].reason
    assert "OPRA" in skipped[(200.0, "P")].reason
    assert "Timed out" in skipped[(205.0, "C")].reason


async def test_quotes_explain_missing_option_data_permissions(
    service: OptionsService, market: Market
) -> None:
    for strike in (200.0, 205.0):
        market.quote_errors[option_con_id(strike, "C")] = RequestError(
            5, 354, "Requested market data is not subscribed."
        )
    with pytest.raises(IbApiError, match="OPRA") as caught:
        await service.option_quotes(
            ContractSpec(symbol="AAPL"), EXPIRY, right="C", strike_min=200, strike_max=205
        )
    assert caught.value.error_code == 354
    assert "set_market_data_type" in str(caught.value)


async def test_quotes_explain_a_competing_session(service: OptionsService, market: Market) -> None:
    market.quote_errors[option_con_id(200.0, "C")] = RequestError(
        5, 10197, "No market data during competing live session"
    )
    with pytest.raises(IbApiError, match="Another session"):
        await service.option_quotes(
            ContractSpec(symbol="AAPL"), EXPIRY, right="C", strike_min=200, strike_max=200
        )


async def test_quotes_raise_other_errors_unchanged_when_nothing_was_quoted(
    service: OptionsService, market: Market
) -> None:
    market.quote_errors[option_con_id(200.0, "C")] = RequestError(5, 322, "Duplicate ticker id")
    with pytest.raises(IbApiError) as caught:
        await service.option_quotes(
            ContractSpec(symbol="AAPL"), EXPIRY, right="C", strike_min=200, strike_max=200
        )
    assert caught.value.error_message == "Duplicate ticker id"


async def test_quotes_when_market_data_lines_run_out(
    service: OptionsService, market: Market
) -> None:
    market.quote_errors[option_con_id(200.0, "C")] = RequestError(5, 101, "Max tickers reached")
    with pytest.raises(SubscriptionLimitError, match="error 101"):
        await service.option_quotes(
            ContractSpec(symbol="AAPL"), EXPIRY, right="C", strike_min=200, strike_max=200
        )


async def test_quotes_report_line_limit_on_single_legs(
    service: OptionsService, market: Market
) -> None:
    market.quote_errors[option_con_id(205.0, "C")] = RequestError(5, 101, "Max tickers reached")
    result = await service.option_quotes(
        ContractSpec(symbol="AAPL"), EXPIRY, right="C", strike_min=200, strike_max=205
    )
    assert leg_keys(result) == [(200.0, "C")]
    assert "list_subscriptions" in result.skipped[0].reason


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (RequestError(3, 10090, "Part of requested market data is not subscribed"), IbApiError),
        (None, RequestTimeoutError),
    ],
)
async def test_underlying_failures_say_how_to_avoid_the_snapshot(
    service: OptionsService,
    market: Market,
    failure: BaseException | None,
    expected: type[Exception],
) -> None:
    if failure is None:
        market.hanging.add(265598)
    else:
        market.quote_errors[265598] = failure
    with pytest.raises(expected, match="pass strike_min/strike_max"):
        await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY)
    assert market.option_calls() == []


async def test_quotes_when_the_underlying_cannot_be_quoted(
    service: OptionsService, fake_ib: MagicMock, market: Market
) -> None:
    market.quote_errors[265598] = RequestError(3, 10089, "Requires additional subscription")
    with pytest.raises(IbApiError, match="strike_min/strike_max to skip it") as caught:
        await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY)
    assert caught.value.error_code == 10089
    assert "OPRA" in str(caught.value)
    assert market.option_calls() == []


async def test_quotes_convert_nan_and_missing_greeks(
    service: OptionsService, market: Market
) -> None:
    market.option_quote = {
        "bid": -1.0,
        "bidSize": 0.0,
        "ask": math.nan,
        "last": math.nan,
        "volume": math.nan,
        "modelGreeks": None,
    }
    result = await service.option_quotes(
        ContractSpec(symbol="AAPL"), EXPIRY, right="C", strike_min=220
    )
    leg = result.legs[0]
    assert (leg.bid, leg.bid_size, leg.ask, leg.last, leg.volume) == (None, 0.0, None, None, None)
    assert leg.greeks is None
    result.model_dump_json()  # no NaN reaches JSON


async def test_quotes_report_the_connection_market_data_type(
    service: OptionsService, gateway: Gateway, market: Market
) -> None:
    gateway.connection.set_market_data_type(3)
    result = await service.option_quotes(
        ContractSpec(symbol="AAPL"), EXPIRY, right="C", strike_min=220
    )
    assert result.market_data_type == "delayed"


async def test_quotes_when_the_gateway_is_down(
    service: OptionsService, fake_ib: MagicMock, market: Market
) -> None:
    go_offline(fake_ib)
    with pytest.raises(NotConnectedError):
        await service.option_quotes(ContractSpec(symbol="AAPL"), EXPIRY)
    with pytest.raises(NotConnectedError):
        await service.implied_volatility(AAPL_OPTION, option_price=1, underlying_price=200)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"strike_min": 95.0, "strike_max": 105.0}, [95.0, 100.0, 105.0]),
        ({"strike_min": 108.0}, [110.0]),
        ({"strike_max": 90.0}, [90.0]),
        ({"price": 101.0, "around": 3}, [100.0, 105.0, 95.0]),
        ({"price": 102.5, "around": 2}, [100.0, 105.0]),  # a tie goes to the lower strike
    ],
)
def test_select_strikes(kwargs: dict[str, Any], expected: list[float]) -> None:
    arguments: dict[str, Any] = {"strike_min": None, "strike_max": None, "around": 5, "price": None}
    arguments.update(kwargs)
    assert _select_strikes([90.0, 95.0, 100.0, 105.0, 110.0], **arguments) == expected
