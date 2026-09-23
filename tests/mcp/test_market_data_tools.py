"""The market_data toolset through an in-memory MCP client, with structured output validated."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from ib_async import BarDataList, Contract, RealTimeBarList, TickAttribLast, TickByTickAllLast
from ib_async.wrapper import RequestError

from ib_gateway_mcp.models.common import SubscriptionDataOut, SubscriptionOut
from ib_gateway_mcp.models.market_data import (
    LiveBarsData,
    MarketDataTypeOut,
    QuoteList,
    QuoteStreamData,
    RealtimeBarsData,
    SubscriptionList,
    TickByTickData,
    UnsubscribeResult,
)
from ib_gateway_mcp.services.market_data import MarketDataService
from tests.conftest import McpClientFactory
from tests.fakes import FIXED_TIME, bar, contract_details, raises, returns, stock, ticker

MARKET_DATA_TOOLS = {
    "get_quotes",
    "set_market_data_type",
    "subscribe_quotes",
    "subscribe_market_depth",
    "subscribe_tick_by_tick",
    "subscribe_realtime_bars",
    "subscribe_bars",
    "list_subscriptions",
    "get_subscription_data",
    "unsubscribe",
}
SESSION_TOOLS = {"set_market_data_type", "unsubscribe"}
"""Not read-only: they change state that every client of the server shares."""
AAPL = {"symbol": "AAPL"}


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MarketDataService, "settle_seconds", 0.01)


@pytest.fixture(autouse=True)
def _known_contracts(fake_ib: MagicMock) -> None:
    aapl = stock()

    async def details(request: Contract, *_args: Any, **_kwargs: Any) -> Any:
        if request.symbol == "AAPL" or request.conId == aapl.conId:
            return [contract_details(aapl)]
        raise RequestError(9, 200, "No security definition has been found for the request")

    fake_ib.reqContractDetailsAsync.side_effect = details


def text(result: Any) -> str:
    return str(result.content[0].text)


async def test_market_data_tools_are_listed_with_schemas(mcp_client: McpClientFactory) -> None:
    async with mcp_client(toolsets=["market_data"]) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= MARKET_DATA_TOOLS
    for name in MARKET_DATA_TOOLS:
        tool = tools[name]
        assert tool.description
        assert tool.output_schema is not None
        assert tool.annotations is not None
        # Switching the session's data type changes what the notional check sees, and
        # unsubscribe can stop streams other clients of the server read.
        assert tool.annotations.read_only_hint is (name not in SESSION_TOOLS)
    for name in SESSION_TOOLS:
        assert tools[name].annotations.destructive_hint is False  # type: ignore[union-attr]
    quotes_schema = tools["get_quotes"].input_schema["properties"]["contracts"]
    assert (quotes_schema["minItems"], quotes_schema["maxItems"]) == (1, 25)
    assert "COSTS MONEY" in str(tools["get_quotes"].input_schema["properties"])


async def test_get_quotes(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqTickersAsync.side_effect = returns([ticker(stock())])
    async with mcp_client() as client:
        refused = await client.call_tool(
            "get_quotes", {"contracts": [AAPL], "regulatory_snapshot": True}
        )
    assert refused.is_error
    assert "IBKR_MCP_ALLOW_REGULATORY_SNAPSHOTS" in text(refused)
    fake_ib.reqTickersAsync.assert_not_called()
    async with mcp_client(allow_regulatory_snapshots=True) as client:
        result = await client.call_tool(
            "get_quotes", {"contracts": [AAPL, {"symbol": "NOPE"}], "regulatory_snapshot": True}
        )
    assert not result.is_error, text(result)
    quotes = QuoteList.model_validate(result.structured_content)
    assert quotes.quotes[0].bid == 99.5
    assert quotes.errors[0].code == "not_found"
    assert quotes.regulatory_snapshots == 1
    assert any("USD 0.01" in notice for notice in quotes.notices)


async def test_get_quotes_error_text(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqTickersAsync.side_effect = raises(
        RequestError(5, 10168, "Requested market data is not subscribed.")
    )
    async with mcp_client() as client:
        result = await client.call_tool("get_quotes", {"contracts": [AAPL]})
        too_many = await client.call_tool("get_quotes", {"contracts": [AAPL] * 26})
    assert result.is_error
    assert "ib_api_error: IB error 10168" in text(result)
    assert "set_market_data_type" in text(result)
    assert too_many.is_error


async def test_set_market_data_type(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    async with mcp_client() as client:
        result = await client.call_tool("set_market_data_type", {"data_type": "delayed"})
        bad = await client.call_tool("set_market_data_type", {"data_type": "realtime"})
        depth = await client.call_tool("subscribe_market_depth", {"contract": AAPL})
    out = MarketDataTypeOut.model_validate(result.structured_content)
    assert (out.data_type, out.code, out.previous) == ("delayed", 3, "live")
    fake_ib.reqMarketDataType.assert_called_with(3)
    assert bad.is_error
    assert depth.is_error
    assert "invalid_request: Market depth needs live market data" in text(depth)


async def test_quote_stream_lifecycle(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    live = ticker(stock())
    fake_ib.reqMktData.return_value = live
    async with mcp_client() as client:
        sub = await client.call_tool(
            "subscribe_quotes", {"contract": AAPL, "generic_ticks": ["shortable"]}
        )
        assert not sub.is_error, text(sub)
        handle = SubscriptionOut.model_validate(sub.structured_content)
        live.shortableShares = 1234.0
        data = await client.call_tool(
            "get_subscription_data", {"subscription_id": handle.subscription_id}
        )
        listing = await client.call_tool("list_subscriptions", {})
        gone = await client.call_tool("unsubscribe", {"subscription_id": handle.subscription_id})
        after = await client.call_tool(
            "get_subscription_data", {"subscription_id": handle.subscription_id}
        )

    assert (handle.kind, handle.key) == ("quotes", "265598")
    assert fake_ib.reqMktData.call_args.args[1] == "236"
    out = SubscriptionDataOut.model_validate(data.structured_content)
    quote = QuoteStreamData.model_validate(out.data)
    assert quote.quote.ask == 100.5
    assert quote.extras is not None
    assert quote.extras.shortable_shares == 1234.0
    subs = SubscriptionList.model_validate(listing.structured_content)
    assert subs.used == 1
    assert subs.subscriptions[0].params == {"generic_ticks": ["shortable"]}
    cancelled = UnsubscribeResult.model_validate(gone.structured_content)
    assert cancelled.cancelled[0].subscription_id == handle.subscription_id
    fake_ib.cancelMktData.assert_called_once()
    assert after.is_error
    assert "subscription_not_found:" in text(after)


async def test_subscribe_market_depth(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqMktDepth.return_value = ticker(stock())
    async with mcp_client() as client:
        result = await client.call_tool(
            "subscribe_market_depth", {"contract": AAPL, "rows": 5, "smart_depth": True}
        )
        too_deep = await client.call_tool("subscribe_market_depth", {"contract": AAPL, "rows": 99})
    handle = SubscriptionOut.model_validate(result.structured_content)
    assert (handle.kind, handle.deduplicated) == ("depth", False)
    assert fake_ib.reqMktDepth.call_args.kwargs == {"numRows": 5, "isSmartDepth": True}
    assert too_deep.is_error


async def test_subscribe_tick_by_tick(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    tape = ticker(stock())
    fake_ib.reqTickByTickData.return_value = tape
    async with mcp_client() as client:
        sub = await client.call_tool(
            "subscribe_tick_by_tick", {"contract": AAPL, "tick_type": "Last", "buffer_size": 10}
        )
        handle = SubscriptionOut.model_validate(sub.structured_content)
        tape.tickByTicks = [
            TickByTickAllLast(1, FIXED_TIME, 100.0 + i, 1.0, TickAttribLast(), "NYSE", "")
            for i in range(3)
        ]
        tape.updateEvent.emit(tape)
        data = await client.call_tool(
            "get_subscription_data", {"subscription_id": handle.subscription_id, "limit": 2}
        )
    out = SubscriptionDataOut.model_validate(data.structured_content)
    ticks = TickByTickData.model_validate(out.data)
    assert [t.price for t in ticks.ticks] == [101.0, 102.0]  # type: ignore[union-attr]
    assert ticks.truncated is True
    assert ticks.received == 3


async def test_subscribe_realtime_bars(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    source = RealTimeBarList()
    source.reqId = 11
    fake_ib.reqRealTimeBars.return_value = source
    async with mcp_client() as client:
        sub = await client.call_tool(
            "subscribe_realtime_bars", {"contract": AAPL, "what_to_show": "BID"}
        )
        handle = SubscriptionOut.model_validate(sub.structured_content)
        data = await client.call_tool(
            "get_subscription_data", {"subscription_id": handle.subscription_id}
        )
    assert handle.key == "265598:BID:all"
    bars = RealtimeBarsData.model_validate(data.structured_content["data"])
    assert (bars.what_to_show, bars.buffer_size, bars.bars) == ("BID", 720, [])


async def test_subscribe_bars(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    source = BarDataList([bar(FIXED_TIME, 100.0)])
    source.reqId = 12
    fake_ib.reqHistoricalDataAsync.side_effect = returns(source)
    async with mcp_client() as client:
        sub = await client.call_tool(
            "subscribe_bars", {"contract": AAPL, "bar_size": "1 hour", "duration": "2 D"}
        )
        assert not sub.is_error, text(sub)
        handle = SubscriptionOut.model_validate(sub.structured_content)
        data = await client.call_tool(
            "get_subscription_data", {"subscription_id": handle.subscription_id}
        )
        bad = await client.call_tool("subscribe_bars", {"contract": AAPL, "bar_size": "1 secs"})
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    bars = LiveBarsData.model_validate(data.structured_content["data"])
    assert [b.close for b in bars.bars] == [100.0]
    assert bad.is_error  # the schema itself leaves out 1 secs
    assert "bar_size" in text(bad)
    assert "1 secs" not in str(tools["subscribe_bars"].input_schema["properties"]["bar_size"])


async def test_unsubscribe_all_and_argument_errors(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqMktData.return_value = ticker(stock())
    async with mcp_client() as client:
        await client.call_tool("subscribe_quotes", {"contract": AAPL})
        neither = await client.call_tool("unsubscribe", {})
        everything = await client.call_tool("unsubscribe", {"all": True})
        listing = await client.call_tool("list_subscriptions", {})
    assert neither.is_error
    assert "invalid_request:" in text(neither)
    result = UnsubscribeResult.model_validate(everything.structured_content)
    assert [c.kind for c in result.cancelled] == ["quotes"]
    assert SubscriptionList.model_validate(listing.structured_content).used == 0
