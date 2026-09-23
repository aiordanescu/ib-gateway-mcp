"""Gateway facade, BaseService request handling and OpsService."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from ib_async import Order
from ib_async.wrapper import RequestError

from ib_gateway_mcp import Gateway as ExportedGateway
from ib_gateway_mcp import Settings as ExportedSettings
from ib_gateway_mcp import __version__
from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import IbApiError, NotConnectedError, RequestTimeoutError
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.ops import ConnectionState
from ib_gateway_mcp.safety import SafetyRails
from ib_gateway_mcp.services import (
    AccountService,
    AdminService,
    AdvisorService,
    BaseService,
    ContractsService,
    FundamentalsService,
    HistoryService,
    MarketDataService,
    NewsService,
    OpsService,
    OptionsService,
    OrdersService,
    ScannersService,
)
from ib_gateway_mcp.subscriptions import Stream
from tests.fakes import (
    FIXED_TIME,
    LIVE_ACCOUNT,
    PAPER_ACCOUNT,
    FakeClock,
    drop_connection,
    emit_error,
    go_offline,
    make_fake_ib,
    pending,
    raises,
    returns,
    stock,
)

SERVICES = {
    "ops": OpsService,
    "contracts": ContractsService,
    "market_data": MarketDataService,
    "history": HistoryService,
    "scanners": ScannersService,
    "news": NewsService,
    "fundamentals": FundamentalsService,
    "account": AccountService,
    "options": OptionsService,
    "orders": OrdersService,
    "advisor": AdvisorService,
    "admin": AdminService,
}


def test_package_exports() -> None:
    assert ExportedGateway is Gateway
    assert ExportedSettings is Settings
    assert __version__


def test_gateway_wires_every_service(settings: Settings, fake_ib: MagicMock) -> None:
    gw = Gateway(settings, ib_factory=lambda: fake_ib)
    for name, cls in SERVICES.items():
        service = getattr(gw, name)
        assert type(service) is cls
        assert service.gateway is gw
        assert service.settings is settings
        assert service.accounts is gw.accounts
        assert service.subs is gw.subscriptions
        assert service.connection is gw.connection


def test_gateway_reads_settings_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, fake_ib: MagicMock
) -> None:
    monkeypatch.setenv("IB_PORT", "4002")
    assert Gateway(ib_factory=lambda: fake_ib).settings.ib_port == 4002


async def test_context_manager_starts_and_stops(settings: Settings, fake_ib: MagicMock) -> None:
    async with Gateway(settings, ib_factory=lambda: fake_ib) as gw:
        await gw.wait_connected(1)
        assert gw.health().state is ConnectionState.CONNECTED
        assert gw.ib is fake_ib
    assert gw.health().state is ConnectionState.NOT_CONNECTED
    assert not fake_ib.isConnected()
    with pytest.raises(NotConnectedError):
        _ = gw.ib


async def test_start_can_skip_waiting(settings: Settings, fake_ib: MagicMock) -> None:
    fake_ib.connectAsync.side_effect = pending()
    gw = Gateway(settings, ib_factory=lambda: fake_ib)
    await gw.start(wait=0)
    assert gw.health().state is ConnectionState.CONNECTING
    await gw.stop()


async def test_a_drop_marks_subscriptions_stale(gateway: Gateway, fake_ib: MagicMock) -> None:
    stream = Stream(cancel=MagicMock(), snapshot=MagicMock(), resubscribe=MagicMock())
    info = await gateway.subscriptions.add("quote", "1", opener=lambda: stream)
    fake_ib.connectAsync.side_effect = pending()  # stay down after the drop
    drop_connection(fake_ib)
    assert gateway.subscriptions.get(info.id).stale is True


def test_safety_rails_take_one_fake_clock(settings: Settings) -> None:
    clock = FakeClock()
    rails = SafetyRails.from_settings(settings, clock=clock.time, monotonic=clock.monotonic)
    token = rails.previews.issue({"action": "BUY"}, PAPER_ACCOUNT, "order")
    assert token.expires_at == FIXED_TIME + timedelta(seconds=settings.token_ttl)
    rails.breaker.record_rejection("r1")
    for _ in range(settings.breaker_rejects - 1):
        rails.breaker.record_rejection("again")
    assert rails.breaker.opened_at == FIXED_TIME
    clock.advance(settings.token_ttl + 1)
    rails.previews.purge_expired()
    assert len(rails.previews) == 0


async def test_stop_cancels_subscriptions(gateway: Gateway) -> None:
    cancel = MagicMock()
    stream = Stream(cancel=cancel, snapshot=MagicMock())
    await gateway.subscriptions.add("quote", "1", opener=lambda: stream)
    await gateway.stop()
    cancel.assert_called_once_with()


# --- BaseService._call ---------------------------------------------------------------------


async def test_call_translates_request_errors(gateway: Gateway, fake_ib: MagicMock) -> None:
    service = BaseService(gateway)
    fake_ib.reqCurrentTimeAsync.side_effect = raises(
        RequestError(12, 200, "No security definition")
    )
    with pytest.raises(IbApiError) as info:
        await service._call(fake_ib.reqCurrentTimeAsync(), what="x")
    assert (info.value.error_code, info.value.req_id) == (200, 12)
    assert "No security definition" in str(info.value)


async def test_call_times_out_with_context(gateway: Gateway, fake_ib: MagicMock) -> None:
    service = BaseService(gateway)
    fake_ib.reqCurrentTimeAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match=r"0.01s waiting for the clock"):
        await service._call(fake_ib.reqCurrentTimeAsync(), what="the clock", timeout=0.01)

    fake_ib.reqCurrentTimeAsync.side_effect = pending()
    emit_error(fake_ib, 1100, "Connectivity lost")
    with pytest.raises(RequestTimeoutError, match="connectivity_lost"):
        await service._call(fake_ib.reqCurrentTimeAsync(), what="the clock", timeout=0.01)


async def test_call_serializes_string_keyed_requests(gateway: Gateway, fake_ib: MagicMock) -> None:
    """ib_async keys currentTime by name: overlapping calls must not orphan a future."""
    active = peak = 0

    async def slow(*_args: Any) -> datetime:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return FIXED_TIME

    fake_ib.reqCurrentTimeAsync.side_effect = slow
    results = await asyncio.gather(*(gateway.ops.server_time() for _ in range(3)))
    assert [r.server_time for r in results] == [FIXED_TIME] * 3
    assert peak == 1


async def test_call_exclusive_needs_a_factory(gateway: Gateway, fake_ib: MagicMock) -> None:
    service = BaseService(gateway)
    fake_ib.reqCurrentTimeAsync.side_effect = returns(FIXED_TIME)
    request = fake_ib.reqCurrentTimeAsync()
    with pytest.raises(TypeError, match="callable"):
        await service._call(request, what="x", exclusive="currentTime")
    await request  # consume the coroutine


async def test_call_fails_fast_on_a_read_only_rejection(
    gateway: Gateway, fake_ib: MagicMock
) -> None:
    """ib_async treats 321 as a warning, so the request itself would never complete."""
    service = BaseService(gateway)
    fake_ib.whatIfOrderAsync.side_effect = pending()
    order = Order(orderId=7, action="BUY", totalQuantity=1, orderType="LMT", lmtPrice=1.0)
    message = "Error validating request.-'bN' : cause - The API interface is in Read-Only mode."
    loop = asyncio.get_running_loop()
    loop.call_later(0.01, lambda: emit_error(fake_ib, 321, message, req_id=99))  # not ours
    loop.call_later(0.02, lambda: emit_error(fake_ib, 321, message, req_id=7))
    with pytest.raises(IbApiError) as info:
        await service._call(
            lambda: fake_ib.whatIfOrderAsync(stock(), order), what="the what-if", req_id=7
        )
    assert (info.value.error_code, info.value.req_id) == (321, 7)
    assert gateway.health().api_read_only is True

    # A plain timeout now names the read-only API even though the state is 'connected'.
    fake_ib.reqCurrentTimeAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match="read-only mode"):
        await service._call(fake_ib.reqCurrentTimeAsync(), what="the clock", timeout=0.01)


async def test_call_translates_connection_errors(gateway: Gateway, fake_ib: MagicMock) -> None:
    service = BaseService(gateway)
    fake_ib.reqCurrentTimeAsync.side_effect = raises(ConnectionError("Not connected"))
    with pytest.raises(NotConnectedError, match="Lost the gateway connection"):
        await service._call(fake_ib.reqCurrentTimeAsync(), what="the clock")


# --- OpsService ------------------------------------------------------------------------------


async def test_health_works_without_a_connection(settings: Settings, fake_ib: MagicMock) -> None:
    gw = Gateway(settings, ib_factory=lambda: fake_ib)
    health = gw.ops.health()
    assert health.state is ConnectionState.NOT_CONNECTED
    assert health.trading_enabled is False
    assert health.accounts == []


async def test_server_time(gateway: Gateway, fake_ib: MagicMock) -> None:
    fake_ib.reqCurrentTimeAsync.side_effect = returns(FIXED_TIME)
    result = await gateway.ops.server_time()
    assert result.server_time == FIXED_TIME
    assert result.local_time.tzinfo is not None
    assert result.skew_seconds == pytest.approx(
        (result.local_time - FIXED_TIME).total_seconds(), abs=0.01
    )
    assert result.local_time - result.server_time > timedelta(0)


async def test_server_time_when_disconnected(gateway: Gateway, fake_ib: MagicMock) -> None:
    go_offline(fake_ib)
    with pytest.raises(NotConnectedError, match="Not connected to the gateway"):
        await gateway.ops.server_time()


async def test_connection_info(gateway: Gateway, fake_ib: MagicMock) -> None:
    info = gateway.ops.connection_info()
    assert info.connected is True
    assert (info.host, info.port, info.client_id) == ("127.0.0.1", 4004, 80)
    assert info.server_version == 178
    assert info.client_version_range == "157..178"
    assert info.orders_synced is False
    assert info.connected_since is not None
    assert (info.bytes_received, info.bytes_sent) == (2048, 1024)
    assert (info.messages_received, info.messages_sent) == (40, 20)
    assert info.ib_async_version == "2.1.0"

    go_offline(fake_ib)
    offline = gateway.ops.connection_info()
    assert offline.connected is False
    assert offline.server_version is None
    assert offline.bytes_received is None


async def test_list_accounts_names_only_allowed_accounts(
    settings_factory: Callable[..., Settings],
) -> None:
    ib = make_fake_ib([PAPER_ACCOUNT, LIVE_ACCOUNT, "DU7654321"])
    settings = settings_factory(ib_account=PAPER_ACCOUNT, accounts_allowlist=[LIVE_ACCOUNT])
    async with Gateway(settings, ib_factory=lambda: ib) as gw:
        result = gw.ops.list_accounts()
    assert result.default_account == PAPER_ACCOUNT
    assert [(a.account, a.is_paper, a.is_default) for a in result.accounts] == [
        (PAPER_ACCOUNT, True, True),
        (LIVE_ACCOUNT, False, False),
    ]
    assert result.other_managed_accounts == 1
    assert "DU7654321" not in result.model_dump_json()


@pytest.mark.parametrize(("raw", "expected"), [("brand-id", "brand-id"), ("", None), ([], None)])
async def test_user_info(
    gateway: Gateway, fake_ib: MagicMock, raw: object, expected: str | None
) -> None:
    fake_ib.reqUserInfoAsync.side_effect = returns(raw)
    assert (await gateway.ops.user_info()).white_branding_id == expected
