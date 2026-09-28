"""Operational queries: health, server time, connection details, accounts, user info."""

from __future__ import annotations

import logging
import time
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from ib_gateway_mcp._gateway_log import DETAILS, describe, is_silent
from ib_gateway_mcp._util import clean_str, ensure_utc, utc_now
from ib_gateway_mcp._version import __version__
from ib_gateway_mcp.connection import ConnectionManager
from ib_gateway_mcp.errors import IbGatewayMcpError
from ib_gateway_mcp.models.ops import (
    AccountInfo,
    AccountList,
    ConnectionInfo,
    ConnectionState,
    HealthProbe,
    HealthReport,
    LoginPhase,
    LoginState,
    ServerTime,
    UserInfo,
)
from ib_gateway_mcp.services.base import BaseService

if TYPE_CHECKING:
    from ib_gateway_mcp.gateway import Gateway

__all__ = [
    "LOGIN_STATE_MISMATCH",
    "LOGIN_STATE_TIMEOUT",
    "OpsService",
]

logger = logging.getLogger(__name__)

LOGIN_STATE_TIMEOUT = 5.0
"""Seconds ``get_health`` waits for the gateway's launcher.log to be read."""
LOGIN_STATE_MISMATCH = (
    "launcher.log does not match the connected gateway; check IB_GATEWAY_SETTINGS_DIR"
)
"""``LoginState.detail`` when the log shows no login, or went silent before one, while the
API session is up."""
HINT_STILL_RETRYING = "This server keeps retrying the connection in the background."
HINT_REPORT_THE_OUTAGE = (
    "Report since when it has been down (last_disconnect_at; if this server started during "
    "the outage, that is its start time and the outage may be older) rather than guessing a "
    "cause; setting IB_GATEWAY_SETTINGS_DIR lets get_health report the gateway's login phase."
)
_UP_STATES = frozenset({ConnectionState.CONNECTED, ConnectionState.CONNECTIVITY_LOST})
# Phases that fit an API session that is up: a login, or a log that can't tell (unreadable,
# no gateway start in it). A log that went silent before a login (is_silent) doesn't fit:
# the logged-in gateway would have logged its login there.
_CONSISTENT_WHILE_UP = frozenset({LoginPhase.LOGGED_IN, LoginPhase.UNKNOWN})


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


class OpsService(BaseService):
    """Health and housekeeping for the gateway connection."""

    def __init__(self, gateway: Gateway) -> None:
        super().__init__(gateway)
        self._mismatch_logged = False

    def health(self) -> HealthReport:
        """Return the connection's health. Works whether or not the gateway is up.

        Adds what the connection alone does not know: the order circuit breaker and the
        subscription usage. Synchronous and free of I/O (``/healthz`` and ``/readyz`` use
        it), so ``login_state`` stays null: :meth:`health_report` fills it.
        """
        breaker = self.safety.breaker
        return self.connection.health().model_copy(
            update={
                "circuit_open": breaker.is_open,
                "circuit_rejections": breaker.consecutive_rejections,
                "circuit_threshold": breaker.threshold,
                "subscriptions_used": len(self.subs),
                "subscriptions_max": self.settings.max_subscriptions,
            }
        )

    async def login_state(self) -> LoginState | None:
        """The gateway's login phase from its launcher.log; None without the setting.

        Reads ``IB_GATEWAY_SETTINGS_DIR`` off the event loop, waiting at most
        :data:`LOGIN_STATE_TIMEOUT` seconds. Never raises: whatever goes wrong is an
        ``unknown`` phase whose ``detail`` says why.
        """
        gateway_log = self.gateway.gateway_log
        if gateway_log is None:
            return None
        try:
            return await gateway_log.read_async(LOGIN_STATE_TIMEOUT)
        except Exception as exc:  # never raise from a health check
            logger.warning("Reading the gateway's login phase failed (%s).", type(exc).__name__)
            return LoginState(phase=LoginPhase.UNKNOWN, detail=DETAILS["failed"])

    async def health_report(self, *, probe: bool = False) -> HealthReport:
        """Return :meth:`health`, with the login phase and optionally a live round trip.

        With ``probe`` the gateway is asked for its time (as :meth:`server_time` does),
        which proves the socket carries requests right now; the outcome lands in
        ``HealthReport.probe``. ``round_trip_ms`` leaves out the spacing wait between
        clock requests (see :meth:`ConnectionManager.request_current_time`).

        With ``IB_GATEWAY_SETTINGS_DIR`` set, ``login_state`` holds :meth:`login_state`.
        When the gateway refused the connection without a reason
        (:attr:`ConnectionManager.refusal_unexplained`), the hint then words that phase;
        without the setting, it asks to report the outage window rather than guess a
        cause. Every other state keeps its hint. While the session is up, a log that shows
        another phase than logged_in, or went silent before a login, can't be this
        gateway's: ``login_state`` is then unknown with :data:`LOGIN_STATE_MISMATCH`.
        Never raises.
        """
        outcome = await self._probe() if probe else None
        login = await self.login_state()
        # Built last: the probe may have changed the state (the socket turned out dead).
        report = self.health()
        if outcome is not None:
            report = report.model_copy(update={"probe": outcome})
        return self._explain(report, login)

    def _explain(self, report: HealthReport, login: LoginState | None) -> HealthReport:
        """Attach the login phase and word the hint of an unexplained refusal."""
        unexplained = (
            report.state is ConnectionState.NOT_ACCEPTING and self.connection.refusal_unexplained
        )
        if login is None:
            if unexplained and report.hint:
                return report.model_copy(update={"hint": f"{report.hint} {HINT_REPORT_THE_OUTAGE}"})
            return report
        if report.state in _UP_STATES and (
            login.phase not in _CONSISTENT_WHILE_UP or is_silent(login)
        ):
            # The API session is up, so the gateway is logged in: this log is not its log.
            if not self._mismatch_logged:
                self._mismatch_logged = True
                shows = (
                    "a login that went silent"
                    if is_silent(login)
                    else f"the login phase {login.phase.value}"
                )
                logger.warning(
                    "The gateway's launcher.log shows %s while the API session is up; "
                    "IB_GATEWAY_SETTINGS_DIR probably names another gateway's directory.",
                    shows,
                )
            login = LoginState(
                phase=LoginPhase.UNKNOWN,
                detail=LOGIN_STATE_MISMATCH,
                log_updated_at=login.log_updated_at,
            )
        update: dict[str, object] = {"login_state": login}
        if unexplained:
            phase = describe(login, utc_now())
            if login.phase is LoginPhase.UNKNOWN and report.hint:
                # The log can't tell, so the generic hint keeps what the API does say.
                update["hint"] = f"{report.hint} {phase}"
            else:
                update["hint"] = f"{phase} {HINT_STILL_RETRYING}"
        return report.model_copy(update=update)

    async def _probe(self) -> HealthProbe:
        """Ask the gateway for its time and report how it went. Never raises."""
        started = time.perf_counter()
        try:
            answer = await self.server_time()
            round_trip = self.connection.current_time_round_trip  # before any other await
        except IbGatewayMcpError as exc:
            outcome = HealthProbe(ok=False, error=f"{exc.code}: {exc}")
        except Exception as exc:  # never raise from a health check
            logger.warning("Health probe failed unexpectedly", exc_info=True)
            outcome = HealthProbe(ok=False, error=f"{type(exc).__name__}: {exc}")
        else:
            if round_trip is None:
                round_trip = time.perf_counter() - started
            outcome = HealthProbe(
                ok=True,
                round_trip_ms=round(round_trip * 1000, 1),
                server_time=answer.server_time,
            )
        return outcome

    async def server_time(self) -> ServerTime:
        """Ask the gateway for its clock and compare it with this machine's.

        Requests are spaced about a second apart (see
        :meth:`ConnectionManager.request_current_time`), so a call right after another
        one waits for its turn instead of being ignored by the gateway.
        """
        server = await self._call(self.connection.request_current_time, what="the server time")
        local = utc_now()
        server_utc = ensure_utc(server)
        return ServerTime(
            server_time=server_utc,
            local_time=local,
            skew_seconds=round((local - server_utc).total_seconds(), 3),
        )

    def connection_info(self) -> ConnectionInfo:
        """Describe the API session: endpoint, versions and traffic counters."""
        connection = self.connection
        settings = self.settings
        info = ConnectionInfo(
            host=settings.ib_host,
            port=settings.ib_port,
            client_id=settings.ib_client_id,
            connected=connection.is_connected,
            client_version_range=ConnectionManager.client_version_range(),
            ib_async_version=_package_version("ib_async"),
            server_package_version=__version__,
        )
        if not connection.is_connected:
            return info
        stats = self.ib.client.connectionStats()
        return info.model_copy(
            update={
                "server_version": connection.server_version,
                "orders_synced": connection.orders_synced,
                "connected_since": connection.connected_since,
                "bytes_received": stats.numBytesRecv,
                "bytes_sent": stats.numBytesSent,
                "messages_received": stats.numMsgRecv,
                "messages_sent": stats.numMsgSent,
            }
        )

    def list_accounts(self) -> AccountList:
        """List the accounts this server may use, with the default marked.

        Accounts the login manages outside the allowlist are counted but not named.
        Before the first connect the list is empty.
        """
        scope = self.accounts
        return AccountList(
            accounts=[
                AccountInfo(account=account, is_paper=is_paper, is_default=is_default)
                for account, is_paper, is_default in scope.describe()
            ],
            default_account=scope.default,
            other_managed_accounts=len(set(scope.managed) - scope.allowed),
        )

    async def user_info(self) -> UserInfo:
        """Return details about the logged-in user (the white-branding id)."""
        result = await self._call(self.ib.reqUserInfoAsync(), what="user info")
        return UserInfo(white_branding_id=clean_str(result) if isinstance(result, str) else None)
