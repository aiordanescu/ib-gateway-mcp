"""The connection lifecycle end to end: real ib_async against the fake gateway.

Covers the handshake and startup sync, health and accounts, the server clock, and each
way a gateway goes wrong in practice: the socket drops, IBKR connectivity is lost and
restored (1100/1102), the client id is taken (326), a relay accepts and closes (the
ib-gateway-docker socat case), the port is closed, the gateway never finishes the
handshake, or it hangs with the socket open.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ib_gateway_mcp import connection as connection_module
from ib_gateway_mcp.connection import (
    CURRENT_TIME_INTERVAL,
    HINT_DROPPED,
    HINT_HANDSHAKE_TIMEOUT,
    HINT_PEER_CLOSED,
    HINT_REFUSED,
)
from ib_gateway_mcp.errors import NotConnectedError
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.common import ContractSpec
from ib_gateway_mcp.models.ops import ConnectionState
from tests.e2e.conftest import (
    FAST_CURRENT_TIME_INTERVAL,
    FAST_CURRENT_TIME_WINDOW,
    GatewayFactory,
    eventually,
)
from tests.e2e.fake_tws import CURRENT_TIME_WINDOW, PAPER_ACCOUNT, SERVER_VERSION, FakeTws

_REQ_ACCOUNT_SUMMARY = 62
_REQ_MARKET_DATA_TYPE = 59
_REQ_POSITIONS = 61
_REQ_ACCOUNT_UPDATES = 6
_REQ_EXECUTIONS = 7
_REQ_OPEN_ORDERS = 5
_REQ_CURRENT_TIME = 49


# --- connect ----------------------------------------------------------------------------


async def test_connect_reports_health_and_accounts(gateway: Gateway, fake_tws: FakeTws) -> None:
    health = gateway.health()
    assert health.state is ConnectionState.CONNECTED
    assert health.hint is None
    assert health.server_version == SERVER_VERSION
    assert health.port == fake_tws.port
    assert health.client_id == 80
    assert health.accounts == [PAPER_ACCOUNT]
    assert health.is_paper is True
    assert health.connected_since is not None
    # The data farm notices (2104, 2106, 2158) are informational, not errors.
    assert health.last_error is None
    assert health.api_read_only is False
    assert health.trading_enabled is False  # readonly profile
    assert health.orders_synced is False

    session = fake_tws.current
    assert session.client_id == 80
    sent = {int(fields[0]) for fields in session.received}
    # ib_async's startup sync, then the market data type the manager applies.
    assert {_REQ_POSITIONS, _REQ_ACCOUNT_UPDATES, _REQ_EXECUTIONS, _REQ_MARKET_DATA_TYPE} <= sent
    # A readonly session skips the order sync.
    assert _REQ_OPEN_ORDERS not in sent

    accounts = gateway.ops.list_accounts()
    assert [info.account for info in accounts.accounts] == [PAPER_ACCOUNT]
    assert accounts.default_account == PAPER_ACCOUNT

    info = gateway.ops.connection_info()
    assert info.connected is True
    assert info.server_version == SERVER_VERSION
    assert info.messages_received is not None
    assert info.messages_received > 0


@pytest.mark.parametrize("server_version", [166, 176])
async def test_connect_negotiates_older_server_versions(
    server_version: int, gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    fake_tws.server_version = server_version
    gw = gateway_factory()
    await gw.start()
    await gw.wait_connected(timeout=3)
    assert gw.health().server_version == server_version
    contract = await gw.contracts.qualify_contract(_aapl())
    assert contract.con_id == 265598


async def test_trading_profile_syncs_orders_on_a_paper_login(
    gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    gw = gateway_factory(profile="trading")
    await gw.start()
    await gw.wait_connected(timeout=3)
    health = gw.health()
    assert health.trading_enabled is True
    assert health.orders_synced is True
    assert fake_tws.current.requests(_REQ_OPEN_ORDERS)


async def test_server_time_round_trip(gateway: Gateway) -> None:
    answer = await gateway.ops.server_time()
    now = datetime.now(UTC)
    assert answer.server_time.tzinfo is not None
    assert abs((now - answer.server_time).total_seconds()) < 5
    assert abs(answer.skew_seconds) < 5

    report = await gateway.ops.health_report(probe=True)
    assert report.probe is not None
    assert report.probe.ok is True
    assert report.probe.round_trip_ms is not None


async def test_probe_round_trip_leaves_out_the_spacing_wait(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe right after another clock request waits its turn; that wait is not latency."""
    spacing = 0.5
    monkeypatch.setattr(connection_module, "CURRENT_TIME_INTERVAL", spacing)
    await gateway.ops.server_time()
    started = asyncio.get_running_loop().time()
    report = await gateway.ops.health_report(probe=True)
    assert asyncio.get_running_loop().time() - started >= spacing * 0.9  # it did wait
    assert report.probe is not None
    assert report.probe.ok is True
    assert report.probe.round_trip_ms is not None
    assert report.probe.round_trip_ms < spacing * 1000 / 2


async def test_the_fake_ignores_a_server_time_request_sent_too_soon(
    gateway: Gateway, fake_tws: FakeTws
) -> None:
    """IB Gateway ignores a reqCurrentTime within its window after an answer, silently."""
    ib = gateway.connection.ib
    assert await asyncio.wait_for(ib.reqCurrentTimeAsync(), 1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(ib.reqCurrentTimeAsync(), 0.3)
    assert fake_tws.current.ignored_current_time == 1


async def test_back_to_back_server_time_requests_all_get_answers(
    gateway_factory: GatewayFactory, fake_tws: FakeTws, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a server-time request right after a health probe used to go unanswered.

    With the gateway's real one-second window and the production spacing, the request
    waits its turn instead of timing out; concurrent ones queue behind it.
    """
    monkeypatch.setattr(connection_module, "CURRENT_TIME_INTERVAL", CURRENT_TIME_INTERVAL)
    fake_tws.current_time_window = CURRENT_TIME_WINDOW
    gw = gateway_factory(request_timeout=3.0)
    await gw.start()
    await gw.wait_connected(timeout=3)
    session = await fake_tws.wait_for_ready()

    report = await gw.ops.health_report(probe=True)
    assert report.probe is not None
    assert report.probe.ok, report.probe.error
    answer = await gw.ops.server_time()
    assert abs(answer.skew_seconds) < 5

    monkeypatch.setattr(connection_module, "CURRENT_TIME_INTERVAL", FAST_CURRENT_TIME_INTERVAL)
    fake_tws.current_time_window = FAST_CURRENT_TIME_WINDOW
    answers = await asyncio.gather(*(gw.ops.server_time() for _ in range(3)))
    assert all(abs(each.skew_seconds) < 5 for each in answers)
    assert len(session.requests(_REQ_CURRENT_TIME)) == 5
    assert session.ignored_current_time == 0


# --- drops and reconnects ---------------------------------------------------------------


async def test_dropped_connection_goes_down_then_reconnects(
    gateway: Gateway, fake_tws: FakeTws, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Leave a visible window between the drop and the retry.
    monkeypatch.setattr(connection_module, "INITIAL_BACKOFF", 0.3)
    first_since = gateway.health().connected_since
    gateway.connection.set_market_data_type(3)  # delayed; must survive the reconnect
    await eventually(lambda: ["59", "1", "3"] in fake_tws.current.received)

    fake_tws.drop()
    await eventually(lambda: gateway.health().state is ConnectionState.NOT_CONNECTED)
    down = gateway.health()
    assert down.hint == HINT_DROPPED
    assert down.connected_since is None
    with pytest.raises(NotConnectedError, match="dropped"):
        await gateway.ops.server_time()

    await fake_tws.wait_for_ready(count=2)
    await gateway.wait_connected(timeout=3)
    up = gateway.health()
    assert up.state is ConnectionState.CONNECTED
    assert up.accounts == [PAPER_ACCOUNT]
    assert up.connected_since is not None
    assert first_since is not None
    assert up.connected_since > first_since
    answer = await gateway.ops.server_time()
    assert abs(answer.skew_seconds) < 5
    # The market data type is re-applied on the new session.
    assert fake_tws.current.requests(_REQ_MARKET_DATA_TYPE)[-1] == ["59", "1", "3"]
    assert up.market_data_type == "delayed"


async def test_request_in_flight_fails_fast_when_the_socket_drops(
    gateway: Gateway, fake_tws: FakeTws
) -> None:
    fake_tws.current.hung = True
    request = asyncio.ensure_future(gateway.ops.server_time())
    await eventually(lambda: bool(fake_tws.current.requests(_REQ_CURRENT_TIME)))
    started = asyncio.get_running_loop().time()
    fake_tws.drop()
    with pytest.raises(NotConnectedError, match="Lost the gateway connection"):
        await request
    # Well inside the 1 s request timeout: the drop, not the timeout, ended the call.
    assert asyncio.get_running_loop().time() - started < 0.5


async def test_hung_gateway_is_caught_by_the_liveness_probe(
    gateway_factory: GatewayFactory, fake_tws: FakeTws, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection_module, "LIVENESS_IDLE", 0.2)
    monkeypatch.setattr(connection_module, "LIVENESS_PROBE_TIMEOUT", 0.2)
    monkeypatch.setattr(connection_module, "INITIAL_BACKOFF", 0.3)
    gw = gateway_factory()
    await gw.start()
    await gw.wait_connected(timeout=3)
    hung = await fake_tws.wait_for_ready()
    hung.hung = True  # the socket stays open; nothing is answered

    await eventually(lambda: gw.health().state is ConnectionState.NOT_CONNECTED)
    down = gw.health()
    assert down.hint is not None
    assert "stopped answering" in down.hint
    assert down.last_error is not None
    assert down.last_error.code == -1
    assert "liveness probe" in down.last_error.message
    assert hung.requests(_REQ_CURRENT_TIME)  # the probe asked for the server time

    await fake_tws.wait_for_ready(count=2)
    await gw.wait_connected(timeout=3)
    assert hung.closed
    assert gw.health().state is ConnectionState.CONNECTED


async def test_health_probe_reports_a_hung_gateway(
    gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    gw = gateway_factory(request_timeout=0.2)
    await gw.start()
    await gw.wait_connected(timeout=3)
    (await fake_tws.wait_for_ready()).hung = True
    report = await gw.ops.health_report(probe=True)
    assert report.state is ConnectionState.CONNECTED  # the socket itself is still open
    assert report.probe is not None
    assert report.probe.ok is False
    assert report.probe.error is not None
    assert report.probe.error.startswith("request_timeout:")


# --- IBKR connectivity (1100 / 1102) -------------------------------------------------------


async def test_connectivity_lost_then_restored(gateway: Gateway, fake_tws: FakeTws) -> None:
    fake_tws.emit_error(1100)
    await eventually(lambda: gateway.health().state is ConnectionState.CONNECTIVITY_LOST)
    health = gateway.health()
    assert health.hint is not None
    assert "lost its connection to IBKR" in health.hint
    assert "1100" in health.hint
    assert health.last_error is not None
    assert health.last_error.code == 1100
    # The API session itself is still up.
    assert gateway.connection.is_connected

    fake_tws.emit_error(1102)
    await eventually(lambda: gateway.health().state is ConnectionState.CONNECTED)
    assert gateway.health().hint is None
    # ib_async re-requests the account summary when IBKR reports 1102.
    await eventually(lambda: bool(fake_tws.current.requests(_REQ_ACCOUNT_SUMMARY)))


async def test_connectivity_lost_reported_during_session_setup(
    gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    fake_tws.welcome_codes = [1100]  # the gateway is up but cut off from IBKR
    gw = gateway_factory()
    await gw.start()
    await gw.wait_connected(timeout=3)
    assert gw.health().state is ConnectionState.CONNECTIVITY_LOST

    fake_tws.emit_error(1102)
    await eventually(lambda: gw.health().state is ConnectionState.CONNECTED)


# --- connection refused, closed or ignored -----------------------------------------------


async def test_client_id_in_use_gives_a_clear_hint(
    gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    fake_tws.client_ids_in_use = {80}
    gw = gateway_factory(connect_timeout=5.0)
    started = asyncio.get_running_loop().time()
    await gw.start()
    assert asyncio.get_running_loop().time() - started < 1.0  # no wait for the 5 s timeout
    health = gw.health()
    assert health.state is ConnectionState.NOT_ACCEPTING
    assert health.hint is not None
    assert "Client id 80 is already in use" in health.hint
    assert "IB_CLIENT_ID" in health.hint
    assert health.last_error is not None
    assert health.last_error.code == 326

    # Once the other application lets go of the id, a retry connects and clears the error.
    fake_tws.client_ids_in_use.clear()
    await gw.wait_connected(timeout=3)
    health = gw.health()
    assert health.state is ConnectionState.CONNECTED
    assert health.last_error is None


async def test_second_server_with_the_same_client_id_is_rejected(
    gateway: Gateway, gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    second = gateway_factory()
    await second.start()
    health = second.health()
    assert health.state is ConnectionState.NOT_ACCEPTING
    assert health.hint is not None
    assert "Client id 80 is already in use" in health.hint
    # The first server keeps its session.
    assert gateway.health().state is ConnectionState.CONNECTED
    assert len(fake_tws.sessions) == 1

    # A different client id is accepted alongside.
    third = gateway_factory(ib_client_id=81)
    await third.start()
    await third.wait_connected(timeout=3)
    assert {session.client_id for session in fake_tws.sessions} == {80, 81}


async def test_accept_then_close_is_not_accepting_fast(
    gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    fake_tws.mode = "accept_close"  # a relay whose gateway is down
    gw = gateway_factory(connect_timeout=5.0)
    started = asyncio.get_running_loop().time()
    await gw.start()
    assert asyncio.get_running_loop().time() - started < 1.0  # not the 5 s connect timeout
    health = gw.health()
    assert health.state is ConnectionState.NOT_ACCEPTING
    assert health.hint == HINT_PEER_CLOSED
    assert health.last_error is not None
    assert health.last_error.code == -1

    fake_tws.mode = "normal"  # the gateway came up behind the relay
    await gw.wait_connected(timeout=3)
    assert gw.health().state is ConnectionState.CONNECTED


async def test_refused_port_then_gateway_comes_up(
    gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    await fake_tws.refuse()
    gw = gateway_factory()
    await gw.start()
    health = gw.health()
    assert health.state is ConnectionState.NOT_ACCEPTING
    assert health.hint == HINT_REFUSED
    with pytest.raises(NotConnectedError, match="refused"):
        await gw.ops.server_time()

    await fake_tws.resume()
    await gw.wait_connected(timeout=3)
    assert gw.health().state is ConnectionState.CONNECTED


async def test_handshake_never_answered_times_out(
    gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    fake_tws.mode = "silent"  # accepts, reads the greeting, never answers
    gw = gateway_factory(connect_timeout=0.3)
    await gw.start()
    health = gw.health()
    assert health.state is ConnectionState.NOT_ACCEPTING
    assert health.hint == HINT_HANDSHAKE_TIMEOUT
    assert fake_tws.connections  # the socket was accepted


def _aapl() -> ContractSpec:
    return ContractSpec(symbol="AAPL")


async def test_stop_during_a_stalled_handshake_returns_promptly(
    gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    fake_tws.mode = "silent"
    gw = gateway_factory(connect_timeout=5.0)
    await gw.start(wait=0.1)
    assert gw.health().state is ConnectionState.CONNECTING
    started = asyncio.get_running_loop().time()
    await gw.stop()
    assert asyncio.get_running_loop().time() - started < 0.5
    assert gw.health().state is ConnectionState.NOT_CONNECTED
    await eventually(lambda: all(session.closed for session in fake_tws.connections))


# --- data synced at connect, compat shims --------------------------------------------------


async def test_account_values_come_from_the_startup_sync(
    gateway: Gateway, fake_tws: FakeTws
) -> None:
    requests_before = len(fake_tws.current.received)
    values = await gateway.account.account_values(tags=["NetLiquidation"])
    assert values.account == PAPER_ACCOUNT
    assert [(row.tag, row.value, row.currency) for row in values.values] == [
        ("NetLiquidation", "100000.00", "USD")
    ]
    # Served from ib_async's cache: nothing new went to the gateway.
    assert len(fake_tws.current.received) == requests_before


async def test_user_info_returns_the_white_branding_id(gateway: Gateway, fake_tws: FakeTws) -> None:
    # ib_async 2.1.0 alone resolves reqUserInfo to []; the compat shim passes the id on.
    fake_tws.white_branding_id = "example-brand"
    info = await gateway.ops.user_info()
    assert info.white_branding_id == "example-brand"
