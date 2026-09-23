"""The MCP server end to end: in-memory MCP client, real server lifespan, real ib_async.

Here nothing is injected: :func:`build_server` gets only Settings, so its lifespan builds
and starts the Gateway exactly as ``ib-gateway-mcp`` does in production, and every tool
call travels MCP client -> server -> service -> ib_async -> socket -> fake gateway.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from mcp import Client
from mcp_types import CallToolResult

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.mcp.server import build_server
from ib_gateway_mcp.models.common import ContractOut
from ib_gateway_mcp.models.contracts import ContractDetailsList
from ib_gateway_mcp.models.ops import (
    AccountList,
    ConnectionInfo,
    ConnectionState,
    HealthReport,
    ServerTime,
)
from tests.e2e.fake_tws import AAPL, PAPER_ACCOUNT, SERVER_VERSION, FakeTws


async def _call(client: Client, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    result = await client.call_tool(tool, arguments or {})
    assert not result.is_error, _text(result)
    return result.structured_content


def _text(result: CallToolResult) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


async def _health_until(
    client: Client, state: ConnectionState, timeout: float = 3.0
) -> HealthReport:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        health = HealthReport.model_validate(await _call(client, "get_health"))
        if health.state is state:
            return health
        if loop.time() > deadline:
            raise AssertionError(f"gateway stayed {health.state.value}, expected {state.value}")
        await asyncio.sleep(0.01)


async def test_tools_over_mcp_against_the_fake_gateway(
    e2e_settings: Callable[..., Settings], fake_tws: FakeTws
) -> None:
    server = build_server(e2e_settings())
    async with Client(server) as client:
        health = await _health_until(client, ConnectionState.CONNECTED)
        assert health.accounts == [PAPER_ACCOUNT]
        assert health.server_version == SERVER_VERSION
        assert fake_tws.current.client_id == 80

        probed = HealthReport.model_validate(await _call(client, "get_health", {"probe": True}))
        assert probed.probe is not None
        assert probed.probe.ok is True

        server_time = ServerTime.model_validate(await _call(client, "get_server_time"))
        assert abs(server_time.skew_seconds) < 5

        accounts = AccountList.model_validate(await _call(client, "list_accounts"))
        assert accounts.default_account == PAPER_ACCOUNT

        info = ConnectionInfo.model_validate(await _call(client, "get_connection_info"))
        assert info.connected is True
        assert info.port == fake_tws.port

        qualified = ContractOut.model_validate(
            await _call(client, "qualify_contract", {"contract": {"symbol": "AAPL"}})
        )
        assert qualified.con_id == AAPL.con_id
        assert qualified.description == "APPLE INC"

        details = ContractDetailsList.model_validate(
            await _call(client, "get_contract_details", {"contract": {"con_id": AAPL.con_id}})
        )
        assert details.total == 1
        assert details.contracts[0].contract.primary_exchange == "NASDAQ"

        missing = await client.call_tool("qualify_contract", {"contract": {"symbol": "NOPE"}})
        assert missing.is_error
        assert "not_found: No contract matches NOPE" in _text(missing)

    # The lifespan stopped the gateway: the fake saw the socket close.
    await asyncio.sleep(0.05)
    assert not fake_tws.sessions


async def test_gateway_outage_is_reported_over_mcp(
    e2e_settings: Callable[..., Settings], fake_tws: FakeTws
) -> None:
    fake_tws.mode = "accept_close"  # the relay is up, the gateway behind it is not
    server = build_server(e2e_settings(connect_timeout=5.0))
    async with Client(server) as client:
        down = await _health_until(client, ConnectionState.NOT_ACCEPTING)
        assert down.hint is not None
        assert "accepted the connection and closed it" in down.hint

        refused = await client.call_tool("get_server_time", {})
        assert refused.is_error
        assert "not_connected: Not connected to the gateway" in _text(refused)

        fake_tws.mode = "normal"
        await _health_until(client, ConnectionState.CONNECTED)
        fake_tws.emit_error(1100)
        lost = await _health_until(client, ConnectionState.CONNECTIVITY_LOST)
        assert lost.last_error is not None
        assert lost.last_error.code == 1100
