"""ContractsService against the autospecced fake IB."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from datetime import date, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from ib_async import (
    Contract,
    ContractDescription,
    DepthMktDataDescription,
    OptionChain,
    PriceIncrement,
    SmartComponent,
    TagValue,
)
from ib_async.util import UNSET_INTEGER
from ib_async.wrapper import RequestError

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    AmbiguousContractError,
    IbApiError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RequestTimeoutError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.common import ComboLegSpec, ContractSpec
from ib_gateway_mcp.services import contracts as contracts_module
from ib_gateway_mcp.services.contracts import (
    DETAILS_LIMIT_DEFAULT,
    DETAILS_LIMIT_MAX,
    MARKET_RULES_MAX,
    ContractsService,
)
from tests.fakes import (
    contract_details,
    future,
    go_offline,
    option,
    pending,
    raises,
    returns,
    stock,
)

EASTERN = ZoneInfo("US/Eastern")
TRADING_HOURS = "20260105:0400-20260105:2000;20260106:CLOSED;20260107:0400-20260107:2000"
LIQUID_HOURS = "20260105:0930-20260105:1600;20260106:CLOSED;20260107:0930-20260107:1600"


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    """Short request timeout so hanging requests fail fast."""
    return settings_factory(request_timeout=0.2)


@pytest.fixture
def service(gateway: Gateway) -> ContractsService:
    return gateway.contracts


def description(
    contract: Contract | None = None, derivatives: list[str] | None = None
) -> ContractDescription:
    """A reqMatchingSymbols row, as the decoder builds it (no exchange, name in description)."""
    the_contract = contract or Contract(
        conId=265598,
        symbol="AAPL",
        secType="STK",
        primaryExchange="NASDAQ",
        currency="USD",
        description="APPLE INC",
    )
    return ContractDescription(
        contract=the_contract,
        derivativeSecTypes=derivatives if derivatives is not None else ["CFD", "OPT", "WAR"],
    )


def chain(
    exchange: str = "SMART",
    trading_class: str = "AAPL",
    expirations: list[str] | None = None,
    strikes: list[float] | None = None,
    multiplier: str = "100",
) -> OptionChain:
    """A reqSecDefOptParams row; the decoder leaves underlyingConId as a string."""
    return OptionChain(
        exchange,
        "265598",  # type: ignore[arg-type]
        trading_class,
        multiplier,
        expirations if expirations is not None else ["20261218", "20260116"],
        strikes if strikes is not None else [210.0, 190.0, 200.0],
    )


# --- search_symbols ---------------------------------------------------------------------------


async def test_search_symbols(service: ContractsService, fake_ib: MagicMock) -> None:
    bond = Contract(conId=5, symbol="T", secType="BOND", currency="USD", issuerId="e1234567")
    fake_ib.reqMatchingSymbolsAsync.side_effect = returns(
        [description(), description(bond, derivatives=[])]
    )
    result = await service.search_symbols("  apple ")
    fake_ib.reqMatchingSymbolsAsync.assert_called_once_with("apple")
    assert (result.pattern, result.total, result.truncated) == ("apple", 2, False)
    first = result.matches[0]
    assert (first.contract.con_id, first.contract.symbol, first.contract.sec_type) == (
        265598,
        "AAPL",
        "STK",
    )
    assert first.contract.primary_exchange == "NASDAQ"
    assert first.contract.exchange is None
    assert first.contract.description == "APPLE INC"
    assert first.derivative_sec_types == ["CFD", "OPT", "WAR"]
    assert first.issuer_id is None
    assert result.matches[1].issuer_id == "e1234567"


async def test_search_symbols_applies_the_limit(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    rows = [description(stock(f"A{i}", con_id=i + 1)) for i in range(5)]
    fake_ib.reqMatchingSymbolsAsync.side_effect = returns(rows)
    result = await service.search_symbols("A", limit=2)
    assert [m.contract.symbol for m in result.matches] == ["A0", "A1"]
    assert (result.total, result.truncated) == (5, True)


async def test_search_symbols_skips_rows_without_a_contract(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqMatchingSymbolsAsync.side_effect = returns(
        [ContractDescription(), description(Contract(symbol="X")), description()]
    )
    result = await service.search_symbols("AAP")
    assert [m.contract.con_id for m in result.matches] == [265598]


async def test_search_symbols_not_found(service: ContractsService, fake_ib: MagicMock) -> None:
    fake_ib.reqMatchingSymbolsAsync.side_effect = returns([])
    with pytest.raises(NotFoundError, match="No instrument matches 'ZZZZQ'"):
        await service.search_symbols("ZZZZQ")


async def test_search_symbols_times_out_when_ib_async_gives_up(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    """reqMatchingSymbolsAsync returns None after its own 4-second wait."""
    fake_ib.reqMatchingSymbolsAsync.side_effect = returns(None)
    with pytest.raises(RequestTimeoutError, match="4-second"):
        await service.search_symbols("AAPL")


async def test_search_symbols_rejects_a_blank_pattern(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match="pattern"):
        await service.search_symbols("   ")
    fake_ib.reqMatchingSymbolsAsync.assert_not_called()


async def test_search_symbols_maps_api_errors(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqMatchingSymbolsAsync.side_effect = raises(RequestError(3, 162, "pacing violation"))
    with pytest.raises(IbApiError) as info:
        await service.search_symbols("AAPL")
    assert info.value.error_code == 162


async def test_search_symbols_are_spaced_out(
    service: ContractsService, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IBKR paces symbol searches to about one per second, even after a failure."""
    monkeypatch.setattr(contracts_module, "SEARCH_INTERVAL", 0.1)
    sent: list[float] = []

    async def answer(_pattern: str) -> list[ContractDescription]:
        sent.append(time.monotonic())
        if len(sent) == 1:
            raise RequestError(3, 162, "pacing")
        return [description()]

    fake_ib.reqMatchingSymbolsAsync.side_effect = answer
    with pytest.raises(IbApiError):
        await service.search_symbols("AAPL")
    results = await asyncio.gather(service.search_symbols("AAPL"), service.search_symbols("MSFT"))
    assert all(r.total == 1 for r in results)
    assert len(sent) == 3
    assert sent[1] - sent[0] >= 0.09
    assert sent[2] - sent[1] >= 0.09


# --- contract_details -------------------------------------------------------------------------


async def test_contract_details_maps_every_field(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    row = contract_details(
        stock(),
        orderTypes="ACTIVETIM,LMT,MKT, STP",
        validExchanges="SMART,NASDAQ,NYSE",
        marketRuleIds="26,26,26",
        tradingHours=TRADING_HOURS,
        liquidHours=LIQUID_HOURS,
        industry="Technology",
        category="Computers",
        subcategory="Computers",
        stockType="COMMON",
        secIdList=[TagValue("ISIN", "US0378331005")],
        minSize=1.0,
        sizeIncrement=1.0,
        suggestedSizeIncrement=100.0,
        priceMagnifier=1,
        aggGroup=1,
    )
    fake_ib.reqContractDetailsAsync.side_effect = returns([row])
    result = await service.contract_details(ContractSpec(symbol="AAPL"))
    assert (result.total, result.truncated) == (1, False)
    details = result.contracts[0]
    assert details.contract.con_id == 265598
    assert details.contract.description == "APPLE INC"
    assert details.market_name == "NMS"
    assert (details.industry, details.category, details.stock_type) == (
        "Technology",
        "Computers",
        "COMMON",
    )
    assert details.time_zone_id == "US/Eastern"
    assert details.min_tick == 0.01
    assert (details.min_size, details.size_increment, details.suggested_size_increment) == (
        1.0,
        1.0,
        100.0,
    )
    assert details.order_types == ["ACTIVETIM", "LMT", "MKT", "STP"]
    assert details.valid_exchanges == ["SMART", "NASDAQ", "NYSE"]
    assert details.market_rule_ids == [26, 26, 26]
    assert details.sec_ids == {"ISIN": "US0378331005"}
    assert details.price_magnifier == 1
    assert details.agg_group == 1
    assert details.under_con_id is None
    assert details.bond is None
    # sessions are aware datetimes in the exchange's zone
    assert details.trading_sessions is not None
    assert [(s.start, s.end) for s in details.trading_sessions] == [
        (datetime(2026, 1, 5, 4, tzinfo=EASTERN), datetime(2026, 1, 5, 20, tzinfo=EASTERN)),
        (datetime(2026, 1, 7, 4, tzinfo=EASTERN), datetime(2026, 1, 7, 20, tzinfo=EASTERN)),
    ]
    assert details.liquid_sessions is not None
    assert details.liquid_sessions[0].start == datetime(2026, 1, 5, 9, 30, tzinfo=EASTERN)
    assert details.closed_dates == [date(2026, 1, 6)]
    assert details.trading_hours is None
    assert details.liquid_hours is None
    dumped = details.model_dump(mode="json")
    assert dumped["liquid_sessions"][0]["start"] == "2026-01-05T09:30:00-05:00"
    assert dumped["closed_dates"] == ["2026-01-06"]


async def test_contract_details_cleans_nan_and_unset_values(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    row = contract_details(
        stock(),
        minTick=math.nan,
        minSize=float(2**127 - 1),
        sizeIncrement=math.nan,
        aggGroup=UNSET_INTEGER,
        marketRuleIds="26,,x",
        tradingHours="",
        liquidHours="",
    )
    fake_ib.reqContractDetailsAsync.side_effect = returns([row])
    details = (await service.contract_details(ContractSpec(symbol="AAPL"))).contracts[0]
    assert details.min_tick is None
    assert details.min_size is None
    assert details.size_increment is None
    assert details.agg_group is None
    assert details.market_rule_ids == [26]
    assert details.trading_sessions == []
    assert details.closed_dates == []
    assert details.trading_hours is None


async def test_contract_details_parses_overnight_and_old_hours(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    chicago = ZoneInfo("US/Central")
    row = contract_details(
        future(),
        timeZoneId="US/Central",
        tradingHours="20260104:1700-20260105:1600;20260105:1700-20260106:1600",
        liquidHours="20260105:0830-1500,1700-0200;20260106:CLOSED",
    )
    fake_ib.reqContractDetailsAsync.side_effect = returns([row])
    details = (
        await service.contract_details(ContractSpec(symbol="ES", sec_type="FUT", exchange="CME"))
    ).contracts[0]
    assert details.trading_sessions is not None
    assert details.trading_sessions[0].start == datetime(2026, 1, 4, 17, tzinfo=chicago)
    assert details.trading_sessions[0].end == datetime(2026, 1, 5, 16, tzinfo=chicago)
    assert details.liquid_sessions is not None
    assert [(s.start, s.end) for s in details.liquid_sessions] == [
        (datetime(2026, 1, 5, 8, 30, tzinfo=chicago), datetime(2026, 1, 5, 15, tzinfo=chicago)),
        (datetime(2026, 1, 5, 17, tzinfo=chicago), datetime(2026, 1, 6, 2, tzinfo=chicago)),
    ]
    # closed_dates come from the trading hours only
    assert details.closed_dates == []


@pytest.mark.parametrize(
    ("time_zone", "hours"),
    [
        ("US/Eastern", "not hours at all"),
        ("US/Eastern", "20260105:0930-20260105:0800"),
        ("Mars/Olympus_Mons", TRADING_HOURS),
        ("", TRADING_HOURS),
    ],
)
async def test_contract_details_keeps_unparsed_hours_raw(
    service: ContractsService, fake_ib: MagicMock, time_zone: str, hours: str
) -> None:
    row = contract_details(stock(), timeZoneId=time_zone, tradingHours=hours, liquidHours="")
    fake_ib.reqContractDetailsAsync.side_effect = returns([row])
    details = (await service.contract_details(ContractSpec(symbol="AAPL"))).contracts[0]
    assert details.trading_sessions is None
    assert details.trading_hours == hours
    assert details.closed_dates == []
    assert details.time_zone_id == (time_zone or None)


async def test_contract_details_sorts_and_truncates_derivatives(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    rows = [
        contract_details(option(expiry=expiry, strike=strike, right=right, con_id=con_id))
        for con_id, (expiry, strike, right) in enumerate(
            [
                ("20261218", 210.0, "C"),
                ("20260116", 200.0, "P"),
                ("20261218", 200.0, "C"),
                ("20260116", 200.0, "C"),
            ],
            start=1,
        )
    ]
    fake_ib.reqContractDetailsAsync.side_effect = returns(rows)
    spec = ContractSpec(symbol="AAPL", sec_type="OPT")
    result = await service.contract_details(spec, limit=3)
    assert [c.contract.con_id for c in result.contracts] == [4, 2, 3]
    assert (result.total, result.truncated) == (4, True)
    assert (await service.contract_details(spec)).truncated is False


async def test_contract_details_limit_defaults_and_cap(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    rows = [contract_details(stock(con_id=i + 1)) for i in range(DETAILS_LIMIT_MAX + 5)]
    fake_ib.reqContractDetailsAsync.side_effect = returns(rows)
    spec = ContractSpec(symbol="AAPL")
    assert len((await service.contract_details(spec)).contracts) == DETAILS_LIMIT_DEFAULT
    capped = await service.contract_details(spec, limit=10_000)
    assert len(capped.contracts) == DETAILS_LIMIT_MAX
    assert (capped.total, capped.truncated) == (DETAILS_LIMIT_MAX + 5, True)


async def test_contract_details_merges_rows_of_one_contract(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    island, nyse = stock(), stock()
    island.exchange, nyse.exchange = "ISLAND", "NYSE"
    event = Contract(secType="EC", conId=99, symbol="AAPL", exchange="NYSE")
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [contract_details(island), contract_details(nyse), contract_details(event)]
    )
    result = await service.contract_details(ContractSpec(symbol="AAPL", exchange="NYSE"))
    assert result.total == 1
    assert result.contracts[0].contract.exchange == "NYSE"


async def test_contract_details_by_con_id_sends_only_the_id(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(future())])
    await service.contract_details(ContractSpec(con_id=800001))
    request = fake_ib.reqContractDetailsAsync.call_args.args[0]
    assert (request.conId, request.secType, request.exchange) == (800001, "", "")


async def test_contract_details_includes_bond_terms(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    """The mapping of bond rows. (ib_async 2.1.0 needs the connection's coupon shim to decode
    a 4.25 coupon at all; see test_connection.)"""
    bond = Contract(secType="BOND", conId=7, symbol="T", exchange="SMART", currency="USD")
    row = contract_details(
        bond,
        cusip="912828XX0",
        coupon=4.25,
        maturity="20301115",
        bondType="US Treasury",
        callable=True,
        notes="",
    )
    fake_ib.reqContractDetailsAsync.side_effect = returns([row])
    details = (
        await service.contract_details(
            ContractSpec(sec_type="BOND", sec_id_type="CUSIP", sec_id="912828XX0")
        )
    ).contracts[0]
    assert details.bond is not None
    assert (details.bond.cusip, details.bond.coupon, details.bond.maturity) == (
        "912828XX0",
        4.25,
        "20301115",
    )
    assert details.bond.callable is True
    assert details.bond.notes is None


@pytest.mark.parametrize(
    ("coupon", "maturity", "expected"),
    [
        (0.0, "", None),  # terms withheld: ib_async decodes the empty coupon as 0
        (0.0, "20300315", 0.0),  # a real zero-coupon bond
        (7.0, "", 7.0),
    ],
)
async def test_bond_coupon_is_unknown_when_ibkr_withholds_the_terms(
    service: ContractsService,
    fake_ib: MagicMock,
    coupon: float,
    maturity: str,
    expected: float | None,
) -> None:
    """Without bond reference data IBKR sends empty terms and an IBCID placeholder CUSIP."""
    bond = Contract(secType="BOND", conId=29105555, exchange="SMART", tradingClass="IBM")
    row = contract_details(
        bond, cusip="IBCID29105555", coupon=coupon, maturity=maturity, descAppend="IBM 7 10/30/45"
    )
    fake_ib.reqContractDetailsAsync.side_effect = returns([row])
    details = (await service.contract_details(ContractSpec(con_id=29105555))).contracts[0]
    assert details.bond is not None
    assert details.bond.coupon == expected
    assert details.bond.desc_append == "IBM 7 10/30/45"


async def test_contract_details_not_found(service: ContractsService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(
        RequestError(4, 200, "No security definition has been found for the request")
    )
    with pytest.raises(NotFoundError, match="No contract matches NOPE STK SMART USD") as info:
        await service.contract_details(ContractSpec(symbol="NOPE"))
    assert isinstance(info.value.__cause__, IbApiError)

    fake_ib.reqContractDetailsAsync.side_effect = returns([])
    with pytest.raises(NotFoundError):
        await service.contract_details(ContractSpec(symbol="NOPE"))


async def test_contract_details_passes_other_errors_through(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(RequestError(4, 321, "Invalid request"))
    with pytest.raises(IbApiError) as info:
        await service.contract_details(ContractSpec(symbol="AAPL"))
    assert info.value.error_code == 321


async def test_contract_details_times_out(service: ContractsService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match="contract details for AAPL STK SMART USD"):
        await service.contract_details(ContractSpec(symbol="AAPL"))


async def test_contract_details_refuses_combos(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    spec = ContractSpec(
        symbol="SPY",
        sec_type="BAG",
        combo_legs=[ComboLegSpec(con_id=1, action="BUY"), ComboLegSpec(con_id=2, action="SELL")],
    )
    with pytest.raises(InvalidRequestError, match="each leg"):
        await service.contract_details(spec)
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_contract_details_when_disconnected(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    go_offline(fake_ib)
    with pytest.raises(NotConnectedError):
        await service.contract_details(ContractSpec(symbol="AAPL"))


# --- qualify_contract -------------------------------------------------------------------------


async def test_qualify_contract(service: ContractsService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    out = await service.qualify_contract(ContractSpec(symbol="AAPL"))
    assert (out.con_id, out.symbol, out.exchange, out.primary_exchange) == (
        265598,
        "AAPL",
        "SMART",
        "NASDAQ",
    )
    assert out.description == "APPLE INC"


async def test_qualify_contract_ambiguous(service: ContractsService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [
            contract_details(stock(con_id=1)),
            contract_details(stock(con_id=2, primaryExchange="NYSE")),
        ]
    )
    with pytest.raises(AmbiguousContractError, match="matches 2 contracts") as info:
        await service.qualify_contract(ContractSpec(symbol="AAPL"))
    assert len(info.value.candidates) == 2


async def test_qualify_contract_not_found(service: ContractsService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(RequestError(4, 200, "No security"))
    with pytest.raises(NotFoundError, match="NOPE"):
        await service.qualify_contract(ContractSpec(symbol="NOPE"))


# --- option_chain -----------------------------------------------------------------------------


async def test_option_chain_merges_identical_listings(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.reqSecDefOptParamsAsync.side_effect = returns(
        [
            chain("SMART"),
            chain("CBOE"),
            chain("SMART", trading_class="2AAPL", strikes=[100.0], expirations=["20260116"]),
            chain("AMEX", strikes=[200.0, math.nan]),
        ]
    )
    result = await service.option_chain(ContractSpec(symbol="AAPL"))
    fake_ib.reqSecDefOptParamsAsync.assert_called_once_with("AAPL", "", "STK", 265598)
    assert result.underlying.con_id == 265598
    assert [(c.trading_class, c.exchanges) for c in result.chains] == [
        ("2AAPL", ["SMART"]),
        ("AAPL", ["AMEX"]),
        ("AAPL", ["CBOE", "SMART"]),
    ]
    merged = result.chains[2]
    assert merged.expirations == ["20260116", "20261218"]
    assert merged.strikes == [190.0, 200.0, 210.0]
    assert merged.multiplier == "100"
    assert result.chains[1].strikes == [200.0]  # NaN strike dropped


async def test_option_chain_filters_by_exchange(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.reqSecDefOptParamsAsync.side_effect = returns(
        [chain("SMART"), chain("CBOE", strikes=[1.0])]
    )
    result = await service.option_chain(ContractSpec(symbol="AAPL"), exchange="cboe")
    assert [c.exchanges for c in result.chains] == [["CBOE"]]

    with pytest.raises(NotFoundError, match="on exchange ISE; chains are listed on CBOE, SMART"):
        await service.option_chain(ContractSpec(symbol="AAPL"), exchange="ISE")


async def test_option_chain_on_a_future_uses_its_exchange(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [contract_details(future(), validExchanges="CME")]
    )
    fake_ib.reqSecDefOptParamsAsync.side_effect = returns([chain("CME", trading_class="ES")])
    spec = ContractSpec(
        symbol="ES", sec_type="FUT", exchange="CME", last_trade_date_or_contract_month="202612"
    )
    await service.option_chain(spec)
    fake_ib.reqSecDefOptParamsAsync.assert_called_with("ES", "CME", "FUT", 800001)

    await service.option_chain(spec, fut_fop_exchange=" globex ")
    fake_ib.reqSecDefOptParamsAsync.assert_called_with("ES", "GLOBEX", "FUT", 800001)


async def test_option_chain_on_a_continuous_future_asks_for_the_future(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    front = future()
    front.secType = "CONTFUT"
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(front)])
    fake_ib.reqSecDefOptParamsAsync.side_effect = returns([chain("CME", trading_class="ES")])
    await service.option_chain(ContractSpec(symbol="ES", sec_type="CONTFUT", exchange="CME"))
    fake_ib.reqSecDefOptParamsAsync.assert_called_once_with("ES", "CME", "FUT", 800001)


async def test_option_chain_refuses_an_option_given_by_con_id(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    with pytest.raises(InvalidRequestError, match=r"AAPL OPT \(con_id 700001\) is itself"):
        await service.option_chain(ContractSpec(con_id=700001))
    fake_ib.reqSecDefOptParamsAsync.assert_not_called()


async def test_option_chain_not_found(service: ContractsService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.reqSecDefOptParamsAsync.side_effect = returns([])
    with pytest.raises(NotFoundError, match=r"no options on AAPL STK \(con_id 265598\)"):
        await service.option_chain(ContractSpec(symbol="AAPL"))


async def test_option_chain_unknown_underlying(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(RequestError(4, 200, "No security"))
    with pytest.raises(NotFoundError, match="NOPE"):
        await service.option_chain(ContractSpec(symbol="NOPE"))
    fake_ib.reqSecDefOptParamsAsync.assert_not_called()


async def test_option_chain_refuses_an_option_as_underlying(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match="not OPT"):
        await service.option_chain(ContractSpec(symbol="AAPL", sec_type="OPT"))
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_option_chain_maps_api_errors(service: ContractsService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.reqSecDefOptParamsAsync.side_effect = raises(RequestError(9, 321, "Invalid underlying"))
    with pytest.raises(IbApiError) as info:
        await service.option_chain(ContractSpec(symbol="AAPL"))
    assert info.value.error_code == 321


# --- market_rules -----------------------------------------------------------------------------


async def test_market_rules(service: ContractsService, fake_ib: MagicMock) -> None:
    ladders = {
        26: [PriceIncrement(0.0, 0.01)],
        239: [PriceIncrement(0.0, 0.01), PriceIncrement(1.0, math.nan), PriceIncrement(3, 0.05)],
    }

    async def answer(rule_id: int) -> list[PriceIncrement]:
        return ladders[rule_id]

    fake_ib.reqMarketRuleAsync.side_effect = answer
    result = await service.market_rules([239, 26, 239])
    assert [call.args[0] for call in fake_ib.reqMarketRuleAsync.call_args_list] == [239, 26]
    assert [r.market_rule_id for r in result.rules] == [239, 26]
    assert [(i.low_edge, i.increment) for i in result.rules[0].increments] == [
        (0.0, 0.01),
        (3.0, 0.05),
    ]
    assert result.missing_ids == []


async def test_market_rules_reports_unanswered_ids(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    """ib_async returns None when a rule is not answered within its fixed 1 second."""

    async def answer(rule_id: int) -> list[PriceIncrement] | None:
        return [PriceIncrement(0.0, 0.25)] if rule_id == 26 else None

    fake_ib.reqMarketRuleAsync.side_effect = answer
    result = await service.market_rules([26, 99999])
    assert [r.market_rule_id for r in result.rules] == [26]
    assert result.missing_ids == [99999]

    with pytest.raises(NotFoundError, match="99999"):
        await service.market_rules([99999])


async def test_market_rules_serializes_the_same_id(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    """ib_async keys rule requests by 'marketRule-<id>': one id is never in flight twice."""
    active: dict[int, int] = {}
    peak: dict[int, int] = {}

    async def slow(rule_id: int) -> list[PriceIncrement]:
        active[rule_id] = active.get(rule_id, 0) + 1
        peak[rule_id] = max(peak.get(rule_id, 0), active[rule_id])
        await asyncio.sleep(0.01)
        active[rule_id] -= 1
        return [PriceIncrement(0.0, 0.01)]

    fake_ib.reqMarketRuleAsync.side_effect = slow
    await asyncio.gather(service.market_rules([26, 27]), service.market_rules([26]))
    assert peak == {26: 1, 27: 1}


@pytest.mark.parametrize(
    ("ids", "message"),
    [
        ([], "at least one"),
        (list(range(MARKET_RULES_MAX + 1)), f"At most {MARKET_RULES_MAX}"),
        ([-1], "non-negative"),
    ],
)
async def test_market_rules_validates_ids(
    service: ContractsService, fake_ib: MagicMock, ids: list[int], message: str
) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        await service.market_rules(ids)
    fake_ib.reqMarketRuleAsync.assert_not_called()


async def test_market_rules_raise_request_errors(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqMarketRuleAsync.side_effect = raises(ConnectionError("socket closed"))
    with pytest.raises(NotConnectedError, match="market rule 26"):
        await service.market_rules([26, 27])


# --- smart_components -------------------------------------------------------------------------


async def test_smart_components(service: ContractsService, fake_ib: MagicMock) -> None:
    fake_ib.reqSmartComponentsAsync.side_effect = returns(
        [SmartComponent(1, "NYSE", "N"), SmartComponent(2, "NASDAQ", "Q")]
    )
    result = await service.smart_components(" 9c0001 ")
    fake_ib.reqSmartComponentsAsync.assert_called_once_with("9c0001")
    assert result.bbo_exchange == "9c0001"
    assert [(c.bit_number, c.exchange, c.exchange_letter) for c in result.components] == [
        (1, "NYSE", "N"),
        (2, "NASDAQ", "Q"),
    ]


async def test_smart_components_empty_is_not_found(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqSmartComponentsAsync.side_effect = returns([])
    with pytest.raises(NotFoundError, match="outside trading hours"):
        await service.smart_components("9c0001")


async def test_smart_components_rejects_a_blank_code(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match="bbo_exchange"):
        await service.smart_components("  ")
    fake_ib.reqSmartComponentsAsync.assert_not_called()


async def test_smart_components_maps_api_errors(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqSmartComponentsAsync.side_effect = raises(RequestError(5, 322, "bad code"))
    with pytest.raises(IbApiError, match="322"):
        await service.smart_components("zz")


# --- depth_exchanges --------------------------------------------------------------------------


def depth_rows() -> list[DepthMktDataDescription]:
    return [
        DepthMktDataDescription("NASDAQ", "STK", "NASDAQ", "Deep2", 1),
        DepthMktDataDescription("CME", "FUT", "", "Deep", UNSET_INTEGER),
    ]


async def test_depth_exchanges_are_cached_per_session(
    service: ContractsService,
    gateway: Gateway,
    fake_ib: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ib.reqMktDepthExchangesAsync.side_effect = returns(depth_rows())
    first = await service.depth_exchanges()
    assert [(e.exchange, e.sec_type, e.service_data_type) for e in first.exchanges] == [
        ("NASDAQ", "STK", "Deep2"),
        ("CME", "FUT", "Deep"),
    ]
    assert first.exchanges[0].listing_exchange == "NASDAQ"
    assert first.exchanges[0].agg_group == 1
    assert first.exchanges[1].listing_exchange is None
    assert first.exchanges[1].agg_group is None

    assert await service.depth_exchanges() == first
    assert fake_ib.reqMktDepthExchangesAsync.call_count == 1

    # a new session (reconnect) asks again
    reconnected = datetime(2030, 1, 1, tzinfo=EASTERN)
    monkeypatch.setattr(
        type(gateway.connection), "connected_since", property(lambda _self: reconnected)
    )
    await service.depth_exchanges()
    assert fake_ib.reqMktDepthExchangesAsync.call_count == 2


async def test_depth_exchanges_serializes_the_shared_key(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    active = peak = 0

    async def slow() -> list[DepthMktDataDescription]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return depth_rows()

    fake_ib.reqMktDepthExchangesAsync.side_effect = slow
    results = await asyncio.gather(service.depth_exchanges(), service.depth_exchanges())
    assert results[0] == results[1]
    assert peak == 1
    assert fake_ib.reqMktDepthExchangesAsync.call_count == 1  # the second call used the cache


async def test_depth_exchanges_empty_is_not_found(
    service: ContractsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqMktDepthExchangesAsync.side_effect = returns([])
    with pytest.raises(NotFoundError):
        await service.depth_exchanges()
    fake_ib.reqMktDepthExchangesAsync.side_effect = returns(depth_rows())
    assert len((await service.depth_exchanges()).exchanges) == 2  # empty was not cached


async def test_depth_exchanges_times_out(service: ContractsService, fake_ib: MagicMock) -> None:
    fake_ib.reqMktDepthExchangesAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match="market depth exchanges"):
        await service.depth_exchanges()
