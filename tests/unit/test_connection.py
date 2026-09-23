"""ConnectionManager: connect, health states, reconnect, trading gate."""

from __future__ import annotations

import asyncio
import gc
import itertools
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from eventkit import Event
from ib_async import IB, ContractDetails
from ib_async.decoder import Decoder
from ib_async.ib import StartupFetch, StartupFetchALL
from ib_async.wrapper import Wrapper

from ib_gateway_mcp import connection as connection_module
from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.connection import ConnectionManager, _install_compat_shims
from ib_gateway_mcp.errors import (
    ConfigurationError,
    InvalidRequestError,
    LiveTradingDisabledError,
    NotConnectedError,
)
from ib_gateway_mcp.models.ops import ConnectionState
from tests.fakes import (
    FIXED_TIME,
    LIVE_ACCOUNT,
    PAPER_ACCOUNT,
    drop_connection,
    emit_error,
    make_fake_ib,
    pending,
    returns,
)


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "INITIAL_BACKOFF", 0.01)
    monkeypatch.setattr(connection_module, "MAX_BACKOFF", 0.02)


async def eventually(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait until ``predicate()`` holds, polling the event loop."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def failing_then_connecting(ib: MagicMock, *errors: BaseException) -> None:
    """Make ``connectAsync`` raise each error in turn, then connect normally."""
    connect = ib.connectAsync.side_effect
    remaining = list(errors)

    async def side_effect(*args: Any, **kwargs: Any) -> Any:
        if remaining:
            raise remaining.pop(0)
        return await connect(*args, **kwargs)

    ib.connectAsync.side_effect = side_effect


def manager(settings: Settings, ib: MagicMock, **kwargs: Any) -> ConnectionManager:
    return ConnectionManager(settings, lambda: ib, **kwargs)


async def test_connects_read_only_for_the_readonly_profile(
    settings: Settings, fake_ib: MagicMock
) -> None:
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        fake_ib.connectAsync.assert_awaited_once_with(
            "127.0.0.1",
            4004,
            clientId=80,
            timeout=1.0,
            readonly=True,
            account="",
            fetchFields=StartupFetchALL & ~StartupFetch.EXECUTIONS,
        )
        fake_ib.reqExecutionsAsync.assert_called_once_with()
        assert cm.state is ConnectionState.CONNECTED
        assert cm.ib is fake_ib
        assert fake_ib.RaiseRequestErrors is True
        health = cm.health()
        assert health.state is ConnectionState.CONNECTED
        assert health.hint is None
        assert health.accounts == [PAPER_ACCOUNT]
        assert health.is_paper is True
        assert health.server_version == 178
        assert health.connected_since is not None
        assert health.orders_synced is False
        assert health.trading_enabled is False
        fake_ib.reqOpenOrdersAsync.assert_not_called()
        fake_ib.setTimeout.assert_called_with(connection_module.LIVENESS_IDLE)
    finally:
        await cm.stop()
    assert cm.state is ConnectionState.NOT_CONNECTED
    assert "stopped" in (cm.health().hint or "")
    with pytest.raises(NotConnectedError, match="stopped"):
        _ = cm.ib


async def test_a_read_only_connect_loads_executions_after_the_orders(
    settings_factory: Callable[..., Settings], fake_ib: MagicMock
) -> None:
    """ib_async attaches a fill to its order only if the order is already known."""
    calls: list[str] = []

    def recorder(name: str) -> Callable[..., Any]:
        async def record(*_args: Any) -> list[Any]:
            calls.append(name)
            return []

        return record

    fake_ib.reqOpenOrdersAsync.side_effect = recorder("open orders")
    fake_ib.reqCompletedOrdersAsync.side_effect = recorder("completed orders")
    fake_ib.reqExecutionsAsync.side_effect = recorder("executions")
    cm = manager(settings_factory(profile="trading"), fake_ib)  # paper: read-only connect
    await cm.start()
    try:
        fields = fake_ib.connectAsync.call_args.kwargs["fetchFields"]
        assert not fields & StartupFetch.EXECUTIONS
        assert calls == ["open orders", "completed orders", "executions"]
    finally:
        await cm.stop()


async def test_start_is_idempotent(settings: Settings, fake_ib: MagicMock) -> None:
    cm = manager(settings, fake_ib)
    await cm.start()
    await cm.start()
    fake_ib.connectAsync.assert_awaited_once()
    await cm.stop()


def test_ib_before_start_raises(settings: Settings, fake_ib: MagicMock) -> None:
    cm = manager(settings, fake_ib)
    with pytest.raises(NotConnectedError, match="not been started"):
        _ = cm.ib
    assert cm.health().state is ConnectionState.NOT_CONNECTED


async def test_refused_connection_is_reported_and_retried(
    settings: Settings, fake_ib: MagicMock
) -> None:
    failing_then_connecting(fake_ib, ConnectionRefusedError(61, "Connection refused"))
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        health = cm.health()
        assert health.state is ConnectionState.NOT_ACCEPTING
        assert "2FA" in (health.hint or "")
        assert health.last_error is not None
        assert health.last_error.code == -1
        with pytest.raises(NotConnectedError, match="not_accepting"):
            _ = cm.ib
        await eventually(lambda: cm.state is ConnectionState.CONNECTED)
        assert fake_ib.connectAsync.await_count == 2
    finally:
        await cm.stop()


async def test_handshake_timeout_and_client_id_in_use(
    settings: Settings, fake_ib: MagicMock
) -> None:
    failing_then_connecting(fake_ib, TimeoutError(), TimeoutError())
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        assert "handshake" in (cm.health().hint or "")
        emit_error(fake_ib, 326, "Unable to connect as the client id is already in use.")
        await eventually(lambda: "IB_CLIENT_ID" in (cm.health().hint or ""))
        await eventually(lambda: cm.state is ConnectionState.CONNECTED)
    finally:
        await cm.stop()


async def test_client_id_clash_only_explains_its_own_attempt(
    settings: Settings, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = fake_ib.connectAsync.side_effect
    script = ["clash", "ok", "timeout"]

    async def side_effect(*args: Any, **kwargs: Any) -> Any:
        step = script.pop(0) if script else "timeout"
        if step == "clash":
            emit_error(fake_ib, 326, "Unable to connect as the client id is already in use.")
            raise TimeoutError
        if step == "timeout":
            raise TimeoutError
        return await connect(*args, **kwargs)

    fake_ib.connectAsync.side_effect = side_effect
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        assert "IB_CLIENT_ID" in (cm.health().hint or "")
        assert cm.health().last_error.code == 326  # type: ignore[union-attr]
        await eventually(lambda: cm.state is ConnectionState.CONNECTED)
        assert cm.health().last_error is None  # the failed attempt's error is resolved
        monkeypatch.setattr(connection_module, "INITIAL_BACKOFF", 0.5)
        monkeypatch.setattr(connection_module, "MAX_BACKOFF", 0.5)
        drop_connection(fake_ib)
        # A later outage (the weekly 2FA wait, say) must not blame the client id.
        await eventually(lambda: cm.state is ConnectionState.NOT_ACCEPTING)
        assert "handshake" in (cm.health().hint or "")
        assert "IB_CLIENT_ID" not in (cm.health().hint or "")
        assert cm.health().last_error.code == -1  # type: ignore[union-attr]
    finally:
        await cm.stop()


async def test_peer_closing_during_the_handshake_fails_fast(
    settings_factory: Callable[..., Settings], fake_ib: MagicMock
) -> None:
    """A relay in front of a stopped gateway accepts, then closes: do not wait it out."""
    socket_closed = Event("disconnected")
    fake_ib.client.conn = SimpleNamespace(disconnected=socket_closed)
    connect = fake_ib.connectAsync.side_effect
    attempts = 0

    async def half_up(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            asyncio.get_running_loop().call_later(0.01, socket_closed.emit, "")
            await asyncio.sleep(30)  # ib_async would wait the whole connect timeout
        return await connect(*args, **kwargs)

    fake_ib.connectAsync.side_effect = half_up
    cm = manager(settings_factory(connect_timeout=30), fake_ib)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await cm.start()
    try:
        assert loop.time() - started < 1
        assert cm.state is ConnectionState.NOT_ACCEPTING
        assert "closed it before the API session was ready" in (cm.health().hint or "")
        await eventually(lambda: cm.state is ConnectionState.CONNECTED)
    finally:
        await cm.stop()


async def test_start_can_return_before_the_first_attempt(
    settings: Settings, fake_ib: MagicMock
) -> None:
    fake_ib.connectAsync.side_effect = pending()
    cm = manager(settings, fake_ib)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await cm.start(wait=0)
    assert loop.time() - started < 0.1
    assert cm.state is ConnectionState.CONNECTING
    await cm.stop()


async def test_cancelling_start_does_not_leak_the_supervisor(
    settings: Settings, fake_ib: MagicMock
) -> None:
    fake_ib.connectAsync.side_effect = pending()
    cm = manager(settings, fake_ib)
    starting = asyncio.create_task(cm.start())
    await asyncio.sleep(0.01)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    await asyncio.sleep(0.01)
    running = [t for t in asyncio.all_tasks() if t.get_name() == "ib-connection"]
    assert all(t.done() for t in running)
    assert cm.state is ConnectionState.NOT_CONNECTED


async def test_short_sessions_do_not_reset_the_backoff(
    settings: Settings, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway that accepts and drops at once is retried with growing delays."""
    monkeypatch.setattr(connection_module, "INITIAL_BACKOFF", 0.01)
    monkeypatch.setattr(connection_module, "MAX_BACKOFF", 0.16)
    loop = asyncio.get_running_loop()
    connect = fake_ib.connectAsync.side_effect
    attempts: list[float] = []

    async def flapping(*args: Any, **kwargs: Any) -> Any:
        attempts.append(loop.time())
        result = await connect(*args, **kwargs)
        loop.call_soon(fake_ib.disconnect)
        return result

    fake_ib.connectAsync.side_effect = flapping
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        await eventually(lambda: len(attempts) >= 5)
    finally:
        await cm.stop()
    gaps = [later - earlier for earlier, later in itertools.pairwise(attempts)]
    assert gaps[3] > 3 * gaps[0]  # 0.01, 0.02, 0.04, 0.08: doubling, not reset each time


@pytest.mark.parametrize(
    ("error", "state", "text"),
    [
        (
            OSError(8, "nodename nor servname provided"),
            ConnectionState.NOT_ACCEPTING,
            "Cannot reach",
        ),
        (ValueError("weird"), ConnectionState.NOT_CONNECTED, "Connection failed"),
    ],
)
async def test_other_connect_failures(
    settings: Settings,
    fake_ib: MagicMock,
    error: BaseException,
    state: ConnectionState,
    text: str,
) -> None:
    failing_then_connecting(fake_ib, error)
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        assert cm.health().state is state
        assert text in (cm.health().hint or "")
    finally:
        await cm.stop()


async def test_session_setup_failure_is_retried(settings: Settings, fake_ib: MagicMock) -> None:
    fake_ib.managedAccounts.side_effect = [ConnectionError("Socket broke"), ["DU1234567"]]
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        assert "Session setup failed" in (cm.health().hint or "")
        await eventually(lambda: cm.state is ConnectionState.CONNECTED)
    finally:
        await cm.stop()


async def test_drop_reconnects_and_resubscribes(settings: Settings, fake_ib: MagicMock) -> None:
    resubscribe = AsyncMock()
    cm = manager(settings, fake_ib, on_resubscribe=resubscribe)
    await cm.start()
    try:
        resubscribe.assert_not_awaited()
        drop_connection(fake_ib)
        assert cm.state is ConnectionState.NOT_CONNECTED
        assert "dropped" in (cm.health().hint or "")
        await eventually(lambda: cm.state is ConnectionState.CONNECTED)
        await eventually(lambda: resubscribe.await_count == 1)
        assert fake_ib.connectAsync.await_count == 2
    finally:
        await cm.stop()


async def test_connectivity_codes_drive_the_state(settings: Settings, fake_ib: MagicMock) -> None:
    resubscribe = AsyncMock()
    cm = manager(settings, fake_ib, on_resubscribe=resubscribe)
    await cm.start()
    try:
        emit_error(fake_ib, 1100, "Connectivity between IB and TWS has been lost.")
        assert cm.state is ConnectionState.CONNECTIVITY_LOST
        assert "1100" in (cm.health().hint or "")
        assert cm.ib is fake_ib  # the API session itself is still up
        emit_error(fake_ib, 1102, "Connectivity restored - data maintained.")
        assert cm.state is ConnectionState.CONNECTED
        resubscribe.assert_not_awaited()

        emit_error(fake_ib, 2110, "Connectivity between TWS and server is broken.")
        assert cm.state is ConnectionState.CONNECTIVITY_LOST
        emit_error(fake_ib, 1101, "Connectivity restored - data lost.")
        assert cm.state is ConnectionState.CONNECTED
        await eventually(lambda: resubscribe.await_count == 1)
        assert cm.health().last_error is not None
        assert cm.health().last_error.code == 1101
    finally:
        await cm.stop()


async def test_not_connected_codes_close_and_reopen_the_session(
    settings: Settings, fake_ib: MagicMock
) -> None:
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        emit_error(fake_ib, 504, "Not connected")
        await eventually(lambda: fake_ib.connectAsync.await_count == 2)
        await eventually(lambda: cm.state is ConnectionState.CONNECTED)
    finally:
        await cm.stop()


async def test_connectivity_lost_during_setup_is_kept(
    settings: Settings, fake_ib: MagicMock
) -> None:
    connect = fake_ib.connectAsync.side_effect

    async def with_outage(*args: Any, **kwargs: Any) -> Any:
        emit_error(fake_ib, 1100, "Connectivity between IB and TWS has been lost.")
        return await connect(*args, **kwargs)

    fake_ib.connectAsync.side_effect = with_outage
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        assert cm.state is ConnectionState.CONNECTIVITY_LOST
        assert "1100" in (cm.health().hint or "")
        emit_error(fake_ib, 1102, "Connectivity restored - data maintained.")
        assert cm.state is ConnectionState.CONNECTED
    finally:
        await cm.stop()


async def test_idle_probe_keeps_a_healthy_session(settings: Settings, fake_ib: MagicMock) -> None:
    fake_ib.reqCurrentTimeAsync.side_effect = returns(FIXED_TIME)
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        fake_ib.setTimeout.reset_mock()
        fake_ib.timeoutEvent.emit(30.0)
        await eventually(lambda: fake_ib.setTimeout.called)  # re-armed after the answer
        fake_ib.reqCurrentTimeAsync.assert_called_once_with()
        assert cm.state is ConnectionState.CONNECTED
        fake_ib.connectAsync.assert_awaited_once()
    finally:
        await cm.stop()


async def test_silent_gateway_is_detected_and_reconnected(
    settings: Settings, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung gateway or half-open socket must not look healthy forever."""
    monkeypatch.setattr(connection_module, "LIVENESS_PROBE_TIMEOUT", 0.02)
    monkeypatch.setattr(connection_module, "INITIAL_BACKOFF", 0.3)
    monkeypatch.setattr(connection_module, "MAX_BACKOFF", 0.3)
    fake_ib.reqCurrentTimeAsync.side_effect = pending()
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        fake_ib.timeoutEvent.emit(30.0)
        await eventually(lambda: cm.state is ConnectionState.NOT_CONNECTED)
        health = cm.health()
        assert "stopped answering" in (health.hint or "")
        assert "liveness probe" in health.last_error.message  # type: ignore[union-attr]
        await eventually(lambda: cm.state is ConnectionState.CONNECTED)
        assert fake_ib.connectAsync.await_count == 2
    finally:
        await cm.stop()


async def test_probe_waits_longer_while_ibkr_is_unreachable(
    settings: Settings, fake_ib: MagicMock
) -> None:
    """Cut off from IBKR, silence is expected for a while, but the gateway is still probed."""
    fake_ib.reqCurrentTimeAsync.side_effect = returns(FIXED_TIME)
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        emit_error(fake_ib, 1100, "Connectivity between IB and TWS has been lost.")
        fake_ib.setTimeout.reset_mock()
        fake_ib.timeoutEvent.emit(30.0)
        await asyncio.sleep(0.01)
        fake_ib.reqCurrentTimeAsync.assert_not_called()
        fake_ib.setTimeout.assert_called_once_with(connection_module.LIVENESS_IDLE_DEGRADED)

        fake_ib.timeoutEvent.emit(connection_module.LIVENESS_IDLE_DEGRADED)
        await eventually(lambda: fake_ib.reqCurrentTimeAsync.called)
        assert cm.state is ConnectionState.CONNECTIVITY_LOST
    finally:
        await cm.stop()


async def test_a_farm_reconnecting_ends_a_2110_outage(
    settings: Settings, fake_ib: MagicMock
) -> None:
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        emit_error(fake_ib, 2110, "Connectivity between TWS and server is broken.")
        assert cm.state is ConnectionState.CONNECTIVITY_LOST
        emit_error(fake_ib, 2104, "Market data farm connection is OK:usfarm")
        assert cm.state is ConnectionState.CONNECTED

        emit_error(fake_ib, 1100, "Connectivity between IB and TWS has been lost.")
        emit_error(fake_ib, 2104, "Market data farm connection is OK:usfarm")
        assert cm.state is ConnectionState.CONNECTIVITY_LOST  # 1100 ends with 1101/1102
    finally:
        await cm.stop()


async def test_a_forced_reconnect_fails_pending_requests_at_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ib_async's disconnect() drops pending futures; they must fail, not time out."""
    monkeypatch.setattr(connection_module, "INITIAL_BACKOFF", 5.0)
    ib = make_fake_ib()
    ib.wrapper = Wrapper(ib)
    cm = manager(settings, ib)
    await cm.start()
    try:
        future = ib.wrapper.startReq(42)
        cm._force_reconnect("the gateway stopped answering")
        with pytest.raises(ConnectionError, match="stopped answering"):
            await asyncio.wait_for(future, 1)
    finally:
        await cm.stop()


async def test_the_session_changes_with_every_connection(
    settings: Settings, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection_module, "INITIAL_BACKOFF", 0.01)
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        first = cm.session
        drop_connection(fake_ib)
        await eventually(lambda: cm.state is ConnectionState.CONNECTED and cm.session != first)
    finally:
        await cm.stop()


def gateway_clock(ib: MagicMock, *, window: float) -> list[float]:
    """Answer ``reqCurrentTimeAsync`` the way IB Gateway does; return the send times.

    The answer arrives through ib_async's wrapper on the next loop turn, except that a
    request within ``window`` seconds of the last answer is ignored (no answer, no
    error). Set this up before building the manager, which hooks the wrapper.
    """
    ib.wrapper = Wrapper(ib)
    sent: list[float] = []
    answered: list[float] = []

    def request() -> asyncio.Future[Any]:
        future: asyncio.Future[Any] = ib.wrapper.startReq("currentTime")
        now = time.monotonic()
        sent.append(now)
        if not answered or now - answered[-1] >= window:
            answered.append(now)
            asyncio.get_running_loop().call_soon(ib.wrapper.currentTime, int(time.time()))
        return future

    ib.reqCurrentTimeAsync.side_effect = request
    return sent


async def test_current_time_requests_are_spaced_past_the_gateway_window(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IB Gateway ignores a reqCurrentTime within a second of its last answer."""
    monkeypatch.setattr(connection_module, "CURRENT_TIME_INTERVAL", 0.1)
    ib = make_fake_ib()
    sent = gateway_clock(ib, window=0.08)
    cm = manager(settings, ib)
    await cm.start()
    try:
        async with asyncio.timeout(2):
            await cm.request_current_time()
            await cm.request_current_time()
            await asyncio.gather(*(cm.request_current_time() for _ in range(2)))
        assert len(sent) == 4
        assert min(later - earlier for earlier, later in itertools.pairwise(sent)) >= 0.1

        await asyncio.sleep(0.1)  # nothing to wait for once the interval has passed
        started = time.monotonic()
        await asyncio.wait_for(cm.request_current_time(), 2)
        assert sent[-1] - started < 0.05
    finally:
        await cm.stop()


async def test_a_late_current_time_answer_still_spaces_the_next_request(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway's window restarts with every answer, even one its caller gave up on."""
    monkeypatch.setattr(connection_module, "CURRENT_TIME_INTERVAL", 0.1)
    ib = make_fake_ib()
    ib.wrapper = Wrapper(ib)
    sent: list[float] = []

    def request() -> asyncio.Future[Any]:
        sent.append(time.monotonic())
        future: asyncio.Future[Any] = ib.wrapper.startReq("currentTime")
        return future  # answered by hand below

    ib.reqCurrentTimeAsync.side_effect = request
    cm = manager(settings, ib)
    await cm.start()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(cm.request_current_time(), 0.02)
        ib.wrapper.currentTime(int(time.time()))  # arrives after the caller gave up
        answered = time.monotonic()
        second = asyncio.ensure_future(cm.request_current_time())
        await eventually(lambda: len(sent) == 2)
        assert sent[1] - answered >= 0.1
        ib.wrapper.currentTime(int(time.time()))
        assert (await asyncio.wait_for(second, 1)).tzinfo is not None
    finally:
        await cm.stop()


async def test_request_current_time_fails_fast_while_down(
    settings: Settings, fake_ib: MagicMock
) -> None:
    cm = manager(settings, fake_ib)
    with pytest.raises(NotConnectedError):
        await cm.request_current_time()
    fake_ib.reqCurrentTimeAsync.assert_not_called()


async def test_request_locks_are_dropped_when_idle(settings: Settings, fake_ib: MagicMock) -> None:
    cm = manager(settings, fake_ib)
    for rule in range(100):
        async with cm.request_lock(f"marketRule-{rule}"):
            pass
    gc.collect()
    assert len(cm._request_locks) == 0


async def test_request_locks_are_shared_per_key(settings: Settings, fake_ib: MagicMock) -> None:
    cm = manager(settings, fake_ib)
    assert cm.request_lock("currentTime") is cm.request_lock("currentTime")
    assert cm.request_lock("currentTime") is not cm.request_lock("positions")


async def test_informational_codes_are_not_errors(settings: Settings, fake_ib: MagicMock) -> None:
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        emit_error(fake_ib, 2104, "Market data farm connection is OK:usfarm")
        emit_error(fake_ib, 200, "No security definition", req_id=12)
        assert cm.health().last_error is None
        emit_error(fake_ib, 2103, "Market data farm connection is broken:usfarm")
        assert cm.health().last_error is not None
        assert cm.health().last_error.code == 2103  # type: ignore[union-attr]
    finally:
        await cm.stop()


async def test_readonly_profile_disables_trading(settings: Settings, fake_ib: MagicMock) -> None:
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        with pytest.raises(ConfigurationError, match="IBKR_MCP_PROFILE=trading"):
            cm.require_trading()
    finally:
        await cm.stop()
    with pytest.raises(NotConnectedError):
        cm.require_trading()


async def test_trading_on_paper_syncs_orders_after_a_read_only_connect(
    settings_factory: Callable[..., Settings], fake_ib: MagicMock
) -> None:
    cm = manager(settings_factory(profile="trading"), fake_ib)
    await cm.start()
    try:
        assert fake_ib.connectAsync.await_args.kwargs["readonly"] is True
        fake_ib.reqOpenOrdersAsync.assert_called_once_with()
        fake_ib.reqCompletedOrdersAsync.assert_called_once_with(False)
        assert cm.trading_enabled is True
        assert cm.health().orders_synced is True
        cm.require_trading()

        emit_error(fake_ib, 321, "Error validating request: API interface is in Read-Only mode.")
        assert cm.health().api_read_only is True
        assert cm.trading_enabled is False
        with pytest.raises(ConfigurationError, match="READ_ONLY_API"):
            cm.require_trading()
    finally:
        await cm.stop()


async def test_live_account_blocks_trading_without_allow_live(
    settings_factory: Callable[..., Settings],
) -> None:
    ib = make_fake_ib([LIVE_ACCOUNT])
    cm = manager(settings_factory(profile="trading"), ib)
    await cm.start()
    try:
        assert ib.connectAsync.await_args.kwargs["readonly"] is True
        ib.reqOpenOrdersAsync.assert_not_called()
        health = cm.health()
        assert health.is_paper is False
        assert health.trading_enabled is False
        assert health.orders_synced is False
        with pytest.raises(LiveTradingDisabledError, match="IBKR_MCP_ALLOW_LIVE"):
            cm.require_trading()
    finally:
        await cm.stop()


async def test_allow_live_opens_a_read_write_session(
    settings_factory: Callable[..., Settings],
) -> None:
    ib = make_fake_ib([LIVE_ACCOUNT])
    cm = manager(settings_factory(profile="trading", allow_live=True), ib)
    await cm.start()
    try:
        assert ib.connectAsync.await_args.kwargs["readonly"] is False
        ib.reqOpenOrdersAsync.assert_not_called()  # connectAsync syncs orders itself
        assert cm.trading_enabled is True
        assert cm.health().orders_synced is True
    finally:
        await cm.stop()


async def test_trading_stays_off_without_managed_accounts(
    settings_factory: Callable[..., Settings],
) -> None:
    """Paper or live cannot be told apart without accounts: fail closed."""
    ib = make_fake_ib([])
    cm = manager(settings_factory(profile="trading"), ib)
    await cm.start()
    try:
        assert cm.health().is_paper is None
        assert cm.trading_enabled is False
        with pytest.raises(LiveTradingDisabledError, match="no managed accounts"):
            cm.require_trading()
    finally:
        await cm.stop()


async def test_market_data_type_is_applied_and_survives_reconnects(
    settings_factory: Callable[..., Settings], fake_ib: MagicMock
) -> None:
    cm = manager(settings_factory(market_data_type=3), fake_ib)
    await cm.start()
    try:
        fake_ib.reqMarketDataType.assert_called_once_with(3)
        cm.set_market_data_type(4)
        assert cm.market_data_type == 4
        fake_ib.reqMarketDataType.assert_called_with(4)
        drop_connection(fake_ib)
        fake_ib.reqMarketDataType.reset_mock()
        await eventually(lambda: cm.state is ConnectionState.CONNECTED)
        fake_ib.reqMarketDataType.assert_called_once_with(4)
        with pytest.raises(InvalidRequestError, match="delayed-frozen"):
            cm.set_market_data_type(7)
    finally:
        await cm.stop()


async def test_wait_connected(settings: Settings, fake_ib: MagicMock) -> None:
    failing_then_connecting(fake_ib, *[ConnectionRefusedError()] * 1000)
    cm = manager(settings, fake_ib)
    await cm.start()
    try:
        with pytest.raises(NotConnectedError, match="refused"):
            await cm.wait_connected(0.05)
    finally:
        await cm.stop()

    ok = manager(settings, make_fake_ib())
    await ok.start()
    await ok.wait_connected(1)
    await ok.stop()


async def test_user_info_shim_delivers_the_white_branding_id() -> None:
    ib = IB()
    _install_compat_shims(ib)
    future = ib.wrapper.startReq(7)
    ib.wrapper.userInfo(7, "brand-id")
    assert await future == "brand-id"


def test_details_shim_lets_the_decoder_parse_fractional_ev_multipliers() -> None:
    """ib_async 2.1.0 converts ContractDetails.evMultiplier with int(), like coupon."""
    ib = IB()
    _install_compat_shims(ib)
    decoder = Decoder(ib.wrapper, 178)
    fractional = ContractDetails(evMultiplier="0.5")  # type: ignore[arg-type]
    decoder.parse(fractional)
    assert fractional.evMultiplier == 0.5


def test_bond_coupon_shim_lets_the_decoder_parse_fractional_coupons() -> None:
    """ib_async 2.1.0 converts ContractDetails.coupon with int() and drops "4.25" rows."""
    ib = IB()
    _install_compat_shims(ib)
    _install_compat_shims(ib)  # idempotent
    decoder = Decoder(ib.wrapper, 178)
    fractional = ContractDetails(coupon="4.25")  # type: ignore[arg-type]  # as decoded text
    decoder.parse(fractional)
    assert fractional.coupon == 4.25
    missing = ContractDetails(coupon="")  # type: ignore[arg-type]
    decoder.parse(missing)
    assert missing.coupon == 0.0


def test_client_version_range() -> None:
    assert ConnectionManager.client_version_range() == "157..178"
