"""The contracts toolset through an in-memory MCP client, with structured output validated."""

from typing import Any
from unittest.mock import MagicMock

from ib_async import (
    ContractDescription,
    DepthMktDataDescription,
    OptionChain,
    PriceIncrement,
    SmartComponent,
)
from ib_async.wrapper import RequestError
from mcp_types import CallToolResult

from ib_gateway_mcp.models.common import ContractOut
from ib_gateway_mcp.models.contracts import (
    ContractDetailsList,
    DepthExchangeList,
    MarketRuleList,
    OptionChainList,
    SmartComponentList,
    SymbolSearchResult,
)
from tests.conftest import McpClientFactory
from tests.fakes import contract_details, go_offline, raises, returns, stock

CONTRACTS_TOOLS = {
    "search_symbols",
    "get_contract_details",
    "qualify_contract",
    "get_option_chain",
    "get_market_rule",
    "get_smart_components",
    "get_depth_exchanges",
}


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    return result.content[0].text  # type: ignore[union-attr]


async def call(
    mcp_client: McpClientFactory, tool: str, arguments: dict[str, Any]
) -> CallToolResult:
    async with mcp_client() as client:
        return await client.call_tool(tool, arguments)


async def test_contracts_tools_are_listed_read_only(mcp_client: McpClientFactory) -> None:
    async with mcp_client() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= CONTRACTS_TOOLS
    for name in CONTRACTS_TOOLS:
        tool = tools[name]
        assert tool.description
        assert tool.output_schema is not None
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    properties = tools["get_market_rule"].input_schema["properties"]["market_rule_ids"]
    assert properties["maxItems"] == 20
    assert "get_contract_details" in properties["description"]
    assert set(tools["get_option_chain"].input_schema["properties"]) == {
        "underlying",
        "exchange",
        "fut_fop_exchange",
    }


async def test_search_symbols(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqMatchingSymbolsAsync.side_effect = returns(
        [ContractDescription(contract=stock(description="APPLE INC"), derivativeSecTypes=["OPT"])]
    )
    result = await call(mcp_client, "search_symbols", {"pattern": "apple", "limit": 5})
    assert not result.is_error
    found = SymbolSearchResult.model_validate(result.structured_content)
    assert [m.contract.symbol for m in found.matches] == ["AAPL"]
    assert found.matches[0].contract.description == "APPLE INC"
    assert found.matches[0].derivative_sec_types == ["OPT"]


async def test_search_symbols_not_found(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqMatchingSymbolsAsync.side_effect = returns([])
    result = await call(mcp_client, "search_symbols", {"pattern": "zzzq"})
    assert "not_found: No instrument matches 'zzzq'" in error_text(result)


async def test_get_contract_details(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [
            contract_details(
                stock(),
                orderTypes="LMT,MKT",
                marketRuleIds="26,26,26",
                tradingHours="20260105:0400-20260105:2000;20260106:CLOSED",
                liquidHours="20260105:0930-20260105:1600",
            )
        ]
    )
    result = await call(mcp_client, "get_contract_details", {"contract": {"symbol": "AAPL"}})
    assert not result.is_error
    details = ContractDetailsList.model_validate(result.structured_content)
    assert (details.total, details.truncated) == (1, False)
    row = details.contracts[0]
    assert row.contract.description == "APPLE INC"
    assert row.order_types == ["LMT", "MKT"]
    assert row.market_rule_ids == [26, 26, 26]
    assert row.liquid_sessions is not None
    assert row.liquid_sessions[0].start.isoformat() == "2026-01-05T09:30:00-05:00"
    assert result.structured_content is not None
    assert result.structured_content["contracts"][0]["closed_dates"] == ["2026-01-06"]


async def test_get_contract_details_not_found(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(RequestError(4, 200, "No security"))
    result = await call(mcp_client, "get_contract_details", {"contract": {"symbol": "NOPE"}})
    assert "not_found: No contract matches NOPE STK SMART USD" in error_text(result)


async def test_get_contract_details_rejects_bad_specs(mcp_client: McpClientFactory) -> None:
    result = await call(mcp_client, "get_contract_details", {"contract": {"currency": "USD"}})
    assert "con_id, symbol" in error_text(result)


async def test_qualify_contract(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    result = await call(mcp_client, "qualify_contract", {"contract": {"symbol": "AAPL"}})
    assert not result.is_error
    out = ContractOut.model_validate(result.structured_content)
    assert (out.con_id, out.exchange, out.description) == (265598, "SMART", "APPLE INC")


async def test_qualify_contract_ambiguous_lists_candidates(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [
            contract_details(stock(con_id=11)),
            contract_details(stock(con_id=12, primaryExchange="NYSE")),
        ]
    )
    result = await call(mcp_client, "qualify_contract", {"contract": {"symbol": "AAPL"}})
    text = error_text(result)
    assert "ambiguous_contract: AAPL STK SMART USD matches 2 contracts" in text
    assert "con_id 11" in text
    assert "con_id 12" in text


async def test_qualify_contract_when_disconnected(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    go_offline(fake_ib)
    result = await call(mcp_client, "qualify_contract", {"contract": {"con_id": 265598}})
    assert "not_connected:" in error_text(result)


async def test_get_option_chain(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.reqSecDefOptParamsAsync.side_effect = returns(
        [
            OptionChain("SMART", 265598, "AAPL", "100", ["20261218"], [200.0, 190.0]),
            OptionChain("CBOE", 265598, "AAPL", "100", ["20261218"], [190.0, 200.0]),
        ]
    )
    result = await call(
        mcp_client, "get_option_chain", {"underlying": {"symbol": "AAPL"}, "exchange": None}
    )
    assert not result.is_error
    chains = OptionChainList.model_validate(result.structured_content)
    assert chains.underlying.con_id == 265598
    assert [(c.exchanges, c.strikes) for c in chains.chains] == [
        (["CBOE", "SMART"], [190.0, 200.0])
    ]


async def test_get_option_chain_refuses_an_option(mcp_client: McpClientFactory) -> None:
    result = await call(
        mcp_client, "get_option_chain", {"underlying": {"symbol": "AAPL", "sec_type": "OPT"}}
    )
    assert "invalid_request: underlying must be" in error_text(result)


async def test_get_market_rule(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    async def answer(rule_id: int) -> list[PriceIncrement] | None:
        return [PriceIncrement(0.0, 0.01), PriceIncrement(1.0, 0.05)] if rule_id == 26 else None

    fake_ib.reqMarketRuleAsync.side_effect = answer
    result = await call(mcp_client, "get_market_rule", {"market_rule_ids": [26, 4242]})
    assert not result.is_error
    rules = MarketRuleList.model_validate(result.structured_content)
    assert [(i.low_edge, i.increment) for i in rules.rules[0].increments] == [
        (0.0, 0.01),
        (1.0, 0.05),
    ]
    assert rules.missing_ids == [4242]


async def test_get_market_rule_validates_input(mcp_client: McpClientFactory) -> None:
    async with mcp_client() as client:
        too_many = await client.call_tool("get_market_rule", {"market_rule_ids": list(range(21))})
        empty = await client.call_tool("get_market_rule", {"market_rule_ids": []})
    assert too_many.is_error
    assert empty.is_error


async def test_get_smart_components(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqSmartComponentsAsync.side_effect = returns([SmartComponent(1, "NYSE", "N")])
    result = await call(mcp_client, "get_smart_components", {"bbo_exchange": "9c0001"})
    assert not result.is_error
    components = SmartComponentList.model_validate(result.structured_content)
    assert [(c.exchange, c.exchange_letter) for c in components.components] == [("NYSE", "N")]


async def test_get_smart_components_closed_exchanges(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqSmartComponentsAsync.side_effect = returns([])
    result = await call(mcp_client, "get_smart_components", {"bbo_exchange": "9c0001"})
    assert "not_found: IBKR returned no exchanges" in error_text(result)


async def test_get_depth_exchanges(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqMktDepthExchangesAsync.side_effect = returns(
        [DepthMktDataDescription("NASDAQ", "STK", "NASDAQ", "Deep2", 1)]
    )
    async with mcp_client() as client:
        first = await client.call_tool("get_depth_exchanges", {})
        second = await client.call_tool("get_depth_exchanges", {})
    assert not first.is_error
    exchanges = DepthExchangeList.model_validate(first.structured_content)
    assert [(e.exchange, e.service_data_type) for e in exchanges.exchanges] == [("NASDAQ", "Deep2")]
    assert second.structured_content == first.structured_content
    assert fake_ib.reqMktDepthExchangesAsync.call_count == 1


async def test_get_depth_exchanges_api_error(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqMktDepthExchangesAsync.side_effect = raises(RequestError(-1, 504, "Not connected"))
    result = await call(mcp_client, "get_depth_exchanges", {})
    assert "ib_api_error: IB error 504" in error_text(result)
