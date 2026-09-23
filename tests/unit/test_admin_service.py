"""AdminService against the autospecced fake IB: circuit breaker, server log level,
display groups."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from ib_async.wrapper import RequestError

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    ConfigurationError,
    ConfirmationUnavailableError,
    IbApiError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RequestTimeoutError,
    SubscriptionNotFoundError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.admin import SERVER_LOG_LEVELS, DisplayGroupSnapshot, ServerLogLevel
from ib_gateway_mcp.models.common import ComboLegSpec, ContractSpec
from ib_gateway_mcp.safety import AUDIT_LOGGER_NAME, SafetyRails
from ib_gateway_mcp.services.admin import GROUP_UPDATES_MAX
from ib_gateway_mcp.subscriptions import Stream
from tests.fakes import (
    FIXED_TIME,
    FakeClock,
    contract_details,
    emit_error,
    returns,
    stock,
)

# --- fixtures and helpers -----------------------------------------------------------------


class RequestFutures:
    """Stands in for ib_async's per-request futures (``wrapper.startReq``/``_endReq``)."""

    def __init__(self, ib: MagicMock, first_id: int = 42) -> None:
        self.futures: dict[Any, asyncio.Future[Any]] = {}
        ids = itertools.count(first_id)
        ib.client.getReqId.side_effect = lambda: next(ids)
        ib.wrapper.startReq.side_effect = self.start
        ib.wrapper._endReq.side_effect = self.end

    def start(self, key: Any, contract: Any = None, container: Any = None) -> asyncio.Future[Any]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.futures[key] = future
        return future

    def end(self, key: Any, result: Any = None, success: bool = True) -> None:
        future = self.futures.pop(key, None)
        if future is None or future.done():
            return
        if success:
            future.set_result([] if result is None else result)
        else:
            future.set_exception(result)


def soon(action: Callable[[], object]) -> None:
    """Run ``action`` on the next loop iteration, like a gateway answer arriving."""
    asyncio.get_running_loop().call_soon(action)


StartGateway = Callable[..., Awaitable[Gateway]]


@pytest.fixture
async def start_gateway(
    settings_factory: Callable[..., Settings], fake_ib: MagicMock
) -> AsyncIterator[StartGateway]:
    """Start a gateway on ``fake_ib`` (profile full unless overridden)."""
    started: list[Gateway] = []

    async def start(*, safety: SafetyRails | None = None, **overrides: Any) -> Gateway:
        overrides.setdefault("profile", "full")
        gateway = Gateway(settings_factory(**overrides), ib_factory=lambda: fake_ib, safety=safety)
        await gateway.start()
        started.append(gateway)
        return gateway

    yield start
    for gateway in started:
        await gateway.stop()


@pytest.fixture
def futures(fake_ib: MagicMock) -> RequestFutures:
    return RequestFutures(fake_ib)


def audit_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == AUDIT_LOGGER_NAME]


def rails(settings_factory: Callable[..., Settings], clock: FakeClock, **kw: Any) -> SafetyRails:
    return SafetyRails.from_settings(
        settings_factory(**kw), clock=clock.time, monotonic=clock.monotonic
    )


# --- circuit breaker ----------------------------------------------------------------------


async def test_breaker_status(
    start_gateway: StartGateway, settings_factory: Callable[..., Settings]
) -> None:
    clock = FakeClock()
    gateway = await start_gateway(safety=rails(settings_factory, clock, breaker_rejects=2))
    status = gateway.admin.circuit_breaker_status()
    assert (status.is_open, status.consecutive_rejections, status.threshold) == (False, 0, 2)
    assert status.needs_reset is False
    gateway.safety.breaker.record_rejection("margin")
    gateway.safety.breaker.record_rejection("margin again")
    status = gateway.admin.circuit_breaker_status()
    assert status.is_open is True
    assert status.opened_at == FIXED_TIME
    assert status.last_reason == "margin again"
    assert status.needs_reset is True


async def test_reset_with_a_human(
    start_gateway: StartGateway,
    settings_factory: Callable[..., Settings],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger=AUDIT_LOGGER_NAME)
    gateway = await start_gateway(safety=rails(settings_factory, FakeClock(), breaker_rejects=1))
    gateway.safety.breaker.record_rejection("Order rejected: no margin")
    result = gateway.admin.reset_circuit_breaker(
        "  user checked   the margin ", human_confirmed=True
    )
    assert result.reset is True
    assert result.before.is_open is True
    assert result.reason == "user checked the margin"
    assert gateway.safety.breaker.is_open is False
    [event] = audit_events(caplog)
    assert event["event"] == "circuit_reset"
    assert event["reason"] == "user checked the margin"
    assert event["before"]["last_reason"] == "Order rejected: no margin"


async def test_reset_without_a_human_is_refused(start_gateway: StartGateway) -> None:
    gateway = await start_gateway()
    gateway.safety.breaker.record_rejection("rejected")
    with pytest.raises(ConfirmationUnavailableError, match="human"):
        gateway.admin.reset_circuit_breaker("please", human_confirmed=False)
    assert gateway.safety.breaker.consecutive_rejections == 1


async def test_reset_with_nothing_to_reset(
    start_gateway: StartGateway, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger=AUDIT_LOGGER_NAME)
    gateway = await start_gateway()
    result = gateway.admin.reset_circuit_breaker("just checking", human_confirmed=False)
    assert result.reset is False
    assert audit_events(caplog) == []


async def test_reset_needs_a_reason(start_gateway: StartGateway) -> None:
    gateway = await start_gateway()
    with pytest.raises(InvalidRequestError, match="reason is empty"):
        gateway.admin.reset_circuit_breaker("   ", human_confirmed=True)


# --- server log level ---------------------------------------------------------------------


@pytest.mark.parametrize(("level", "code"), list(SERVER_LOG_LEVELS.items()))
async def test_set_server_log_level(
    start_gateway: StartGateway,
    fake_ib: MagicMock,
    caplog: pytest.LogCaptureFixture,
    level: ServerLogLevel,
    code: int,
) -> None:
    caplog.set_level("INFO", logger=AUDIT_LOGGER_NAME)
    gateway = await start_gateway()
    result = gateway.admin.set_server_log_level(level)
    fake_ib.client.setServerLogLevel.assert_called_once_with(code)
    assert (result.level, result.code) == (level, code)
    [event] = audit_events(caplog)
    assert (event["event"], event["level"], event["code"]) == ("server_log_level", level, code)


async def test_set_server_log_level_needs_a_write_profile(
    start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    gateway = await start_gateway(profile="readonly")
    with pytest.raises(ConfigurationError):
        gateway.admin.set_server_log_level("detail")
    fake_ib.client.setServerLogLevel.assert_not_called()


async def test_set_server_log_level_on_a_closed_socket(
    start_gateway: StartGateway, fake_ib: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger=AUDIT_LOGGER_NAME)
    gateway = await start_gateway()
    fake_ib.client.setServerLogLevel.side_effect = ConnectionError("Not connected")
    with pytest.raises(NotConnectedError, match="server log level"):
        gateway.admin.set_server_log_level("error")
    assert audit_events(caplog) == []  # nothing was changed, so nothing is audited


async def test_set_server_log_level_unknown(start_gateway: StartGateway) -> None:
    gateway = await start_gateway()
    with pytest.raises(InvalidRequestError, match="Unknown log level"):
        gateway.admin.set_server_log_level("loud")  # type: ignore[arg-type]


# --- list_display_groups ------------------------------------------------------------------


async def test_list_display_groups(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(profile="readonly")

    def send(req_id: int) -> None:
        soon(lambda: fake_ib.wrapper.displayGroupList(req_id + 1, "9"))  # another request
        soon(lambda: fake_ib.wrapper.displayGroupList(req_id, "1|2|3|x|"))

    fake_ib.client.queryDisplayGroups.side_effect = send
    result = await gateway.admin.list_display_groups()
    fake_ib.client.queryDisplayGroups.assert_called_once_with(42)
    assert result.groups == [1, 2, 3]
    assert "displayGroupList" not in vars(fake_ib.wrapper)  # the hook is gone


async def test_no_display_groups(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(profile="readonly")
    fake_ib.client.queryDisplayGroups.side_effect = lambda req_id: soon(
        lambda: fake_ib.wrapper.displayGroupList(req_id, "")
    )
    with pytest.raises(NotFoundError, match="IB Gateway normally has none"):
        await gateway.admin.list_display_groups()


async def test_display_groups_timeout(start_gateway: StartGateway, futures: RequestFutures) -> None:
    gateway = await start_gateway(profile="readonly", request_timeout=0.05)
    with pytest.raises(RequestTimeoutError, match="TWS window feature"):
        await gateway.admin.list_display_groups()


async def test_display_groups_error(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(profile="readonly")

    def fail(req_id: int) -> None:
        error = RequestError(req_id, 10, "Not supported")
        soon(lambda: fake_ib.wrapper._endReq(req_id, error, False))

    fake_ib.client.queryDisplayGroups.side_effect = fail
    with pytest.raises(IbApiError, match="IB error 10"):
        await gateway.admin.list_display_groups()


# --- display group subscriptions ----------------------------------------------------------


def snapshot(gateway: Gateway, subscription_id: str) -> DisplayGroupSnapshot:
    data = gateway.admin._subscription_data(subscription_id).data
    return DisplayGroupSnapshot.model_validate(data)


async def test_subscribe_display_group(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(profile="readonly")
    sub = await gateway.admin.subscribe_display_group(4)
    fake_ib.client.subscribeToGroupEvents.assert_called_once_with(42, 4)
    assert (sub.kind, sub.key, sub.deduplicated) == ("display_group", "group:4", False)
    assert snapshot(gateway, sub.subscription_id).current is None

    wrapper = fake_ib.wrapper
    wrapper.displayGroupUpdated(42, "265598@SMART")
    wrapper.displayGroupUpdated(99, "1@X")  # someone else's request id
    current = snapshot(gateway, sub.subscription_id).current
    assert current is not None
    assert (current.selection, current.con_id, current.exchange) == ("contract", 265598, "SMART")

    wrapper.displayGroupUpdated(42, "combo")
    wrapper.displayGroupUpdated(42, "none")
    data = snapshot(gateway, sub.subscription_id)
    assert [u.selection for u in data.updates] == ["contract", "combo", "none"]
    assert data.group_id == 4


async def test_display_group_updates_are_bounded(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(profile="readonly")
    sub = await gateway.admin.subscribe_display_group(1)
    for con_id in range(GROUP_UPDATES_MAX + 5):
        fake_ib.wrapper.displayGroupUpdated(42, f"{con_id}@SMART")
    updates = snapshot(gateway, sub.subscription_id).updates
    assert len(updates) == GROUP_UPDATES_MAX
    assert updates[-1].con_id == GROUP_UPDATES_MAX + 4


async def test_display_group_errors_show_in_the_data(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(profile="readonly")
    sub = await gateway.admin.subscribe_display_group(2)
    emit_error(fake_ib, 2104, "Market data farm connection is OK", req_id=42)
    emit_error(fake_ib, 10, "Other request", req_id=7)
    assert snapshot(gateway, sub.subscription_id).error is None
    emit_error(fake_ib, 10, "Display groups are not supported", req_id=42)
    assert snapshot(gateway, sub.subscription_id).error == (
        "IB error 10: Display groups are not supported"
    )
    fake_ib.wrapper.displayGroupUpdated(42, "none")
    assert snapshot(gateway, sub.subscription_id).error is None


async def test_display_group_subscriptions_are_deduplicated(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(profile="readonly")
    first = await gateway.admin.subscribe_display_group(3)
    second = await gateway.admin.subscribe_display_group(3)
    assert second.subscription_id == first.subscription_id
    assert second.deduplicated is True
    fake_ib.client.subscribeToGroupEvents.assert_called_once()


async def test_unsubscribe_display_group(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(profile="readonly")
    sub = await gateway.admin.subscribe_display_group(3)
    other = await gateway.admin.subscribe_display_group(5)
    await gateway.subscriptions.remove(sub.subscription_id)
    fake_ib.client.unsubscribeFromGroupEvents.assert_called_once_with(42)
    fake_ib.wrapper.displayGroupUpdated(42, "1@SMART")  # ignored now
    fake_ib.wrapper.displayGroupUpdated(43, "2@SMART")
    current = snapshot(gateway, other.subscription_id).current
    assert current is not None
    assert current.con_id == 2


async def test_display_group_is_resubscribed_after_a_reconnect(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(profile="readonly")
    sub = await gateway.admin.subscribe_display_group(6)
    assert await gateway.subscriptions.resubscribe_all() == 1
    assert fake_ib.client.subscribeToGroupEvents.call_args_list[-1].args == (43, 6)
    fake_ib.wrapper.displayGroupUpdated(42, "1@SMART")  # the old id is gone
    fake_ib.wrapper.displayGroupUpdated(43, "2@SMART")
    current = snapshot(gateway, sub.subscription_id).current
    assert current is not None
    assert current.con_id == 2


async def test_failed_subscribe_leaves_nothing_behind(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(profile="readonly")
    fake_ib.client.subscribeToGroupEvents.side_effect = ConnectionError("Not connected")
    with pytest.raises(NotConnectedError, match="display group subscription"):
        await gateway.admin.subscribe_display_group(1)
    assert gateway.subscriptions.list() == []
    assert gateway.admin._groups_by_req == {}


async def test_subscribe_display_group_needs_a_positive_id(start_gateway: StartGateway) -> None:
    gateway = await start_gateway(profile="readonly")
    with pytest.raises(InvalidRequestError, match="group_id"):
        await gateway.admin.subscribe_display_group(0)


# --- update_display_group -----------------------------------------------------------------


async def test_update_display_group(
    start_gateway: StartGateway,
    fake_ib: MagicMock,
    futures: RequestFutures,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger=AUDIT_LOGGER_NAME)
    gateway = await start_gateway()
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    sub = await gateway.admin.subscribe_display_group(1)
    result = await gateway.admin.update_display_group(
        sub.subscription_id, ContractSpec(symbol="AAPL")
    )
    fake_ib.client.updateDisplayGroup.assert_called_once_with(42, "265598@SMART")
    assert result.contract.con_id == 265598
    assert (result.group_id, result.contract_info) == (1, "265598@SMART")
    [event] = audit_events(caplog)
    assert (event["event"], event["contract_info"]) == ("display_group_update", "265598@SMART")


async def test_update_display_group_on_a_closed_socket(
    start_gateway: StartGateway,
    fake_ib: MagicMock,
    futures: RequestFutures,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger=AUDIT_LOGGER_NAME)
    gateway = await start_gateway()
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    sub = await gateway.admin.subscribe_display_group(1)
    fake_ib.client.updateDisplayGroup.side_effect = ConnectionError("Not connected")
    with pytest.raises(NotConnectedError, match="display group update"):
        await gateway.admin.update_display_group(sub.subscription_id, ContractSpec(symbol="AAPL"))
    assert audit_events(caplog) == []


async def test_unsubscribe_after_the_socket_closed(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(profile="readonly")
    sub = await gateway.admin.subscribe_display_group(3)
    fake_ib.client.unsubscribeFromGroupEvents.side_effect = ConnectionError("Not connected")
    await gateway.subscriptions.remove(sub.subscription_id)
    assert gateway.admin._groups_by_req == {}


async def test_update_display_group_needs_a_write_profile(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(profile="readonly")
    sub = await gateway.admin.subscribe_display_group(1)
    with pytest.raises(ConfigurationError):
        await gateway.admin.update_display_group(sub.subscription_id, ContractSpec(symbol="AAPL"))
    fake_ib.client.updateDisplayGroup.assert_not_called()


async def test_update_display_group_unknown_subscription(start_gateway: StartGateway) -> None:
    gateway = await start_gateway()
    with pytest.raises(SubscriptionNotFoundError):
        await gateway.admin.update_display_group("display_group-99", ContractSpec(symbol="AAPL"))


async def test_update_display_group_other_kind(start_gateway: StartGateway) -> None:
    gateway = await start_gateway()
    info = await gateway.subscriptions.add(
        "quotes",
        "265598",
        opener=lambda: Stream(
            cancel=lambda: None, snapshot=lambda: DisplayGroupSnapshot(group_id=1)
        ),
    )
    with pytest.raises(InvalidRequestError, match="not a display group"):
        await gateway.admin.update_display_group(info.id, ContractSpec(symbol="AAPL"))


async def test_update_display_group_refuses_combos(
    start_gateway: StartGateway, futures: RequestFutures
) -> None:
    gateway = await start_gateway()
    sub = await gateway.admin.subscribe_display_group(1)
    bag = ContractSpec(
        symbol="AAPL",
        sec_type="BAG",
        combo_legs=[
            ComboLegSpec(con_id=1, action="BUY"),
            ComboLegSpec(con_id=2, action="SELL"),
        ],
    )
    with pytest.raises(InvalidRequestError, match="combos"):
        await gateway.admin.update_display_group(sub.subscription_id, bag)
