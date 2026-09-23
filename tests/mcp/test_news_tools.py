"""The news toolset through an in-memory MCP client, with structured output validated."""

import itertools
from unittest.mock import MagicMock

import pytest
from ib_async import NewsBulletin

from ib_gateway_mcp.models.common import SubscriptionOut
from ib_gateway_mcp.models.news import (
    NewsArticleOut,
    NewsBulletinSnapshot,
    NewsHeadlineList,
    NewsProviderList,
    NewsSnapshot,
)
from ib_gateway_mcp.services import news as news_module
from tests.conftest import McpClientFactory
from tests.fakes import FIXED_TIME, contract_details, historical_news, news_article, returns, stock
from tests.unit.test_news_service import PROVIDERS

NEWS_TOOLS = {
    "get_news_providers",
    "get_historical_news",
    "get_news_article",
    "subscribe_news_bulletins",
    "subscribe_news",
}


def error_text(result: object) -> str:
    return result.content[0].text  # type: ignore[attr-defined,no-any-return]


@pytest.fixture(autouse=True)
def ib(fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(news_module, "_OPEN_CHECK_SECONDS", 0.02)
    fake_ib.reqNewsProvidersAsync.side_effect = returns(PROVIDERS)
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.client.getReqId.side_effect = itertools.count(9000).__next__
    return fake_ib


async def test_news_tools_are_listed(mcp_client: McpClientFactory) -> None:
    async with mcp_client() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= NEWS_TOOLS
    for name in NEWS_TOOLS:
        tool = tools[name]
        assert tool.description
        assert tool.output_schema is not None
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    assert tools["get_historical_news"].input_schema["required"] == ["contract"]
    article = tools["get_news_article"].input_schema
    assert article["required"] == ["provider_code", "article_id"]
    assert article["properties"]["max_chars"]["default"] == 20000
    assert "required" not in tools["subscribe_news"].input_schema


async def test_get_news_providers(mcp_client: McpClientFactory, ib: MagicMock) -> None:
    async with mcp_client() as client:
        result = await client.call_tool("get_news_providers", {})
        ib.reqNewsProvidersAsync.side_effect = returns([])
        none = await client.call_tool("get_news_providers", {})
    assert not result.is_error
    providers = NewsProviderList.model_validate(result.structured_content)
    assert [p.code for p in providers.providers] == ["BRFG", "DJNL"]
    assert none.is_error
    assert "not_found: This login has no news providers" in error_text(none)


async def test_get_historical_news(mcp_client: McpClientFactory, ib: MagicMock) -> None:
    ib.reqHistoricalNewsAsync.side_effect = returns([historical_news("Results are in")])
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_historical_news",
            {
                "contract": {"symbol": "AAPL"},
                "provider_codes": ["BRFG"],
                "start": "2026-01-01T00:00:00Z",
                "limit": 5,
            },
        )
        unknown = await client.call_tool(
            "get_historical_news", {"contract": {"symbol": "AAPL"}, "provider_codes": ["BZ"]}
        )
    assert not result.is_error
    news = NewsHeadlineList.model_validate(result.structured_content)
    assert [h.headline for h in news.headlines] == ["Results are in"]
    assert news.headlines[0].time == FIXED_TIME
    assert news.provider_codes == ["BRFG"]
    assert ib.reqHistoricalNewsAsync.call_args.args[:2] == (265598, "BRFG")
    assert ib.reqHistoricalNewsAsync.call_args.args[4] == 6
    assert unknown.is_error
    assert "invalid_request: Not a subscribed news provider: BZ" in error_text(unknown)


async def test_get_news_article(mcp_client: McpClientFactory, ib: MagicMock) -> None:
    ib.reqNewsArticleAsync.side_effect = returns(news_article("<p>Hello <i>world</i></p>" * 50))
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_news_article", {"provider_code": "BRFG", "article_id": "BRFG$1", "max_chars": 100}
        )
        too_small = await client.call_tool(
            "get_news_article", {"provider_code": "BRFG", "article_id": "BRFG$1", "max_chars": 5}
        )
    assert not result.is_error
    article = NewsArticleOut.model_validate(result.structured_content)
    assert article.text is not None
    assert article.text.startswith("Hello world")
    assert len(article.text) == 100
    assert article.truncated is True
    assert article.format == "text"
    assert too_small.is_error


async def test_subscribe_news_bulletins(mcp_client: McpClientFactory, ib: MagicMock) -> None:
    async with mcp_client() as client:
        result = await client.call_tool("subscribe_news_bulletins", {"all_messages": True})
        ib.newsBulletinEvent.emit(NewsBulletin(7, 2, "Exchange down", "ISLAND"))
        handle = SubscriptionOut.model_validate(result.structured_content)
        assert mcp_client.gateway is not None
        data = mcp_client.gateway.news._subscription_data(handle.subscription_id).data
    assert not result.is_error
    assert (handle.kind, handle.key) == ("news_bulletins", "bulletins")
    ib.reqNewsBulletins.assert_called_once_with(True)
    snapshot = NewsBulletinSnapshot.model_validate(data)
    assert [(b.msg_id, b.type, b.exchange) for b in snapshot.bulletins] == [
        (7, "exchange_unavailable", "ISLAND")
    ]


async def test_subscribe_news(mcp_client: McpClientFactory, ib: MagicMock) -> None:
    async with mcp_client() as client:
        per_contract = await client.call_tool(
            "subscribe_news", {"contract": {"con_id": 265598}, "provider_code": "BRFG"}
        )
        tape = await client.call_tool("subscribe_news", {"provider_code": "BZ"})
        neither = await client.call_tool("subscribe_news", {})
        ib.wrapper.tickNews(9001, 1_767_367_800_000, "BZ", "BZ$1", "Tape story", "")
        assert mcp_client.gateway is not None
        handle = SubscriptionOut.model_validate(tape.structured_content)
        data = mcp_client.gateway.news._subscription_data(handle.subscription_id).data
    assert not per_contract.is_error
    contract_handle = SubscriptionOut.model_validate(per_contract.structured_content)
    assert contract_handle.key == "265598:BRFG"
    assert contract_handle.contract is not None
    assert contract_handle.contract.symbol == "AAPL"
    assert handle.key == "BZ:BZ_ALL"
    assert [h.headline for h in NewsSnapshot.model_validate(data).headlines] == ["Tape story"]
    assert neither.is_error
    assert "invalid_request: Give a contract" in error_text(neither)
