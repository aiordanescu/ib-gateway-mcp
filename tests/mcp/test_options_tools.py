"""The options toolset through an in-memory MCP client, with structured output validated."""

import asyncio
import math
from collections.abc import Collection
from typing import Any
from unittest.mock import MagicMock

import pytest
from ib_async import Contract, ContractDetails, OptionChain, Ticker
from ib_async.wrapper import RequestError

from ib_gateway_mcp.models.options import ImpliedVolatilityOut, OptionPriceOut, OptionQuoteList
from tests.conftest import McpClientFactory
from tests.fakes import contract_details, option, option_computation, returns, stock, ticker

OPTIONS_TOOLS = {"calculate_implied_volatility", "calculate_option_price", "get_option_quotes"}
EXPIRY = "20261218"
OPTION_SPEC = {
    "symbol": "AAPL",
    "sec_type": "OPT",
    "last_trade_date_or_contract_month": EXPIRY,
    "strike": 200,
    "right": "C",
}


def text_of(result: Any) -> str:
    return str(result.content[0].text)


def answer_calculations(fake_ib: MagicMock, answer: object | None) -> None:
    """Answer option calculations as ib_async's wrapper does: the service sends them with
    ``client.send`` and awaits ``wrapper.startReq``'s future; None never answers."""
    fake_ib.client.getReqId.return_value = 42
    futures: list[asyncio.Future[Any]] = []

    def start_req(_key: object, _contract: object = None, _container: object = None) -> Any:
        futures.append(asyncio.get_running_loop().create_future())
        return futures[-1]

    def send(*_fields: object, makeEmpty: bool = True) -> None:
        if answer is not None:
            futures[-1].set_result(answer)

    fake_ib.wrapper.startReq.side_effect = start_req
    fake_ib.client.send.side_effect = send


def serve_option_market(fake_ib: MagicMock, *, unlisted: Collection[float] = ()) -> None:
    """Answer the underlying, its chain, every listed option and their snapshots."""

    async def details(contract: Contract) -> list[ContractDetails]:
        if contract.secType != "OPT":
            return [contract_details(stock())]
        if contract.strike in unlisted:
            raise RequestError(1, 200, "No security definition has been found for the request")
        con_id = 700000 + int(contract.strike) * 2 + (contract.right == "P")
        found = option(strike=contract.strike, right=contract.right, con_id=con_id)
        found.tradingClass = contract.tradingClass
        return [contract_details(found)]

    async def tickers(*contracts: Contract, regulatorySnapshot: bool = False) -> list[Ticker]:
        return [
            ticker(c, modelGreeks=option_computation(), volume=math.nan)
            if c.secType == "OPT"
            else ticker(c, bid=201.0, ask=202.0)
            for c in contracts
        ]

    fake_ib.reqContractDetailsAsync.side_effect = details
    fake_ib.reqSecDefOptParamsAsync.side_effect = returns(
        [
            OptionChain(
                "SMART",
                "265598",  # type: ignore[arg-type]
                "AAPL",
                "100",
                [EXPIRY],
                [190.0, 195.0, 200.0, 205.0, 210.0],
            )
        ]
    )
    fake_ib.reqTickersAsync.side_effect = tickers


async def test_options_tools_are_listed_with_schemas(mcp_client: McpClientFactory) -> None:
    async with mcp_client() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= OPTIONS_TOOLS
    for name in OPTIONS_TOOLS:
        tool = tools[name]
        assert tool.description
        assert tool.output_schema is not None
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    quotes = tools["get_option_quotes"].input_schema
    assert sorted(quotes["required"]) == ["expiration", "underlying"]
    assert "OPRA" in (tools["get_option_quotes"].description or "")
    volatility = tools["calculate_option_price"].input_schema["properties"]["volatility"]
    assert "decimal" in volatility["description"]


async def test_calculate_implied_volatility(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    answer_calculations(fake_ib, option_computation(gamma=math.nan))
    async with mcp_client() as client:
        result = await client.call_tool(
            "calculate_implied_volatility",
            {"contract": OPTION_SPEC, "option_price": 12.5, "underlying_price": 200},
        )
    assert not result.is_error
    out = ImpliedVolatilityOut.model_validate(result.structured_content)
    assert out.implied_vol == 0.25
    assert out.greeks.gamma is None
    assert out.contract.con_id == 700001


async def test_calculate_implied_volatility_refuses_a_stock(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    async with mcp_client() as client:
        result = await client.call_tool(
            "calculate_implied_volatility",
            {"contract": {"symbol": "AAPL"}, "option_price": 1, "underlying_price": 200},
        )
    assert result.is_error
    assert "invalid_request:" in text_of(result)
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_calculate_option_price(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    answer_calculations(fake_ib, option_computation(optPrice=11.8))
    async with mcp_client() as client:
        result = await client.call_tool(
            "calculate_option_price",
            {"contract": {"con_id": 700001}, "volatility": 0.3, "underlying_price": 200},
        )
    assert not result.is_error
    out = OptionPriceOut.model_validate(result.structured_content)
    assert (out.option_price, out.volatility) == (11.8, 0.3)
    assert fake_ib.client.send.call_args.args[:3] == (55, 3, 42)  # calculateOptionPrice
    assert fake_ib.client.send.call_args.args[4:] == (0.3, 200, "")


async def test_calculate_option_price_rejects_percent_volatility(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    async with mcp_client() as client:
        result = await client.call_tool(
            "calculate_option_price",
            {"contract": OPTION_SPEC, "volatility": 25, "underlying_price": 200},
        )
    assert result.is_error
    assert "volatility" in text_of(result)
    fake_ib.client.send.assert_not_called()


async def test_calculate_option_price_times_out(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(option())])
    answer_calculations(fake_ib, None)
    async with mcp_client() as client:
        result = await client.call_tool(
            "calculate_option_price",
            {"contract": OPTION_SPEC, "volatility": 0.3, "underlying_price": 200},
        )
    assert result.is_error
    assert "request_timeout:" in text_of(result)


async def test_get_option_quotes_around_the_money(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    serve_option_market(fake_ib, unlisted={195.0})
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_option_quotes",
            {
                "underlying": {"symbol": "AAPL"},
                "expiration": EXPIRY,
                "right": "call",
                "strikes_around_atm": 3,
            },
        )
    assert not result.is_error, text_of(result)
    out = OptionQuoteList.model_validate(result.structured_content)
    assert out.underlying_price == 201.5
    assert [(leg.contract.strike, leg.contract.right) for leg in out.legs] == [
        (200.0, "C"),
        (205.0, "C"),
    ]
    assert [(s.strike, s.right) for s in out.skipped] == [(195.0, "C")]
    assert out.legs[0].greeks is not None
    assert out.legs[0].volume is None
    assert (out.total, out.truncated, out.market_data_type) == (3, False, "live")


async def test_get_option_quotes_by_range_with_limit(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    serve_option_market(fake_ib)
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_option_quotes",
            {
                "underlying": {"symbol": "AAPL"},
                "expiration": "2026-12-18",
                "strike_min": 195,
                "strike_max": 210,
                "limit": 3,
            },
        )
    out = OptionQuoteList.model_validate(result.structured_content)
    assert len(out.legs) == 3
    assert (out.total, out.truncated) == (8, True)
    assert out.underlying_quote is None


async def test_get_option_quotes_unlisted_expiration(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    serve_option_market(fake_ib)
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_option_quotes", {"underlying": {"symbol": "AAPL"}, "expiration": "20261204"}
        )
    assert result.is_error
    assert "not_found:" in text_of(result)
    assert EXPIRY in text_of(result)


async def test_get_option_quotes_without_option_data(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    serve_option_market(fake_ib)

    async def refuse_options(*contracts: Contract, regulatorySnapshot: bool = False) -> Any:
        if contracts[0].secType == "OPT":
            raise RequestError(5, 354, "Requested market data is not subscribed.")
        return [ticker(contracts[0], bid=201.0, ask=202.0)]

    fake_ib.reqTickersAsync.side_effect = refuse_options
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_option_quotes", {"underlying": {"symbol": "AAPL"}, "expiration": EXPIRY}
        )
    assert result.is_error
    assert "ib_api_error:" in text_of(result)
    assert "OPRA" in text_of(result)


@pytest.mark.parametrize(
    "arguments",
    [
        {"strike_min": 210, "strike_max": 200},
        {"strike_min": 200, "strikes_around_atm": 2},
    ],
)
async def test_get_option_quotes_rejects_bad_strikes(
    mcp_client: McpClientFactory, fake_ib: MagicMock, arguments: dict[str, Any]
) -> None:
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_option_quotes",
            {"underlying": {"symbol": "AAPL"}, "expiration": EXPIRY, **arguments},
        )
    assert result.is_error
    assert "invalid_request:" in text_of(result)
