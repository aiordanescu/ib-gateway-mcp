"""The trading gate end to end: what reaches the wire when orders are not allowed.

The fake gateway never executes orders; these tests only check that the safety paths
hold against real ib_async traffic: a gateway whose API is read-only (warning 321)
disables trading at once, and a live login without IBKR_MCP_ALLOW_LIVE never sends an
order-related message at all.
"""

from __future__ import annotations

import asyncio

import pytest

from ib_gateway_mcp.connection import HINT_READ_ONLY_API
from ib_gateway_mcp.errors import ConfigurationError, IbApiError, LiveTradingDisabledError
from ib_gateway_mcp.models.common import ContractSpec
from ib_gateway_mcp.models.ops import ConnectionState
from ib_gateway_mcp.models.orders import OrderSpec
from tests.e2e.conftest import GatewayFactory
from tests.e2e.fake_tws import READ_ONLY_API, FakeTws

LIVE_ACCOUNT = "U1234567"
"""Placeholder live account id."""

_PLACE_ORDER = 3
_ORDER_MESSAGES = {"3", "4", "5", "16", "58", "99"}
"""placeOrder, cancelOrder, reqOpenOrders, reqAllOpenOrders, reqGlobalCancel,
reqCompletedOrders."""


def _market_buy() -> OrderSpec:
    return OrderSpec(
        contract=ContractSpec(symbol="AAPL"), action="BUY", quantity=1, order_type="MKT"
    )


async def test_read_only_api_fails_the_what_if_fast_and_disables_trading(
    gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    fake_tws.read_only_api = True
    gw = gateway_factory(profile="trading")
    await gw.start()
    await gw.wait_connected(timeout=3)
    assert gw.health().trading_enabled is True  # paper login, trading profile

    started = asyncio.get_running_loop().time()
    with pytest.raises(IbApiError) as caught:
        await gw.orders.preview_order(_market_buy())
    # Warning 321 never completes an ib_async request; the service must not wait for the
    # 1 s request timeout.
    assert asyncio.get_running_loop().time() - started < 0.5
    assert caught.value.error_code == READ_ONLY_API
    (what_if,) = fake_tws.current.requests(_PLACE_ORDER)
    assert int(what_if[1]) == caught.value.req_id

    health = gw.health()
    assert health.state is ConnectionState.CONNECTED
    assert health.api_read_only is True
    assert health.trading_enabled is False
    assert health.last_error is not None
    assert health.last_error.code == READ_ONLY_API

    # From now on the gate refuses before anything is sent.
    with pytest.raises(ConfigurationError) as refused:
        await gw.orders.preview_order(_market_buy())
    assert str(refused.value) == HINT_READ_ONLY_API
    assert len(fake_tws.current.requests(_PLACE_ORDER)) == 1


async def test_live_login_without_allow_live_sends_no_order_traffic(
    gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    fake_tws.accounts = [LIVE_ACCOUNT]
    gw = gateway_factory(profile="trading")
    await gw.start()
    await gw.wait_connected(timeout=3)

    health = gw.health()
    assert health.state is ConnectionState.CONNECTED
    assert health.accounts == [LIVE_ACCOUNT]
    assert health.is_paper is False
    assert health.trading_enabled is False
    assert health.orders_synced is False

    with pytest.raises(LiveTradingDisabledError, match="IBKR_MCP_ALLOW_LIVE"):
        await gw.orders.preview_order(_market_buy())
    # Read-only tools keep working on the same session.
    await gw.ops.server_time()
    await gw.contracts.qualify_contract(ContractSpec(symbol="AAPL"))

    sent = {fields[0] for session in fake_tws.connections for fields in session.received}
    assert not sent & _ORDER_MESSAGES
