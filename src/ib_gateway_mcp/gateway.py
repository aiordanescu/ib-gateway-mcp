"""The :class:`Gateway` facade: one object for the whole library API.

Example::

    from ib_gateway_mcp import Gateway, Settings

    async with Gateway(Settings(ib_port=4004)) as gw:
        await gw.wait_connected(timeout=15)
        print(gw.ops.health().state)
        print((await gw.ops.server_time()).server_time)
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Self

from ib_async import IB

from ib_gateway_mcp.accounts import AccountScope
from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.connection import ConnectionManager
from ib_gateway_mcp.models.ops import HealthReport
from ib_gateway_mcp.safety import SafetyRails
from ib_gateway_mcp.services import (
    AccountService,
    AdminService,
    AdvisorService,
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
from ib_gateway_mcp.services._pacing import HistoricalPacing
from ib_gateway_mcp.startup import check_audit_log, log_safety_configuration
from ib_gateway_mcp.subscriptions import SubscriptionRegistry

__all__ = ["Gateway"]


class Gateway:
    """Connection, account scope, subscriptions and every domain service, wired together.

    Use it as an async context manager (or call :meth:`start` and :meth:`stop`).
    Starting never fails because the gateway is down: the connection keeps retrying in
    the background, and calls raise :class:`~ib_gateway_mcp.errors.NotConnectedError`
    until it is up.

    Args:
        settings: Configuration; read from the environment when omitted.
        ib_factory: Builds the ``ib_async.IB`` instance; tests pass a fake.
        safety: Preview store, order policy, rate limiter, circuit breaker and audit
            log shared by every write path; built from ``settings`` when omitted
            (tests inject rails with fake clocks).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        ib_factory: Callable[[], IB] = IB,
        safety: SafetyRails | None = None,
    ) -> None:
        self.settings = settings if settings is not None else Settings()
        self.safety = safety if safety is not None else SafetyRails.from_settings(self.settings)
        self.accounts = AccountScope(self.settings)
        self.subscriptions = SubscriptionRegistry(self.settings)
        self.pacing = HistoricalPacing()
        """IBKR's historical-data pacing and open-request cap, shared by the history
        tools, real-time bars and live bar backfills."""
        self.connection = ConnectionManager(
            self.settings,
            ib_factory,
            accounts=self.accounts,
            on_resubscribe=self.subscriptions.resubscribe_all,
            on_disconnect=self.subscriptions.mark_all_stale,
        )

        self.ops = OpsService(self)
        self.contracts = ContractsService(self)
        self.market_data = MarketDataService(self)
        self.history = HistoryService(self)
        self.scanners = ScannersService(self)
        self.news = NewsService(self)
        self.fundamentals = FundamentalsService(self)
        self.account = AccountService(self)
        self.options = OptionsService(self)
        self.orders = OrdersService(self)
        self.advisor = AdvisorService(self)
        self.admin = AdminService(self)

    @property
    def ib(self) -> IB:
        """The connected ``IB`` instance, for anything the services do not cover yet.

        An escape hatch for library users: calls made on it bypass the account scope,
        the trading gate and the order safety rails. The MCP layer never uses it.

        Raises:
            NotConnectedError: The gateway connection is not up.
        """
        return self.connection.ib

    def health(self) -> HealthReport:
        """Return the connection's health (same as ``gw.ops.health()``)."""
        return self.ops.health()

    async def start(self, *, wait: float | None = None) -> None:
        """Connect (or start retrying in the background) and start the subscription reaper.

        Logs the safety configuration first (warning about risky combinations), and
        refuses to start when a write toolset is on and the audit file cannot be written.

        Args:
            wait: Longest time (seconds) to wait for the first connection attempt; by
                default, until that attempt ends (each connect step is bounded by
                ``IB_CONNECT_TIMEOUT``; see ``ConnectionManager.start``). Pass ``0`` to
                return at once; :meth:`wait_connected` waits for a session.

        Raises:
            ConfigurationError: The audit file cannot be written while writes are enabled.
        """
        check_audit_log(self.settings, self.safety.audit)
        log_safety_configuration(self.settings)
        await self.connection.start(wait=wait)
        try:
            self.subscriptions.start()
        except BaseException:
            await self.connection.stop()
            raise

    async def stop(self) -> None:
        """Cancel every subscription and disconnect."""
        await self.subscriptions.stop()
        await self.connection.stop()

    async def wait_connected(self, timeout: float | None = None) -> None:
        """Wait until the gateway connection is up.

        Raises:
            NotConnectedError: Not connected within ``timeout`` seconds (None waits forever).
        """
        await self.connection.wait_connected(timeout)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()
