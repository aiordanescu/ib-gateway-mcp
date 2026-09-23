"""BaseService helpers every domain uses: contract qualification and subscriptions.

``_call`` itself is covered in ``test_gateway.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from ib_async import Bag, Contract, ContractDetails
from ib_async.wrapper import RequestError
from pydantic import BaseModel

from ib_gateway_mcp._util import contract_to_out
from ib_gateway_mcp.errors import (
    AmbiguousContractError,
    IbApiError,
    InvalidRequestError,
    NotFoundError,
    SubscriptionLimitError,
    SubscriptionNotFoundError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.common import ComboLegSpec, ContractOut, ContractSpec
from ib_gateway_mcp.services.base import MAX_LISTED_CANDIDATES, BaseService, describe_spec
from ib_gateway_mcp.subscriptions import Stream
from tests.fakes import contract_details, future, option, raises, returns, stock


@pytest.fixture
def service(gateway: Gateway) -> BaseService:
    return BaseService(gateway)


def listed_on(exchange: str) -> Contract:
    """The :func:`stock` contract as a direct-routed listing."""
    contract = stock()
    contract.exchange = exchange
    return contract


def sent_contract(fake_ib: MagicMock, call: int = -1) -> Contract:
    """The contract passed to ``reqContractDetailsAsync`` (last call by default)."""
    contract = fake_ib.reqContractDetailsAsync.call_args_list[call].args[0]
    assert isinstance(contract, Contract)
    return contract


# --- qualify ---------------------------------------------------------------------------------


async def test_qualify_returns_the_single_match(service: BaseService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    contract = await service.qualify(ContractSpec(symbol="AAPL"))
    assert (contract.conId, contract.symbol, contract.exchange) == (265598, "AAPL", "SMART")
    assert contract.includeExpired is False
    request = sent_contract(fake_ib)
    assert (request.symbol, request.secType, request.exchange) == ("AAPL", "STK", "SMART")


async def test_qualify_details_returns_the_row(service: BaseService, fake_ib: MagicMock) -> None:
    row = contract_details(stock(), longName="APPLE INC")
    fake_ib.reqContractDetailsAsync.side_effect = returns([row])
    details = await service.qualify_details(ContractSpec(symbol="AAPL"))
    assert details is row
    assert details.longName == "APPLE INC"


async def test_qualify_not_found_on_error_200(service: BaseService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(
        RequestError(5, 200, "No security definition has been found for the request")
    )
    with pytest.raises(NotFoundError, match=r"No contract matches NOPE STK SMART USD") as info:
        await service.qualify(ContractSpec(symbol="NOPE"))
    assert isinstance(info.value.__cause__, IbApiError)
    assert "search_symbols" in str(info.value)


async def test_qualify_not_found_on_no_rows(service: BaseService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([])
    with pytest.raises(NotFoundError):
        await service.qualify(ContractSpec(symbol="NOPE"))


async def test_qualify_passes_other_api_errors_through(
    service: BaseService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(RequestError(5, 162, "pacing"))
    with pytest.raises(IbApiError) as info:
        await service.qualify(ContractSpec(symbol="AAPL"))
    assert info.value.error_code == 162


async def test_qualify_ambiguous_lists_candidates(service: BaseService, fake_ib: MagicMock) -> None:
    rows = [
        contract_details(option(strike=200.0, con_id=1, tradingClass="AAPL")),
        contract_details(option(strike=200.0, con_id=2, tradingClass="2AAPL")),
    ]
    fake_ib.reqContractDetailsAsync.side_effect = returns(rows)
    spec = ContractSpec(
        symbol="AAPL",
        sec_type="OPT",
        last_trade_date_or_contract_month="20261218",
        strike=200,
        right="C",
    )
    with pytest.raises(AmbiguousContractError) as info:
        await service.qualify(spec)
    message = str(info.value)
    assert "AAPL OPT 20261218 200 C SMART USD matches 2 contracts" in message
    assert "con_id 1 " in message
    assert "class 2AAPL" in message
    candidates = info.value.candidates
    assert [c.con_id for c in candidates if isinstance(c, ContractOut)] == [1, 2]
    assert all(isinstance(c, ContractOut) for c in candidates)
    assert candidates[0].description == "APPLE INC"


async def test_qualify_caps_listed_candidates(service: BaseService, fake_ib: MagicMock) -> None:
    rows = [contract_details(stock(con_id=100 + i)) for i in range(MAX_LISTED_CANDIDATES + 5)]
    fake_ib.reqContractDetailsAsync.side_effect = returns(rows)
    with pytest.raises(AmbiguousContractError) as info:
        await service.qualify(ContractSpec(symbol="AAPL"))
    assert len(info.value.candidates) == MAX_LISTED_CANDIDATES
    assert f"matches {MAX_LISTED_CANDIDATES + 5} contracts" in str(info.value)
    assert str(info.value).endswith("and 5 more")


async def test_qualify_merges_rows_of_one_contract(
    service: BaseService, fake_ib: MagicMock
) -> None:
    """One conId listed per exchange is one contract, not an ambiguity."""
    rows = [
        contract_details(listed_on("ISLAND")),
        contract_details(listed_on("NYSE")),
    ]
    fake_ib.reqContractDetailsAsync.side_effect = returns(rows)
    contract = await service.qualify(ContractSpec(symbol="AAPL", exchange="NYSE"))
    assert (contract.conId, contract.exchange) == (265598, "NYSE")


async def test_qualify_drops_rows_of_another_sec_type(
    service: BaseService, fake_ib: MagicMock
) -> None:
    """IBKR adds event contracts to some FOP answers; ib_async filters them the same way."""
    fop = Contract(secType="FOP", conId=11, symbol="ES", exchange="CME")
    event = Contract(secType="EC", conId=12, symbol="ES", exchange="CME")
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [contract_details(fop), contract_details(event)]
    )
    contract = await service.qualify(ContractSpec(symbol="ES", sec_type="FOP", exchange="CME"))
    assert contract.conId == 11


async def test_qualify_by_con_id_sends_only_the_id(
    service: BaseService, fake_ib: MagicMock
) -> None:
    """The spec's STK/SMART/USD defaults must not contradict a future's conId."""
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [contract_details(future(), validExchanges="CME")]
    )
    contract = await service.qualify(ContractSpec(con_id=800001))
    request = sent_contract(fake_ib)
    assert (request.conId, request.secType, request.exchange, request.symbol) == (
        800001,
        "",
        "",
        "",
    )
    assert (contract.conId, contract.secType, contract.exchange) == (800001, "FUT", "CME")


async def test_qualify_by_con_id_prefers_smart_routing(
    service: BaseService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [contract_details(listed_on("NASDAQ"), validExchanges="SMART,NASDAQ,NYSE")]
    )
    contract = await service.qualify(ContractSpec(con_id=265598))
    assert contract.exchange == "SMART"


async def test_qualify_by_con_id_keeps_explicit_fields(
    service: BaseService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(future())])
    await service.qualify(
        ContractSpec(con_id=800001, sec_type="FUT", exchange="CME", include_expired=True)
    )
    request = sent_contract(fake_ib)
    assert (request.secType, request.exchange, request.includeExpired) == ("FUT", "CME", True)


async def test_qualify_carries_include_expired(service: BaseService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(future())])
    contract = await service.qualify(
        ContractSpec(
            symbol="ES",
            sec_type="FUT",
            exchange="CME",
            last_trade_date_or_contract_month="202403",
            include_expired=True,
        )
    )
    assert sent_contract(fake_ib).includeExpired is True
    assert contract.includeExpired is True


async def test_qualify_by_sec_id(service: BaseService, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    contract = await service.qualify(ContractSpec(sec_id_type="ISIN", sec_id="US0378331005"))
    request = sent_contract(fake_ib)
    assert (request.secIdType, request.secId) == ("ISIN", "US0378331005")
    assert contract.conId == 265598


async def test_qualify_returns_combos_as_built(service: BaseService, fake_ib: MagicMock) -> None:
    spec = ContractSpec(
        symbol="SPY",
        sec_type="BAG",
        combo_legs=[ComboLegSpec(con_id=1, action="BUY"), ComboLegSpec(con_id=2, action="SELL")],
    )
    contract = await service.qualify(spec)
    assert isinstance(contract, Bag)
    assert [leg.conId for leg in contract.comboLegs] == [1, 2]
    fake_ib.reqContractDetailsAsync.assert_not_called()
    with pytest.raises(InvalidRequestError, match="each leg"):
        await service.qualify_details(spec)


async def test_qualify_many_keeps_input_order(service: BaseService, fake_ib: MagicMock) -> None:
    by_symbol = {"AAPL": stock(), "MSFT": stock("MSFT", con_id=272093)}

    async def details(contract: Contract) -> list[ContractDetails]:
        await asyncio.sleep(0.01 if contract.symbol == "AAPL" else 0)
        return [contract_details(by_symbol[contract.symbol])]

    fake_ib.reqContractDetailsAsync.side_effect = details
    contracts = await service.qualify_many(
        [ContractSpec(symbol="AAPL"), ContractSpec(symbol="MSFT")]
    )
    assert [c.conId for c in contracts] == [265598, 272093]
    assert await service.qualify_many([]) == []


async def test_qualify_many_raises_the_first_failure(
    service: BaseService, fake_ib: MagicMock
) -> None:
    async def details(contract: Contract) -> list[ContractDetails]:
        if contract.symbol == "NOPE":
            raise RequestError(9, 200, "No security definition")
        return [contract_details(stock())]

    fake_ib.reqContractDetailsAsync.side_effect = details
    with pytest.raises(NotFoundError, match="NOPE"):
        await service.qualify_many([ContractSpec(symbol="AAPL"), ContractSpec(symbol="NOPE")])
    assert fake_ib.reqContractDetailsAsync.call_count == 2


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (ContractSpec(con_id=265598, symbol="AAPL"), "con_id 265598"),
        (ContractSpec(symbol="AAPL", primary_exchange="NASDAQ"), "AAPL STK SMART/NASDAQ USD"),
        (
            ContractSpec(
                symbol="SPX",
                sec_type="OPT",
                last_trade_date_or_contract_month="20261218",
                strike=5000.5,
                right="P",
                trading_class="SPXW",
                exchange="CBOE",
            ),
            "SPX OPT 20261218 5000.5 P class SPXW CBOE USD",
        ),
        (
            ContractSpec(sec_id_type="ISIN", sec_id="US0378331005", exchange=""),
            "ISIN US0378331005 STK USD",
        ),
        (ContractSpec(sec_type="BOND", issuer_id="e1234567"), "issuer e1234567 BOND SMART USD"),
    ],
)
def test_describe_spec(spec: ContractSpec, expected: str) -> None:
    assert describe_spec(spec) == expected


# --- subscriptions ---------------------------------------------------------------------------


class _Snap(BaseModel):
    price: float


async def test_subscribe_opens_once_and_reports_duplicates(
    service: BaseService, gateway: Gateway
) -> None:
    opened: list[str] = []

    def opener() -> Stream:
        opened.append("x")
        return Stream(cancel=MagicMock(), snapshot=lambda: _Snap(price=1.5))

    contract = contract_to_out(stock())
    first = await service._subscribe(
        "quotes", "265598", opener=opener, contract=contract, meta={"ticks": "233"}
    )
    assert first.deduplicated is False
    assert (first.kind, first.key, first.contract) == ("quotes", "265598", contract)
    assert first.idle_ttl_s == gateway.settings.subscription_idle_ttl
    assert gateway.subscriptions.get(first.subscription_id).meta["ticks"] == "233"

    second = await service._subscribe("quotes", "265598", opener=opener)
    assert second.deduplicated is True
    assert second.subscription_id == first.subscription_id
    assert second.contract == contract  # from the stored entry
    assert opened == ["x"]


async def test_subscribe_accepts_async_openers(service: BaseService) -> None:
    async def opener() -> Stream:
        return Stream(cancel=MagicMock(), snapshot=lambda: _Snap(price=1.0))

    out = await service._subscribe("news_bulletins", "all", opener=opener)
    assert out.contract is None
    assert out.deduplicated is False


async def test_subscribe_respects_the_cap(settings_factory: Any, fake_ib: MagicMock) -> None:
    gw = Gateway(settings_factory(max_subscriptions=1), ib_factory=lambda: fake_ib)
    await gw.start()
    try:
        service = BaseService(gw)
        stream = Stream(cancel=MagicMock(), snapshot=lambda: _Snap(price=1.0))
        await service._subscribe("quotes", "1", opener=lambda: stream)
        opener = MagicMock(return_value=stream)
        with pytest.raises(SubscriptionLimitError):
            await service._subscribe("quotes", "2", opener=opener)
        opener.assert_not_called()
    finally:
        await gw.stop()


async def test_subscription_data(service: BaseService, gateway: Gateway) -> None:
    price = {"value": 1.5}
    stream = Stream(cancel=MagicMock(), snapshot=lambda: _Snap(price=price["value"]))
    handle = await service._subscribe("quotes", "265598", opener=lambda: stream)

    first = service._subscription_data(handle.subscription_id)
    assert (first.subscription_id, first.kind, first.stale) == (
        handle.subscription_id,
        "quotes",
        False,
    )
    assert first.data == {"price": 1.5}
    assert first.last_read_at is None
    assert first.created_at == handle.created_at

    price["value"] = 2.0
    gateway.subscriptions.mark_all_stale()
    second = service._subscription_data(handle.subscription_id)
    assert second.data == {"price": 2.0}
    assert second.stale is True
    assert second.last_read_at is not None  # the first read

    with pytest.raises(SubscriptionNotFoundError):
        service._subscription_data("quotes-999")
