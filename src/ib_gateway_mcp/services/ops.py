"""Operational queries: health, server time, connection details, accounts, user info."""

from __future__ import annotations

import logging
import time
from importlib.metadata import PackageNotFoundError, version

from ib_gateway_mcp._util import clean_str, ensure_utc, utc_now
from ib_gateway_mcp._version import __version__
from ib_gateway_mcp.connection import ConnectionManager
from ib_gateway_mcp.errors import IbGatewayMcpError
from ib_gateway_mcp.models.ops import (
    AccountInfo,
    AccountList,
    ConnectionInfo,
    HealthProbe,
    HealthReport,
    ServerTime,
    UserInfo,
)
from ib_gateway_mcp.services.base import BaseService

__all__ = ["OpsService"]

logger = logging.getLogger(__name__)


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


class OpsService(BaseService):
    """Health and housekeeping for the gateway connection."""

    def health(self) -> HealthReport:
        """Return the connection's health. Works whether or not the gateway is up.

        Adds what the connection alone does not know: the order circuit breaker and the
        subscription usage.
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

    async def health_report(self, *, probe: bool = False) -> HealthReport:
        """Return :meth:`health`, optionally with a live round trip to the gateway.

        With ``probe`` the gateway is asked for its time (as :meth:`server_time` does),
        which proves the socket carries requests right now; the outcome lands in
        ``HealthReport.probe``. ``round_trip_ms`` leaves out the spacing wait between
        clock requests (see :meth:`ConnectionManager.request_current_time`). Never raises.
        """
        if not probe:
            return self.health()
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
        # The probe may have changed the state (e.g. the socket turned out to be dead).
        return self.health().model_copy(update={"probe": outcome})

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
