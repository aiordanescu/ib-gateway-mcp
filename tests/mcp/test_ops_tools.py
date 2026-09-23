"""The ops toolset through an in-memory MCP client, with structured output validated."""

from unittest.mock import MagicMock

import pytest

from ib_gateway_mcp.models.ops import (
    AccountList,
    ConnectionInfo,
    ConnectionState,
    HealthReport,
    ServerTime,
    UserInfo,
)
from tests.conftest import McpClientFactory
from tests.fakes import FIXED_TIME, PAPER_ACCOUNT, go_offline, pending, returns

OPS_TOOLS = {
    "get_health",
    "get_server_time",
    "get_connection_info",
    "list_accounts",
    "get_user_info",
}


async def test_ops_tools_are_listed_with_schemas(mcp_client: McpClientFactory) -> None:
    async with mcp_client() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= OPS_TOOLS
    for name in OPS_TOOLS:
        tool = tools[name]
        assert tool.description
        assert tool.output_schema is not None
        expected = {"probe"} if name == "get_health" else set()
        assert set(tool.input_schema.get("properties", {})) == expected
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True


async def test_get_health(mcp_client: McpClientFactory) -> None:
    async with mcp_client() as client:
        result = await client.call_tool("get_health", {})
    assert not result.is_error
    health = HealthReport.model_validate(result.structured_content)
    assert health.state is ConnectionState.CONNECTED
    assert health.accounts == [PAPER_ACCOUNT]
    assert health.is_paper is True


async def test_get_health_reports_breaker_data_type_and_subscriptions(
    mcp_client: McpClientFactory,
) -> None:
    async with mcp_client(breaker_rejects=2, max_subscriptions=7) as client:
        gateway = mcp_client.gateway
        assert gateway is not None
        gateway.safety.breaker.record_rejection("rejected once")
        gateway.safety.breaker.record_rejection("rejected twice")
        result = await client.call_tool("get_health", {})
    health = HealthReport.model_validate(result.structured_content)
    assert health.circuit_open is True
    assert health.circuit_rejections == 2
    assert health.circuit_threshold == 2
    assert health.market_data_type == "live"
    assert health.subscriptions_used == 0
    assert health.subscriptions_max == 7
    assert health.probe is None


async def test_get_health_probe_round_trip(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqCurrentTimeAsync.side_effect = returns(FIXED_TIME)
    async with mcp_client() as client:
        result = await client.call_tool("get_health", {"probe": True})
    assert not result.is_error
    probe = HealthReport.model_validate(result.structured_content).probe
    assert probe is not None
    assert probe.ok is True
    assert probe.server_time == FIXED_TIME
    assert probe.round_trip_ms is not None
    assert probe.error is None


async def test_get_health_probe_failure_is_reported_not_raised(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqCurrentTimeAsync.side_effect = pending()
    async with mcp_client(request_timeout=0.05) as client:
        result = await client.call_tool("get_health", {"probe": True})
    assert not result.is_error
    health = HealthReport.model_validate(result.structured_content)
    assert health.probe is not None
    assert health.probe.ok is False
    assert health.probe.error is not None
    assert health.probe.error.startswith("request_timeout:")


async def test_get_health_probe_while_disconnected(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    go_offline(fake_ib)
    async with mcp_client() as client:
        result = await client.call_tool("get_health", {"probe": True})
    assert not result.is_error
    probe = HealthReport.model_validate(result.structured_content).probe
    assert probe is not None
    assert probe.ok is False
    assert (probe.error or "").startswith("not_connected:")


async def test_get_health_when_the_gateway_is_down(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    go_offline(fake_ib)
    async with mcp_client() as client:
        result = await client.call_tool("get_health", {})
    assert not result.is_error
    health = HealthReport.model_validate(result.structured_content)
    assert health.state is not ConnectionState.CONNECTED
    assert health.hint


async def test_get_server_time(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqCurrentTimeAsync.side_effect = returns(FIXED_TIME)
    async with mcp_client() as client:
        result = await client.call_tool("get_server_time", {})
    assert not result.is_error
    assert ServerTime.model_validate(result.structured_content).server_time == FIXED_TIME


async def test_get_server_time_reports_not_connected(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    go_offline(fake_ib)
    async with mcp_client() as client:
        result = await client.call_tool("get_server_time", {})
    assert result.is_error
    assert "not_connected:" in result.content[0].text  # type: ignore[union-attr]


async def test_get_connection_info(mcp_client: McpClientFactory) -> None:
    async with mcp_client() as client:
        result = await client.call_tool("get_connection_info", {})
    info = ConnectionInfo.model_validate(result.structured_content)
    assert info.connected is True
    assert info.server_version == 178


async def test_list_accounts(mcp_client: McpClientFactory) -> None:
    async with mcp_client() as client:
        result = await client.call_tool("list_accounts", {})
    accounts = AccountList.model_validate(result.structured_content)
    assert accounts.default_account == PAPER_ACCOUNT
    assert [a.account for a in accounts.accounts] == [PAPER_ACCOUNT]


@pytest.mark.parametrize(("raw", "expected"), [("brand-id", "brand-id"), ([], None)])
async def test_get_user_info(
    mcp_client: McpClientFactory, fake_ib: MagicMock, raw: object, expected: str | None
) -> None:
    fake_ib.reqUserInfoAsync.side_effect = returns(raw)
    async with mcp_client() as client:
        result = await client.call_tool("get_user_info", {})
    assert UserInfo.model_validate(result.structured_content).white_branding_id == expected
