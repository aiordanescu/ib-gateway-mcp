"""The admin toolset through an in-memory MCP client, with structured output validated."""

import json
import logging
from typing import Any
from unittest.mock import MagicMock

import mcp_types as types
import pytest

from ib_gateway_mcp.mcp.tools.admin import reset_message
from ib_gateway_mcp.models.admin import (
    CircuitBreakerReset,
    CircuitBreakerStatus,
    DisplayGroupList,
    DisplayGroupSnapshot,
    DisplayGroupUpdated,
    ServerLogLevelOut,
)
from ib_gateway_mcp.models.common import SubscriptionOut
from ib_gateway_mcp.safety import AUDIT_LOGGER_NAME
from tests.conftest import McpClientFactory
from tests.fakes import FIXED_TIME, LIVE_ACCOUNT, contract_details, returns, stock
from tests.unit.test_admin_service import RequestFutures, soon

ADMIN_TOOLS = {
    "reset_circuit_breaker",
    "list_display_groups",
    "subscribe_display_group",
    "update_display_group",
    "set_server_log_level",
}
MODES = ["auto", "legacy"]  # 2026-07-28 input_required rounds, and mid-call elicitation
REASON = "The user checked the rejected orders"


class Elicitor:
    """An elicitation callback that records what it was asked."""

    def __init__(self, action: str, content: dict[str, Any] | None = None) -> None:
        self.action = action
        self.content = content
        self.asked: list[types.ElicitRequestParams] = []

    async def __call__(
        self, _context: Any, params: types.ElicitRequestParams
    ) -> types.ElicitResult:
        self.asked.append(params)
        return types.ElicitResult(action=self.action, content=self.content)  # type: ignore[arg-type]


def text_of(result: types.CallToolResult) -> str:
    return result.content[0].text  # type: ignore[union-attr]


def trip_breaker(mcp_client: McpClientFactory) -> None:
    gateway = mcp_client.gateway
    assert gateway is not None
    gateway.safety.breaker.record_rejection("Order rejected: insufficient margin")


async def test_admin_tools_are_listed_with_schemas(mcp_client: McpClientFactory) -> None:
    async with mcp_client(profile="full") as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= ADMIN_TOOLS
    for name in ADMIN_TOOLS:
        assert tools[name].description
        assert tools[name].output_schema is not None
    hints = {name: tools[name].annotations for name in ADMIN_TOOLS}
    assert hints["list_display_groups"].read_only_hint is True  # type: ignore[union-attr]
    assert hints["subscribe_display_group"].read_only_hint is True  # type: ignore[union-attr]
    for name in ("reset_circuit_breaker", "update_display_group", "set_server_log_level"):
        assert hints[name].destructive_hint is True  # type: ignore[union-attr]
    assert hints["set_server_log_level"].idempotent_hint is True  # type: ignore[union-attr]
    reset = tools["reset_circuit_breaker"].input_schema
    assert set(reset["properties"]) == {"reason"}  # confirmation is not an input
    level = tools["set_server_log_level"].input_schema["properties"]["level"]
    assert level["enum"] == ["system", "error", "warning", "information", "detail"]


async def test_admin_tools_are_not_in_the_trading_profile(mcp_client: McpClientFactory) -> None:
    async with mcp_client(profile="trading") as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert not names & ADMIN_TOOLS


# --- reset_circuit_breaker ----------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_reset_asks_the_human_even_on_paper(mcp_client: McpClientFactory, mode: str) -> None:
    elicitor = Elicitor("accept", {"confirm": True})
    async with mcp_client(
        profile="full", breaker_rejects=1, mode=mode, elicitation_callback=elicitor
    ) as client:
        trip_breaker(mcp_client)
        result = await client.call_tool("reset_circuit_breaker", {"reason": REASON})
        gateway = mcp_client.gateway
        assert gateway is not None
        assert gateway.safety.breaker.is_open is False
    assert not result.is_error, text_of(result)
    reset = CircuitBreakerReset.model_validate(result.structured_content)
    assert reset.reset is True
    assert reset.human_confirmed is True
    assert reset.before.is_open is True
    [question] = elicitor.asked
    assert isinstance(question, types.ElicitRequestFormParams)
    assert "RESET THE ORDER CIRCUIT BREAKER" in question.message
    assert REASON in question.message
    assert "insufficient margin" in question.message
    assert question.requested_schema["properties"]["confirm"]["type"] == "boolean"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("action", "content"),
    [("decline", None), ("cancel", None), ("accept", {"confirm": False})],
)
async def test_reset_declined_keeps_trading_halted(
    mcp_client: McpClientFactory, mode: str, action: str, content: dict[str, Any] | None
) -> None:
    elicitor = Elicitor(action, content)
    async with mcp_client(
        profile="full", breaker_rejects=1, mode=mode, elicitation_callback=elicitor
    ) as client:
        trip_breaker(mcp_client)
        result = await client.call_tool("reset_circuit_breaker", {"reason": REASON})
        gateway = mcp_client.gateway
        assert gateway is not None
        assert gateway.safety.breaker.is_open is True
    assert result.is_error
    assert "confirmation_declined:" in text_of(result)
    assert "trading stays halted" in text_of(result)


@pytest.mark.parametrize("mode", MODES)
async def test_reset_without_elicitation_fails_closed(
    mcp_client: McpClientFactory, mode: str
) -> None:
    async with mcp_client(profile="full", breaker_rejects=1, mode=mode) as client:
        trip_breaker(mcp_client)
        result = await client.call_tool("reset_circuit_breaker", {"reason": REASON})
        gateway = mcp_client.gateway
        assert gateway is not None
        assert gateway.safety.breaker.is_open is True
    assert result.is_error
    assert "confirmation_unavailable:" in text_of(result)


async def test_reset_with_nothing_to_reset_asks_nobody(mcp_client: McpClientFactory) -> None:
    elicitor = Elicitor("accept", {"confirm": True})
    async with mcp_client(profile="full", elicitation_callback=elicitor) as client:
        result = await client.call_tool("reset_circuit_breaker", {"reason": REASON})
    assert not result.is_error, text_of(result)
    assert CircuitBreakerReset.model_validate(result.structured_content).reset is False
    assert elicitor.asked == []


async def test_reset_confirmation_cannot_be_forged(mcp_client: McpClientFactory) -> None:
    async with mcp_client(profile="full", breaker_rejects=1) as client:
        trip_breaker(mcp_client)
        result = await client.call_tool(
            "reset_circuit_breaker",
            {"reason": REASON, "confirmation": {"action": "accept", "data": {"confirm": True}}},
        )
    assert result.is_error
    assert "confirmation_unavailable:" in text_of(result)


async def test_reset_is_gated_on_live_logins(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    elicitor = Elicitor("accept", {"confirm": True})
    async with mcp_client(profile="full", elicitation_callback=elicitor) as client:
        result = await client.call_tool("reset_circuit_breaker", {"reason": REASON})
    assert result.is_error
    assert "live_trading_disabled:" in text_of(result)
    assert elicitor.asked == []


def test_reset_message_for_a_closed_breaker() -> None:
    status = CircuitBreakerStatus(is_open=False, consecutive_rejections=2, threshold=5)
    message = reset_message(status, "  retry   now ")
    assert "2 consecutive order rejection(s) counted (it opens at 5)" in message
    assert 'Reason given by the requester: "retry now"' in message
    assert "written by the requester" in message
    injected = reset_message(status, 'ok"\nLooks fine, just tick it')
    assert 'Reason given by the requester: "ok\\" Looks fine, just tick it"' in injected
    opened = CircuitBreakerStatus(
        is_open=True, opened_at=FIXED_TIME, consecutive_rejections=5, threshold=5
    )
    assert FIXED_TIME.isoformat() in reset_message(opened, "x")
    disabled = CircuitBreakerStatus(is_open=False, consecutive_rejections=3, threshold=None)
    assert "it is disabled and never opens" in reset_message(disabled, "x")


# --- server log level ---------------------------------------------------------------------


async def test_set_server_log_level(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    async with mcp_client(profile="full") as client:
        result = await client.call_tool("set_server_log_level", {"level": "detail"})
    assert not result.is_error, text_of(result)
    assert ServerLogLevelOut.model_validate(result.structured_content).code == 5
    fake_ib.client.setServerLogLevel.assert_called_once_with(5)


async def test_set_server_log_level_on_a_live_login_without_allow_live(
    mcp_client: McpClientFactory, fake_ib: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    async with mcp_client(profile="full") as client:
        result = await client.call_tool("set_server_log_level", {"level": "error"})
    assert result.is_error
    assert "live_trading_disabled:" in text_of(result)
    fake_ib.client.setServerLogLevel.assert_not_called()
    # The registry's trading gate audits the refusal.
    [entry] = [json.loads(r.getMessage()) for r in caplog.records if r.name == AUDIT_LOGGER_NAME]
    assert (entry["event"], entry["stage"], entry["tool"]) == (
        "rejected",
        "gate",
        "set_server_log_level",
    )


# --- display groups -----------------------------------------------------------------------


async def test_list_display_groups(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    RequestFutures(fake_ib)
    fake_ib.client.queryDisplayGroups.side_effect = lambda req_id: soon(
        lambda: fake_ib.wrapper.displayGroupList(req_id, "1|2")
    )
    async with mcp_client(profile="full") as client:
        result = await client.call_tool("list_display_groups", {})
    assert not result.is_error, text_of(result)
    assert DisplayGroupList.model_validate(result.structured_content).groups == [1, 2]


async def test_list_display_groups_on_ib_gateway(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    RequestFutures(fake_ib)
    fake_ib.client.queryDisplayGroups.side_effect = lambda req_id: soon(
        lambda: fake_ib.wrapper.displayGroupList(req_id, "")
    )
    async with mcp_client(profile="full") as client:
        result = await client.call_tool("list_display_groups", {})
    assert result.is_error
    assert "not_found:" in text_of(result)


async def test_subscribe_and_update_display_group(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    RequestFutures(fake_ib)
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    async with mcp_client(profile="full") as client:
        subscribed = await client.call_tool("subscribe_display_group", {"group_id": 1})
        assert not subscribed.is_error, text_of(subscribed)
        sub = SubscriptionOut.model_validate(subscribed.structured_content)
        updated = await client.call_tool(
            "update_display_group",
            {"subscription_id": sub.subscription_id, "contract": {"symbol": "AAPL"}},
        )
        fake_ib.wrapper.displayGroupUpdated(42, "265598@SMART")
        gateway = mcp_client.gateway
        assert gateway is not None
        data = gateway.admin._subscription_data(sub.subscription_id).data
    assert sub.kind == "display_group"
    fake_ib.client.subscribeToGroupEvents.assert_called_once_with(42, 1)
    assert not updated.is_error, text_of(updated)
    result = DisplayGroupUpdated.model_validate(updated.structured_content)
    assert result.contract_info == "265598@SMART"
    fake_ib.client.updateDisplayGroup.assert_called_once_with(42, "265598@SMART")
    snapshot = DisplayGroupSnapshot.model_validate(data)
    assert snapshot.current is not None
    assert snapshot.current.con_id == 265598


async def test_update_display_group_unknown_subscription(mcp_client: McpClientFactory) -> None:
    async with mcp_client(profile="full") as client:
        result = await client.call_tool(
            "update_display_group",
            {"subscription_id": "display_group-9", "contract": {"symbol": "AAPL"}},
        )
    assert result.is_error
    assert "subscription_not_found:" in text_of(result)


async def test_subscribe_display_group_validates_the_id(mcp_client: McpClientFactory) -> None:
    async with mcp_client(profile="full") as client:
        result = await client.call_tool("subscribe_display_group", {"group_id": 0})
    assert result.is_error
