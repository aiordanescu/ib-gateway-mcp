"""OpsService: the login phase in the health report and the hints it words.

The reader is stubbed here (``GatewayLog.read_async``), so each branch gets the exact
state it needs; ``tests/mcp/test_login_state.py`` reads real fixture files end to end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ib_gateway_mcp import connection as connection_module
from ib_gateway_mcp._gateway_log import DETAILS, describe, is_silent, login_state_from_lines
from ib_gateway_mcp._util import utc_now
from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.connection import HINT_CLIENT_ID_IN_USE, HINT_REFUSED
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.ops import ConnectionState, LoginPhase, LoginState
from ib_gateway_mcp.services.ops import (
    HINT_REPORT_THE_OUTAGE,
    HINT_STILL_RETRYING,
    LOGIN_STATE_MISMATCH,
    LOGIN_STATE_TIMEOUT,
)
from tests.fakes import FIXED_TIME, emit_error, returns

AWAITING = LoginState(
    phase=LoginPhase.AWAITING_2FA,
    since=FIXED_TIME,
    detail=DETAILS["ib_key_push"],
    login_attempts=2,
    twofa_challenges=2,
    counted_since=FIXED_TIME - timedelta(minutes=10),
    counts_complete=False,
    log_updated_at=FIXED_TIME + timedelta(seconds=20),
)
LOGGED_IN = LoginState(
    phase=LoginPhase.LOGGED_IN,
    since=FIXED_TIME,
    detail=DETAILS["password"],
    login_attempts=1,
    twofa_challenges=0,
    log_updated_at=FIXED_TIME,
)
NO_FILE = LoginState(phase=LoginPhase.UNKNOWN, detail=DETAILS["no_file"])
DATA = Path(__file__).resolve().parents[1] / "data" / "gateway_log"


@pytest.fixture(autouse=True)
def _slow_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused gateway stays not_accepting for the whole test (no retry in between)."""
    monkeypatch.setattr(connection_module, "INITIAL_BACKOFF", 60.0)


def refused(fake_ib: MagicMock) -> None:
    fake_ib.connectAsync.side_effect = ConnectionRefusedError(61, "Connection refused")


@pytest.fixture
def make_gateway(
    settings_factory: Callable[..., Settings], fake_ib: MagicMock, tmp_path: Path
) -> Callable[..., Gateway]:
    """A Gateway on ``fake_ib``; ``configured`` sets IB_GATEWAY_SETTINGS_DIR (unused dir)."""

    def make(*, configured: bool = True) -> Gateway:
        settings = settings_factory(gateway_settings_dir=tmp_path if configured else None)
        return Gateway(settings, ib_factory=lambda: fake_ib)

    return make


@pytest.fixture
async def started(make_gateway: Callable[..., Gateway]) -> AsyncIterator[Callable[..., Gateway]]:
    """Like ``make_gateway``, but the gateways it makes are started (and stopped after)."""
    gateways: list[Gateway] = []

    def make(*, configured: bool = True, state: LoginState | None = None) -> Gateway:
        gw = make_gateway(configured=configured)
        if state is not None:
            assert gw.gateway_log is not None
            gw.gateway_log.read_async = AsyncMock(return_value=state)  # type: ignore[method-assign]
        gateways.append(gw)
        return gw

    yield make
    for gw in gateways:
        await gw.stop()


async def test_login_state_is_none_without_the_setting(
    make_gateway: Callable[..., Gateway],
) -> None:
    gw = make_gateway(configured=False)
    assert gw.gateway_log is None
    assert await gw.ops.login_state() is None


async def test_login_state_reads_with_the_timeout(make_gateway: Callable[..., Gateway]) -> None:
    gw = make_gateway()
    assert gw.gateway_log is not None
    read = AsyncMock(return_value=AWAITING)
    gw.gateway_log.read_async = read  # type: ignore[method-assign]
    assert await gw.ops.login_state() == AWAITING
    read.assert_awaited_once_with(LOGIN_STATE_TIMEOUT)


async def test_a_failed_read_is_unknown_and_logs_the_class_only(
    make_gateway: Callable[..., Gateway], caplog: pytest.LogCaptureFixture
) -> None:
    gw = make_gateway()
    assert gw.gateway_log is not None
    gw.gateway_log.read_async = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("line content that must stay private")
    )
    state = await gw.ops.login_state()
    assert state == LoginState(phase=LoginPhase.UNKNOWN, detail=DETAILS["failed"])
    assert "RuntimeError" in caplog.text
    assert "must stay private" not in caplog.text


async def test_health_stays_free_of_io(started: Callable[..., Gateway]) -> None:
    """/healthz, /readyz and Gateway.health use it: the log is never read there."""
    gw = started(state=AWAITING)
    await gw.start()
    assert gw.ops.health().login_state is None
    assert gw.health().login_state is None
    assert gw.gateway_log is not None
    gw.gateway_log.read_async.assert_not_awaited()  # type: ignore[attr-defined]


async def test_an_unexplained_refusal_gets_the_login_phase_as_its_hint(
    started: Callable[..., Gateway], fake_ib: MagicMock
) -> None:
    refused(fake_ib)
    gw = started(state=AWAITING)
    await gw.start()
    report = await gw.ops.health_report()
    assert report.state is ConnectionState.NOT_ACCEPTING
    assert report.login_state == AWAITING
    assert report.hint == f"{describe(AWAITING, utc_now())} {HINT_STILL_RETRYING}"
    assert report.hint.startswith("The gateway is waiting for two-factor approval")
    assert "The API doesn't say which" not in report.hint


async def test_an_unknown_phase_keeps_the_generic_hint(
    started: Callable[..., Gateway], fake_ib: MagicMock
) -> None:
    refused(fake_ib)
    gw = started(state=NO_FILE)
    await gw.start()
    report = await gw.ops.health_report()
    assert report.login_state == NO_FILE
    assert report.hint == f"{HINT_REFUSED} No launcher.log in the settings directory."


async def test_explained_states_keep_their_hint(
    started: Callable[..., Gateway], fake_ib: MagicMock
) -> None:
    async def clash(*_args: object, **_kwargs: object) -> None:
        emit_error(fake_ib, 326, "Unable to connect as the client id is already in use.")
        raise TimeoutError

    fake_ib.connectAsync.side_effect = clash
    gw = started(state=AWAITING)
    await gw.start()
    report = await gw.ops.health_report()
    assert report.state is ConnectionState.NOT_ACCEPTING
    assert report.hint == HINT_CLIENT_ID_IN_USE.format(client_id=80)
    assert report.login_state == AWAITING


async def test_a_log_that_contradicts_the_session_is_unknown(
    started: Callable[..., Gateway], caplog: pytest.LogCaptureFixture
) -> None:
    gw = started(state=AWAITING)
    await gw.start()
    first = await gw.ops.health_report()
    second = await gw.ops.health_report()
    for report in (first, second):
        assert report.state is ConnectionState.CONNECTED
        assert report.hint is None
        assert report.login_state == LoginState(
            phase=LoginPhase.UNKNOWN,
            detail=LOGIN_STATE_MISMATCH,
            log_updated_at=AWAITING.log_updated_at,
        )
    warnings = [r for r in caplog.records if r.name == "ib_gateway_mcp.services.ops"]
    assert len(warnings) == 1  # once, not on every call
    assert "awaiting_2fa" in warnings[0].getMessage()


async def test_a_log_that_went_silent_before_a_login_contradicts_the_session(
    started: Callable[..., Gateway], caplog: pytest.LogCaptureFixture
) -> None:
    """A wrong directory whose pre-login log went stale is not "the gateway is down"."""
    written = datetime(2026, 8, 29, 22, 46, tzinfo=UTC)  # the fixture's last lines
    lines = (DATA / "2fa_timeout_retry.log").read_text(encoding="utf-8").splitlines()
    silent = login_state_from_lines(lines, now=written + timedelta(days=1), log_mtime=written)
    assert is_silent(silent)
    gw = started(state=silent)
    await gw.start()
    first = await gw.ops.health_report()
    second = await gw.ops.health_report()
    for report in (first, second):
        assert report.state is ConnectionState.CONNECTED
        assert report.login_state == LoginState(
            phase=LoginPhase.UNKNOWN, detail=LOGIN_STATE_MISMATCH, log_updated_at=written
        )
    warnings = [r for r in caplog.records if r.name == "ib_gateway_mcp.services.ops"]
    assert len(warnings) == 1
    assert "shows a login that went silent while the API session is up" in (
        warnings[0].getMessage()
    )


@pytest.mark.parametrize("state", [LOGGED_IN, NO_FILE])
async def test_a_consistent_log_is_kept_while_connected(
    started: Callable[..., Gateway], state: LoginState
) -> None:
    gw = started(state=state)
    await gw.start()
    report = await gw.ops.health_report()
    assert report.state is ConnectionState.CONNECTED
    assert report.login_state == state
    assert report.hint is None


async def test_connectivity_lost_keeps_its_hint(
    started: Callable[..., Gateway], fake_ib: MagicMock
) -> None:
    gw = started(state=LOGGED_IN)
    await gw.start()
    emit_error(fake_ib, 1100, "Connectivity between IB and TWS has been lost.")
    report = await gw.ops.health_report()
    assert report.state is ConnectionState.CONNECTIVITY_LOST
    assert report.hint is not None
    assert report.hint.startswith("The gateway is up but has lost its connection")
    assert report.login_state == LOGGED_IN


async def test_unconfigured_refusals_ask_to_report_the_outage(
    started: Callable[..., Gateway], fake_ib: MagicMock
) -> None:
    refused(fake_ib)
    gw = started(configured=False)
    await gw.start()
    report = await gw.ops.health_report()
    assert report.login_state is None
    assert report.hint == f"{HINT_REFUSED} {HINT_REPORT_THE_OUTAGE}"
    assert report.last_disconnect_at is not None


async def test_unconfigured_explained_states_keep_their_hint(
    started: Callable[..., Gateway], fake_ib: MagicMock
) -> None:
    fake_ib.connectAsync.side_effect = OSError(8, "nodename nor servname provided")
    gw = started(configured=False)
    await gw.start()
    report = await gw.ops.health_report()
    assert report.state is ConnectionState.NOT_ACCEPTING
    assert report.hint is not None
    assert report.hint.startswith("Cannot reach the gateway")
    assert HINT_REPORT_THE_OUTAGE not in report.hint


async def test_the_probe_and_the_login_phase_come_together(
    started: Callable[..., Gateway], fake_ib: MagicMock
) -> None:
    fake_ib.reqCurrentTimeAsync.side_effect = returns(FIXED_TIME)
    gw = started(state=LOGGED_IN)
    await gw.start()
    report = await gw.ops.health_report(probe=True)
    assert report.probe is not None
    assert report.probe.ok is True
    assert report.login_state == LOGGED_IN
