"""get_health's login_state end to end: a real settings directory, read through the server.

Each test copies a scrubbed fixture from ``tests/data/gateway_log/`` into a temporary
settings directory as ``launcher.log``, fresh (mtime now), as a gateway writing it would
leave it. Times inside the fixtures are weeks old, so hints carry their date.
"""

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx2
import pytest

from ib_gateway_mcp import connection as connection_module
from ib_gateway_mcp._gateway_log import GatewayLog
from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.connection import (
    HINT_CLIENT_ID_IN_USE,
    HINT_CONNECT_TIMEOUT,
    HINT_CONNECTIVITY_LOST,
    HINT_REFUSED,
    LOGIN_PHASE_POINTER,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.mcp.server import HEALTH_PATH, MCP_PATH, READY_PATH, build_server
from ib_gateway_mcp.models.ops import ConnectionState, HealthReport, LoginPhase
from ib_gateway_mcp.services.ops import (
    HINT_REPORT_THE_OUTAGE,
    HINT_STILL_RETRYING,
    LOGIN_STATE_MISMATCH,
)
from tests.conftest import McpClientFactory
from tests.fakes import emit_error

DATA = Path(__file__).resolve().parents[1] / "data" / "gateway_log"
AWAITING_FIXTURE = "2fa_timeout_retry.log"  # ends with an IB Key push waiting for approval
LOGGED_IN_FIXTURE = "cold_start.log"  # ends with a password login
# Text from the fixtures that must never reach tool output.
LOG_TOKENS = ("192.0.2.", "server.example", "a" * 40, "XXXXX", "userDirname")


@pytest.fixture(autouse=True)
def _slow_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused gateway stays not_accepting for the whole test (no retry in between)."""
    monkeypatch.setattr(connection_module, "INITIAL_BACKOFF", 60.0)


@pytest.fixture
def settings_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "tws_settings"
    directory.mkdir()
    return directory


def install(directory: Path, fixture: str) -> None:
    """Copy a fixture in as launcher.log, written just now."""
    target = directory / "launcher.log"
    shutil.copyfile(DATA / fixture, target)
    now = time.time()
    os.utime(target, (now, now))


def refuse(fake_ib: MagicMock) -> None:
    fake_ib.connectAsync.side_effect = ConnectionRefusedError(61, "Connection refused")


def unanswered_tcp_connect(fake_ib: MagicMock) -> None:
    """A blackholed host: the connect times out before ib_async sends its API greeting."""
    fake_ib.client.conn = SimpleNamespace(numMsgSent=0)
    fake_ib.connectAsync.side_effect = TimeoutError()


def clash(fake_ib: MagicMock) -> None:
    async def side_effect(*_args: Any, **_kwargs: Any) -> Any:
        emit_error(fake_ib, 326, "Unable to connect as the client id is already in use.")
        raise TimeoutError

    fake_ib.connectAsync.side_effect = side_effect


async def get_health(mcp_client: McpClientFactory, **settings: Any) -> tuple[HealthReport, str]:
    async with mcp_client(**settings) as client:
        result = await client.call_tool("get_health", {})
    assert not result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    return HealthReport.model_validate(result.structured_content), text


async def test_a_refusal_while_2fa_is_pending_says_so(
    mcp_client: McpClientFactory, fake_ib: MagicMock, settings_dir: Path
) -> None:
    install(settings_dir, AWAITING_FIXTURE)
    refuse(fake_ib)
    health, text = await get_health(mcp_client, gateway_settings_dir=settings_dir)
    assert health.state is ConnectionState.NOT_ACCEPTING
    login = health.login_state
    assert login is not None
    assert login.phase is LoginPhase.AWAITING_2FA
    assert login.twofa_challenges == 2
    assert login.retry_at is None
    assert health.hint == (
        "The gateway is waiting for two-factor approval: IBKR sent an IB Key push to IBKR "
        "Mobile at 2026-08-29 22:45:32 UTC (at least 2 challenges in this login sequence). "
        "The account holder must approve it; an unanswered challenge ends after about 4 to "
        f"15 minutes, and the gateway may then send a new one. {HINT_STILL_RETRYING}"
    )
    for token in (*LOG_TOKENS, str(settings_dir)):
        assert token not in text


async def test_not_connected_errors_point_to_get_health(
    mcp_client: McpClientFactory, fake_ib: MagicMock, settings_dir: Path
) -> None:
    install(settings_dir, AWAITING_FIXTURE)
    refuse(fake_ib)
    async with mcp_client(gateway_settings_dir=settings_dir) as client:
        result = await client.call_tool("get_server_time", {})
    assert result.is_error
    message = result.content[0].text  # type: ignore[union-attr]
    assert "not_connected: Not connected to the gateway (not_accepting)." in message
    assert message.endswith(f"{HINT_REFUSED} {LOGIN_PHASE_POINTER}")


async def test_an_unreadable_phase_keeps_the_generic_hint(
    mcp_client: McpClientFactory, fake_ib: MagicMock, settings_dir: Path
) -> None:
    refuse(fake_ib)  # and no launcher.log in the directory
    health, _text = await get_health(mcp_client, gateway_settings_dir=settings_dir)
    assert health.login_state is not None
    assert health.login_state.phase is LoginPhase.UNKNOWN
    assert health.hint == f"{HINT_REFUSED} No launcher.log in the settings directory."


async def test_connectivity_lost_keeps_its_hint(
    mcp_client: McpClientFactory, fake_ib: MagicMock, settings_dir: Path
) -> None:
    install(settings_dir, LOGGED_IN_FIXTURE)
    async with mcp_client(gateway_settings_dir=settings_dir) as client:
        emit_error(fake_ib, 1100, "Connectivity between IB and TWS has been lost.")
        result = await client.call_tool("get_health", {})
    health = HealthReport.model_validate(result.structured_content)
    assert health.state is ConnectionState.CONNECTIVITY_LOST
    assert health.hint == HINT_CONNECTIVITY_LOST.format(code=1100)
    assert health.login_state is not None
    assert health.login_state.phase is LoginPhase.LOGGED_IN


async def test_a_client_id_clash_keeps_its_hint(
    mcp_client: McpClientFactory, fake_ib: MagicMock, settings_dir: Path
) -> None:
    install(settings_dir, AWAITING_FIXTURE)
    clash(fake_ib)
    health, _text = await get_health(mcp_client, gateway_settings_dir=settings_dir)
    assert health.state is ConnectionState.NOT_ACCEPTING
    assert health.hint == HINT_CLIENT_ID_IN_USE.format(client_id=80)
    assert health.login_state is not None
    assert health.login_state.phase is LoginPhase.AWAITING_2FA


async def test_an_unreachable_host_keeps_its_hint(
    mcp_client: McpClientFactory, fake_ib: MagicMock, settings_dir: Path
) -> None:
    """The TCP connect got no answer: the cause is known, so the login phase isn't worded."""
    install(settings_dir, AWAITING_FIXTURE)
    unanswered_tcp_connect(fake_ib)
    health, _text = await get_health(mcp_client, gateway_settings_dir=settings_dir)
    assert health.state is ConnectionState.NOT_ACCEPTING
    assert health.hint == HINT_CONNECT_TIMEOUT.format(host="127.0.0.1", port=4004, timeout=1.0)
    assert health.login_state is not None
    assert health.login_state.phase is LoginPhase.AWAITING_2FA


async def test_a_log_that_contradicts_the_connected_gateway_is_unknown(
    mcp_client: McpClientFactory, settings_dir: Path
) -> None:
    install(settings_dir, AWAITING_FIXTURE)
    health, _text = await get_health(mcp_client, gateway_settings_dir=settings_dir)
    assert health.state is ConnectionState.CONNECTED
    assert health.hint is None
    login = health.login_state
    assert login is not None
    assert login.phase is LoginPhase.UNKNOWN
    assert login.detail == LOGIN_STATE_MISMATCH
    assert login.since is None
    assert login.log_updated_at is not None


async def test_a_log_gone_silent_before_a_login_contradicts_the_connected_gateway(
    mcp_client: McpClientFactory, settings_dir: Path
) -> None:
    """Silent for days before any login: another gateway's directory, not a stopped one."""
    install(settings_dir, AWAITING_FIXTURE)
    days_ago = time.time() - 3 * 86400
    os.utime(settings_dir / "launcher.log", (days_ago, days_ago))
    health, _text = await get_health(mcp_client, gateway_settings_dir=settings_dir)
    assert health.state is ConnectionState.CONNECTED
    login = health.login_state
    assert login is not None
    assert login.phase is LoginPhase.UNKNOWN
    assert login.detail == LOGIN_STATE_MISMATCH
    assert login.since is None
    assert login.log_updated_at is not None
    assert abs(login.log_updated_at.timestamp() - days_ago) < 1


async def test_unset_reports_no_login_state(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    connected, _text = await get_health(mcp_client)
    assert connected.login_state is None
    assert connected.hint is None
    assert connected.last_disconnect_at is None  # up at the first attempt: no outage seen

    refuse(fake_ib)
    down, _text = await get_health(mcp_client)
    assert down.login_state is None
    assert down.hint == f"{HINT_REFUSED} {HINT_REPORT_THE_OUTAGE}"
    assert down.last_disconnect_at is not None  # down since at least the server's start


@pytest.mark.parametrize("failure", ["clash", "unreachable", "tcp_timeout"])
async def test_unset_explained_failures_keep_their_hint(
    mcp_client: McpClientFactory, fake_ib: MagicMock, failure: str
) -> None:
    if failure == "clash":
        clash(fake_ib)
    elif failure == "tcp_timeout":
        unanswered_tcp_connect(fake_ib)
    else:
        fake_ib.connectAsync.side_effect = OSError(8, "nodename nor servname provided")
    health, _text = await get_health(mcp_client)
    assert health.state is ConnectionState.NOT_ACCEPTING
    assert health.login_state is None
    assert health.hint is not None
    assert HINT_REPORT_THE_OUTAGE not in health.hint


# --- /healthz and /readyz ------------------------------------------------------------------


@pytest.fixture
def spy_reads(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Counts reads of any settings directory."""
    spy = MagicMock(wraps=GatewayLog.read)
    monkeypatch.setattr(GatewayLog, "read", lambda self, now=None: spy(self, now))
    return spy


async def test_probes_never_read_the_login_phase(
    settings_factory: Callable[..., Settings],
    fake_ib: MagicMock,
    settings_dir: Path,
    spy_reads: MagicMock,
) -> None:
    install(settings_dir, AWAITING_FIXTURE)
    refuse(fake_ib)
    settings: Settings = settings_factory(gateway_settings_dir=settings_dir)
    gateway = Gateway(settings, ib_factory=lambda: fake_ib)
    await gateway.start()
    try:
        server = build_server(settings, gateway=gateway)
        app = server.streamable_http_app(streamable_http_path=MCP_PATH, host=settings.http_host)
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000"
            ) as http:
                live = await http.get(HEALTH_PATH)
                ready = await http.get(READY_PATH)
        spy_reads.assert_not_called()
        report = await gateway.ops.health_report()  # the spy does see get_health's read
        assert report.login_state is not None
        spy_reads.assert_called_once()
    finally:
        await gateway.stop()
    # Unauthenticated probes carry the state and readiness only, never the login phase.
    assert (live.status_code, live.json()) == (200, {"state": "not_accepting", "ready": False})
    assert (ready.status_code, ready.json()) == (503, {"state": "not_accepting", "ready": False})
