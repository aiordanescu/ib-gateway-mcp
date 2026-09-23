"""Contract lookups end to end: real ``reqContractDetails`` traffic decoded by ib_async."""

from __future__ import annotations

import asyncio

import pytest

from ib_gateway_mcp.errors import AmbiguousContractError, NotFoundError
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.common import ContractOut, ContractSpec
from tests.e2e.fake_tws import AAPL, FakeBond, FakeStock, FakeTws

_REQ_CONTRACT_DETAILS = 9

# Two listings of one (fictional) symbol, so a bare symbol is ambiguous.
ACME_NYSE = FakeStock(
    con_id=9100001,
    symbol="ACME",
    long_name="ACME WIDGETS INC",
    primary_exchange="NYSE",
    valid_exchanges=("SMART", "NYSE"),
)
ACME_ARCA = FakeStock(
    con_id=9100002,
    symbol="ACME",
    long_name="ACME WIDGETS INC CL B",
    primary_exchange="ARCA",
    valid_exchanges=("SMART", "ARCA"),
)

ACME_BOND = FakeBond(
    con_id=9100100,
    issuer="ACME",
    cusip="000000AA0",
    coupon="4.25",
    maturity="20300315",
    long_name="ACME WIDGETS 4 1/4 03/15/30",
)

WITHHELD_BOND = FakeBond(
    con_id=9100101,
    issuer="ACME",
    cusip="IBCID9100101",
    coupon="",
    maturity="",
    long_name="",
    currency="",
    issue_date="",
    bond_type="",
    coupon_type="",
)
"""A bond as IBKR sends it to a login without bond reference data (IB Gateway 10.45)."""


@pytest.fixture
def two_acme_listings(fake_tws: FakeTws) -> None:
    fake_tws.contracts.extend([ACME_NYSE, ACME_ARCA])


async def test_qualify_contract_by_symbol(gateway: Gateway, fake_tws: FakeTws) -> None:
    out = await gateway.contracts.qualify_contract(ContractSpec(symbol="AAPL"))
    assert out.con_id == AAPL.con_id
    assert out.symbol == "AAPL"
    assert out.sec_type == "STK"
    assert out.exchange == "SMART"
    assert out.primary_exchange == "NASDAQ"
    assert out.currency == "USD"
    assert out.local_symbol == "AAPL"
    assert out.trading_class == "NMS"
    assert out.description == "APPLE INC"
    assert out.strike is None

    # What went over the wire: the spec's symbol, type, exchange and currency.
    (request,) = fake_tws.current.requests(_REQ_CONTRACT_DETAILS)
    assert request[3:15] == [
        "0",
        "AAPL",
        "STK",
        "",
        "0.0",  # strike
        "",
        "",
        "SMART",
        "",
        "USD",
        "",
        "",
    ]


async def test_get_contract_details_by_con_id(gateway: Gateway, fake_tws: FakeTws) -> None:
    result = await gateway.contracts.contract_details(ContractSpec(con_id=AAPL.con_id))
    assert result.total == 1
    assert result.truncated is False
    (details,) = result.contracts
    assert details.contract.con_id == AAPL.con_id
    assert details.contract.description == "APPLE INC"
    assert details.market_name == "NMS"
    assert details.industry == "Technology"
    assert details.category == "Computers"
    assert details.stock_type == "COMMON"
    assert details.time_zone_id == "US/Eastern"
    assert details.min_tick == pytest.approx(0.01)
    assert details.min_size == pytest.approx(0.0001)
    assert details.size_increment == pytest.approx(0.0001)
    assert details.suggested_size_increment == pytest.approx(100)
    assert details.valid_exchanges == list(AAPL.valid_exchanges)
    assert details.market_rule_ids == [26] * len(AAPL.valid_exchanges)
    assert "LMT" in details.order_types
    assert details.sec_ids == {"ISIN": "US0378331005"}
    assert details.under_con_id is None
    # Seven days of hours: every weekday has a session, the weekend is closed.
    assert details.trading_sessions is not None
    assert len(details.trading_sessions) == 5
    assert details.liquid_sessions is not None
    assert len(details.liquid_sessions) == 5
    assert len(details.closed_dates) == 2
    assert details.trading_hours is None  # parsed, so the raw text is not repeated

    # A con_id lookup sends only the id: the spec's STK/SMART/USD defaults stay out.
    (request,) = fake_tws.current.requests(_REQ_CONTRACT_DETAILS)
    con_id, symbol, sec_type = request[3:6]
    exchange, currency = request[10], request[12]
    assert (con_id, symbol, sec_type, exchange, currency) == (str(AAPL.con_id), "", "", "", "")


async def test_unknown_symbol_is_not_found_from_error_200(gateway: Gateway) -> None:
    started = asyncio.get_running_loop().time()
    with pytest.raises(NotFoundError, match="No contract matches NOPE STK SMART USD"):
        await gateway.contracts.qualify_contract(ContractSpec(symbol="NOPE"))
    # Error 200 ends the request; it does not wait for the 1 s request timeout.
    assert asyncio.get_running_loop().time() - started < 0.5
    # A request error is not a connection-level error.
    assert gateway.health().last_error is None


@pytest.mark.usefixtures("two_acme_listings")
async def test_ambiguous_symbol_lists_candidates(gateway: Gateway) -> None:
    with pytest.raises(AmbiguousContractError) as caught:
        await gateway.contracts.qualify_contract(ContractSpec(symbol="ACME"))
    assert "matches 2 contracts" in str(caught.value)
    candidates = caught.value.candidates
    assert all(isinstance(candidate, ContractOut) for candidate in candidates)
    assert {getattr(candidate, "con_id", None) for candidate in candidates} == {
        ACME_NYSE.con_id,
        ACME_ARCA.con_id,
    }

    narrowed = await gateway.contracts.qualify_contract(
        ContractSpec(symbol="ACME", primary_exchange="ARCA")
    )
    assert narrowed.con_id == ACME_ARCA.con_id
    assert narrowed.description == "ACME WIDGETS INC CL B"


@pytest.mark.usefixtures("two_acme_listings")
async def test_concurrent_lookups_are_matched_by_request_id(gateway: Gateway) -> None:
    specs = [
        ContractSpec(symbol="ACME", primary_exchange="NYSE"),
        ContractSpec(con_id=AAPL.con_id),
        ContractSpec(symbol="ACME", primary_exchange="ARCA"),
        ContractSpec(symbol="AAPL", exchange="NASDAQ"),
    ]
    contracts = await gateway.contracts.qualify_many(specs)
    assert [contract.conId for contract in contracts] == [
        ACME_NYSE.con_id,
        AAPL.con_id,
        ACME_ARCA.con_id,
        AAPL.con_id,
    ]
    assert contracts[3].exchange == "NASDAQ"


async def test_market_rule_increments(gateway: Gateway) -> None:
    result = await gateway.contracts.market_rules([26, 26])
    assert result.missing_ids == []
    (rule,) = result.rules
    assert rule.market_rule_id == 26
    assert [(row.low_edge, row.increment) for row in rule.increments] == [(0.0, 0.01)]


async def test_stock_by_isin(gateway: Gateway) -> None:
    out = await gateway.contracts.qualify_contract(
        ContractSpec(sec_id_type="ISIN", sec_id="US0378331005")
    )
    assert out.con_id == AAPL.con_id


async def test_bond_with_a_fractional_coupon_decodes(gateway: Gateway, fake_tws: FakeTws) -> None:
    # ib_async 2.1.0 alone drops this row (int("4.25") in its decoder); the connection
    # manager's compat shim makes the coupon decode as a float.
    fake_tws.contracts.append(ACME_BOND)
    result = await gateway.contracts.contract_details(
        ContractSpec(sec_type="BOND", sec_id_type="CUSIP", sec_id=ACME_BOND.cusip)
    )
    (details,) = result.contracts
    assert details.contract.con_id == ACME_BOND.con_id
    assert details.contract.sec_type == "BOND"
    assert details.contract.description == "ACME WIDGETS 4 1/4 03/15/30"
    assert details.bond is not None
    assert details.bond.coupon == pytest.approx(4.25)
    assert details.bond.maturity == "20300315"
    assert details.bond.cusip == ACME_BOND.cusip
    assert details.bond.callable is True
    assert details.sec_ids == {"CUSIP": ACME_BOND.cusip}


async def test_bond_without_reference_data_has_no_coupon(
    gateway: Gateway, fake_tws: FakeTws
) -> None:
    # The empty coupon field decodes as 0 in ib_async; it must not read as a 0% coupon.
    fake_tws.contracts.append(WITHHELD_BOND)
    result = await gateway.contracts.contract_details(ContractSpec(con_id=WITHHELD_BOND.con_id))
    (details,) = result.contracts
    assert details.bond is not None
    assert details.bond.coupon is None
    assert details.bond.maturity is None
    assert details.bond.cusip == "IBCID9100101"
