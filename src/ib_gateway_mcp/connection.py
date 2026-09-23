"""Gateway connection lifecycle: connect, watch, reconnect, and report health.

:class:`ConnectionManager` owns the single ``ib_async.IB`` instance. It connects on the
running asyncio loop (``connectAsync`` only; never the blocking wrappers), keeps a
:class:`~ib_gateway_mcp.models.ops.HealthReport` current from the gateway's error
stream, and reconnects with exponential backoff (1 s doubling to 60 s) whenever the
connection drops. It deliberately does not use ib_async's ``Watchdog``, which assumes
it controls IBC; here the gateway container belongs to someone else.

The manager never fails because the gateway is down: :meth:`ConnectionManager.start`
returns after the first attempt (or a shorter ``wait``) either way, retries continue in
the background, and :attr:`ConnectionManager.ib` raises :class:`NotConnectedError` with a
hint meanwhile. A gateway that goes silent without closing the socket (a hung JVM, a
stalled tunnel) is caught by an idle probe: after ``LIVENESS_IDLE`` seconds without
incoming data the manager asks for the server time, and reconnects if no answer comes.

What protects against unwanted orders
-------------------------------------
``connectAsync(readonly=True)`` is **not** a safety boundary. In ib_async 2.1.0 it only
skips loading open and completed orders at startup; ``placeOrder``, ``cancelOrder`` and
``reqGlobalCancel`` still reach IBKR over such a session. The gates are:

* tool registration: order, advisor and admin tools exist only when their toolset is on;
* :meth:`ConnectionManager.require_trading`, which every write path calls (the MCP
  registry calls it before any WRITE or ADMIN tool body runs): it refuses unless a write
  toolset is enabled, the managed accounts are known, and none of the accounts in scope
  is live without ``IBKR_MCP_ALLOW_LIVE``;
* the order safety rails (:mod:`ib_gateway_mcp.safety`);
* for a hard guarantee on read-only deployments, the gateway's own Read-Only API setting
  (ib-gateway-docker: ``READ_ONLY_API=yes``).

The session is opened with ``readonly=True`` (no startup order sync) unless a write
toolset is enabled **and** live trading is allowed. With a write toolset and no
``IBKR_MCP_ALLOW_LIVE``, orders are synced after the accounts prove to be paper
accounts; a live account in scope leaves trading disabled with a plain explanation,
while every read-only tool keeps working. On a read-only connect the executions are
fetched last, after any order sync: ib_async attaches a fill to its order only when
the order is already known, and never re-attaches an execution it has seen.

Every connection attempt starts a new session (:attr:`ConnectionManager.session`):
ib_async restarts its request ids with each connection, so a stream opened in an
earlier session must not cancel "its" request id in a later one, where the same number
may belong to another request.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime
from typing import Any

from ib_async import IB, Client, ContractDetails
from ib_async.ib import StartupFetch, StartupFetchALL

from ib_gateway_mcp._ib_compat import end_request, fail_pending_requests
from ib_gateway_mcp._util import utc_now
from ib_gateway_mcp.accounts import AccountScope
from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    ConfigurationError,
    InvalidRequestError,
    LiveTradingDisabledError,
    NotConnectedError,
)
from ib_gateway_mcp.models.common import MARKET_DATA_TYPE_NAMES
from ib_gateway_mcp.models.ops import ConnectionState, ErrorInfo, HealthReport

__all__ = ["ConnectionManager", "ConnectionState", "HealthReport"]

logger = logging.getLogger(__name__)

INITIAL_BACKOFF = 1.0
"""Seconds before the first retry."""
MAX_BACKOFF = 60.0
"""Upper bound for the retry delay."""
STABLE_SESSION = 60.0
"""A session must stay up this long (seconds) before a drop resets the retry delay, so a
gateway that accepts and drops at once is not hammered every second."""
LIVENESS_IDLE = 30.0
"""Seconds without incoming data before the gateway is probed."""
LIVENESS_PROBE_TIMEOUT = 10.0
"""Seconds the gateway has to answer the probe before the session is treated as dead."""
CURRENT_TIME_INTERVAL = 1.1
"""Seconds to leave between a ``currentTime`` answer and the next ``reqCurrentTime``.

IB Gateway (seen on 10.45) silently ignores a ``reqCurrentTime`` that arrives within one
second of the last one it answered: no answer and no error, so the request only times
out. An ignored request does not restart that second. The extra 0.1 s is margin."""

# TWS API message codes this module reacts to.
CONNECTIVITY_LOST_CODES = frozenset({1100, 2110})
CONNECTIVITY_RESTORED_CODES = frozenset({1101, 1102})
DATA_LOST_CODE = 1101
READ_ONLY_CODE = 321
CLIENT_ID_IN_USE_CODE = 326
NOT_CONNECTED_CODES = frozenset({502, 504})
# "Market data farm connection is OK" and friends: informational, not errors.
INFORMATIONAL_CODES = frozenset({2104, 2106, 2107, 2108, 2119, 2158})
# "... farm connection is OK": the gateway reaches IBKR's servers again, which ends a
# 2110 outage (a 1100 outage always ends with 1101 or 1102).
FARM_OK_CODES = frozenset({2104, 2106, 2158})
LINK_DOWN_WITHOUT_RESTORE_CODE = 2110
LIVENESS_IDLE_DEGRADED = 4 * LIVENESS_IDLE
"""Idle seconds before the gateway is probed while IBKR connectivity is lost: silence is
expected then, but a hung gateway must still be caught."""

HINT_REFUSED = (
    "The gateway refused the connection: it is not running, the API port is wrong, "
    "or it is logged out / waiting on 2FA. Retrying in the background."
)
HINT_PEER_CLOSED = (
    "The gateway accepted the connection and closed it before the API session was ready: "
    "the gateway behind the relay (ib-gateway-docker's socat accepts, then closes) is not "
    "running, is starting up, logged out, or waiting on 2FA. Retrying in the background."
)
HINT_HANDSHAKE_TIMEOUT = (
    "The gateway accepted the socket but did not complete the API handshake: it is "
    "starting up, logged out, or waiting on 2FA. Retrying in the background."
)
HINT_CLIENT_ID_IN_USE = (
    "Client id {client_id} is already in use by another API connection. "
    "Set IB_CLIENT_ID to an id no other API client of this gateway uses."
)
HINT_CONNECTIVITY_LOST = (
    "The gateway is up but has lost its connection to IBKR's servers (error {code}). "
    "Requests will fail or stall until IBKR restores it; this usually heals by itself."
)
HINT_DROPPED = "The connection to the gateway dropped. Reconnecting in the background."
HINT_UNRESPONSIVE = (
    "The gateway stopped answering (no data for {idle:.0f}s and no reply to a probe within "
    "{timeout:.0f}s): the gateway process is hung or the network path stalled. The session "
    "was closed; reconnecting in the background."
)
HINT_NO_ACCOUNTS = (
    "The gateway reported no managed accounts, so paper and live accounts cannot be told "
    "apart; trading stays disabled until it does."
)
HINT_NO_ALLOWED_ACCOUNT = (
    "No account is allowed: the login manages several accounts and neither IB_ACCOUNT "
    "(the default account) nor IBKR_MCP_ACCOUNTS (the allowlist) names one of them, so "
    "trading is disabled. Set one of them."
)
HINT_STOPPED = "The connection manager is stopped."
HINT_NOT_STARTED = "The connection manager has not been started."
HINT_READ_ONLY_API = (
    "The gateway's API is in read-only mode (error 321), so orders are rejected. "
    "Untick 'Read-Only API' in the gateway settings (ib-gateway-docker: READ_ONLY_API=no; "
    "if it already is no, set it to yes and then back to no, restarting the gateway each "
    "time)."
)

ResubscribeHook = Callable[[], Awaitable[object]]
DisconnectHook = Callable[[], object]


class _PeerClosedError(Exception):
    """The gateway closed the socket before the API session was ready."""


_FLOAT_DETAILS_FIELDS = ("coupon", "evMultiplier")
"""``ContractDetails`` fields ib_async 2.1.0 types as int although IBKR sends a double."""


def _fix_contract_details_decoding() -> None:
    """Let ib_async 2.1.0 decode fractional ``coupon`` and ``evMultiplier`` values.

    ``Decoder.parse`` converts each ``ContractDetails`` field with the type of its
    dataclass default, and both default to the int ``0``: a bond coupon such as
    ``"4.25"`` (or an economic-value multiplier such as ``"0.5"``) hits ``int()``, the
    message handler raises, and the whole details row is dropped (only logged), so the
    contract looks unknown. A float default makes the decoder use ``float()``.
    Process-wide and idempotent.
    """
    for name in _FLOAT_DETAILS_FIELDS:
        field = ContractDetails.__dataclass_fields__[name]
        if type(field.default) is int:
            field.default = 0.0


def _install_compat_shims(ib: IB) -> None:
    """Patch known ib_async 2.1.0 gaps on this ``IB`` instance.

    * ``Wrapper.userInfo`` drops the white-branding id, so ``reqUserInfoAsync()``
      resolves to ``[]``. The shim resolves the request with the id instead.
    * Fractional bond coupons and economic-value multipliers are not decodable
      (:func:`_fix_contract_details_decoding`, applied to the ``ContractDetails`` class).
    """
    _fix_contract_details_decoding()
    wrapper = ib.wrapper

    def user_info(req_id: int, white_branding_id: str) -> None:
        end_request(wrapper, req_id, white_branding_id)

    wrapper.userInfo = user_info  # type: ignore[method-assign,assignment]


class ConnectionManager:
    """Owns the ``IB`` instance and its connection to the gateway.

    Args:
        settings: Connection and behaviour settings.
        ib_factory: Builds the ``IB`` instance; tests pass a fake.
        accounts: The account scope to refresh on every connect. A private one is
            created when omitted.
        on_resubscribe: Awaited after IBKR reports lost market data (1101) and after a
            reconnect, so streams can be re-requested.
        on_disconnect: Called (synchronously) whenever the session drops, e.g. to mark
            streams stale.
    """

    def __init__(
        self,
        settings: Settings,
        ib_factory: Callable[[], IB] = IB,
        *,
        accounts: AccountScope | None = None,
        on_resubscribe: ResubscribeHook | None = None,
        on_disconnect: DisconnectHook | None = None,
    ) -> None:
        self._settings = settings
        self._accounts = accounts if accounts is not None else AccountScope(settings)
        self._on_resubscribe = on_resubscribe
        self._on_disconnect = on_disconnect
        self._ib = ib_factory()
        _install_compat_shims(self._ib)
        self._track_current_time_answers()
        self._ib.errorEvent += self._on_error
        self._ib.disconnectedEvent += self._on_disconnected
        self._ib.timeoutEvent += self._on_idle

        self._state = ConnectionState.NOT_CONNECTED
        self._hint: str | None = HINT_NOT_STARTED
        self._last_error: ErrorInfo | None = None
        self._connected_since: datetime | None = None
        self._server_version: int | None = None
        self._orders_synced: bool | None = None
        self._api_read_only = False
        self._trading_allowed = False
        self._trading_block: str | None = None
        self._market_data_type: int = settings.market_data_type
        self._connects = 0
        # Per attempt / per session facts the error stream reports before we are CONNECTED.
        self._client_id_clash = False
        self._ibkr_link_down: int | None = None  # the 1100/2110 code while IBKR is cut off
        self._probing = False
        # time.monotonic() of the last currentTime answer (see request_current_time).
        self._current_time_answered: float | None = None
        self._current_time_round_trip: float | None = None
        self._session = 0
        # Locks live while someone holds or awaits them; idle ones are dropped, so keys
        # such as "marketRule-<id>" or per-account P&L keys do not pile up.
        self._request_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

        self._stopping = False
        self._supervisor: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._dropped = asyncio.Event()
        self._first_attempt = asyncio.Event()
        self._connected = asyncio.Event()

    # --- public state -------------------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        """The current connection state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """True while the API session is up (even if IBKR connectivity is degraded)."""
        return (
            self._state in (ConnectionState.CONNECTED, ConnectionState.CONNECTIVITY_LOST)
            and self._ib.isConnected()
        )

    @property
    def ib(self) -> IB:
        """The connected ``IB`` instance.

        Raises:
            NotConnectedError: The session is not up; the message carries the health hint.
        """
        if not self.is_connected:
            raise NotConnectedError(self._not_connected_message())
        return self._ib

    @property
    def accounts(self) -> AccountScope:
        """The account scope refreshed on each connect."""
        return self._accounts

    @property
    def settings(self) -> Settings:
        """The settings this manager was built with."""
        return self._settings

    @property
    def orders_synced(self) -> bool | None:
        """Whether the current session loaded open and completed orders (None before).

        Informational only; it says nothing about whether orders may be placed (see
        :meth:`require_trading`).
        """
        return self._orders_synced

    def request_lock(self, key: str) -> asyncio.Lock:
        """The lock that serializes ib_async requests sharing the fixed key ``key``.

        Some ib_async requests are keyed by a fixed string instead of a request id
        (``"currentTime"``, ``"positions"``...), so a second concurrent call replaces the
        first one's future and the first caller hangs. Hold this lock while creating and
        awaiting such a request (``BaseService._call(..., exclusive=key)`` does).
        """
        lock = self._request_locks.get(key)
        if lock is None:
            lock = self._request_locks[key] = asyncio.Lock()
        return lock

    async def request_current_time(self) -> datetime:
        """Ask the gateway for its clock (``reqCurrentTime``), as a timezone-aware datetime.

        Send every ``reqCurrentTime`` through here. ib_async keys the request by the fixed
        name ``"currentTime"``, so it holds that :meth:`request_lock`; and it waits until
        :data:`CURRENT_TIME_INTERVAL` has passed since the last answer, because the gateway
        silently ignores a request sent sooner. The caller sets the time limit; it covers
        both waits (at most about a second for the spacing).

        Raises:
            NotConnectedError: The session is not up.
            ConnectionError: The connection dropped before the answer (from ib_async).
        """
        ib = self.ib  # fail fast while the session is down
        async with self.request_lock("currentTime"):
            if self._current_time_answered is not None:
                delay = self._current_time_answered + CURRENT_TIME_INTERVAL - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            sent = time.monotonic()
            answer = await ib.reqCurrentTimeAsync()
            self._current_time_round_trip = time.monotonic() - sent
            return answer

    @property
    def current_time_round_trip(self) -> float | None:
        """Seconds the last :meth:`request_current_time` answer took, spacing wait excluded.

        Read it right after awaiting :meth:`request_current_time` (no other ``await`` in
        between) to get that request's own round trip. None before the first answer.
        """
        return self._current_time_round_trip

    @property
    def session(self) -> int:
        """A number that changes with every connection attempt.

        ib_async restarts its request ids on each connection, so a request id is only
        meaningful within the session it was issued in: record this when a stream sends
        its request, and send no cancel for that id once it has changed.
        """
        return self._session

    @property
    def connected_since(self) -> datetime | None:
        """When the current session came up (UTC), or None."""
        return self._connected_since

    @property
    def server_version(self) -> int | None:
        """The TWS API server version of the current session, or None."""
        return self._server_version

    @property
    def market_data_type(self) -> int:
        """The market data type requested for this session (1 live ... 4 delayed-frozen)."""
        return self._market_data_type

    def set_market_data_type(self, market_data_type: int) -> None:
        """Switch between live (1), frozen (2), delayed (3) and delayed-frozen (4) data.

        Applies immediately when connected and is re-applied after every reconnect.

        Raises:
            InvalidRequestError: ``market_data_type`` is not 1-4.
        """
        if market_data_type not in (1, 2, 3, 4):
            raise InvalidRequestError(
                f"market_data_type must be 1 (live), 2 (frozen), 3 (delayed) or "
                f"4 (delayed-frozen), not {market_data_type}"
            )
        self._market_data_type = market_data_type
        if self.is_connected:
            self._ib.reqMarketDataType(market_data_type)

    @property
    def trading_enabled(self) -> bool:
        """True when order tools may run: connected, allowed, and the API is not read-only."""
        return self.is_connected and self._trading_allowed and not self._api_read_only

    def require_trading(self) -> None:
        """Raise the most useful error if orders cannot be placed right now.

        This is the gate for every write path (orders, FA replacement, admin changes):
        call it first. The session's ``readonly`` flag does not stop orders.

        Raises:
            NotConnectedError: The session is not up.
            ConfigurationError: No order toolset is enabled, no account is allowed, or the
                API is read-only.
            LiveTradingDisabledError: A live account is in scope without IBKR_MCP_ALLOW_LIVE,
                or the managed accounts are unknown.
        """
        if not self.is_connected:
            raise NotConnectedError(self._not_connected_message())
        if not self._settings.needs_write_access:
            raise ConfigurationError(
                "Order tools are disabled: the active profile opens a read-only session. "
                "Set IBKR_MCP_PROFILE=trading (or add 'orders' to IBKR_MCP_TOOLSETS)."
            )
        if not self._trading_allowed:
            if self._trading_block == HINT_NO_ALLOWED_ACCOUNT:
                raise ConfigurationError(HINT_NO_ALLOWED_ACCOUNT)
            raise LiveTradingDisabledError(self._trading_block or "Trading is disabled.")
        if self._api_read_only:
            raise ConfigurationError(HINT_READ_ONLY_API)

    def health(self) -> HealthReport:
        """Return a snapshot of the connection's health."""
        connected = self.is_connected
        return HealthReport(
            state=self._state,
            hint=None if self._state is ConnectionState.CONNECTED else self._hint,
            host=self._settings.ib_host,
            port=self._settings.ib_port,
            client_id=self._settings.ib_client_id,
            server_version=self._server_version if connected else None,
            connected_since=self._connected_since if connected else None,
            last_error=self._last_error,
            api_read_only=self._api_read_only,
            accounts=sorted(self._accounts.allowed),
            is_paper=self._accounts.all_paper,
            trading_enabled=self.trading_enabled,
            orders_synced=self._orders_synced if connected else None,
            market_data_type=MARKET_DATA_TYPE_NAMES.get(self._market_data_type),
        )

    # --- lifecycle ------------------------------------------------------------------------

    async def start(self, *, wait: float | None = None) -> None:
        """Start connecting in the background and wait for the first attempt.

        Never raises because the gateway is unreachable; check :meth:`health` or use
        :meth:`wait_connected`.

        Args:
            wait: Longest time (seconds) to wait for the first attempt to finish; ``0``
                returns at once. By default it waits for the attempt itself, up to
                ``6 x IB_CONNECT_TIMEOUT + 1`` seconds: ib_async bounds each of its
                connect steps by ``IB_CONNECT_TIMEOUT`` (TCP connect, API handshake,
                startup requests, executions), and a read-only session adds the order
                sync (open and completed orders) and the executions after it. A server
                passes a short wait, so a half-up gateway cannot hold up its startup.
        """
        if self._supervisor is not None:
            return
        self._stopping = False
        self._first_attempt.clear()
        self._supervisor = asyncio.create_task(self._supervise(), name="ib-connection")
        self._supervisor.add_done_callback(_log_task_failure)
        self._set_state(ConnectionState.CONNECTING, "Connecting to the gateway.")
        # Each connect step is bounded by connect_timeout; this covers all of them.
        budget = 6 * self._settings.connect_timeout + 1
        if wait is not None:
            budget = min(max(wait, 0.0), budget)
        if budget <= 0:
            return
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._first_attempt.wait(), budget)
        except BaseException:
            self._abort()  # cancelled while waiting: do not leave the supervisor running
            raise

    async def stop(self) -> None:
        """Stop reconnecting and disconnect."""
        self._stopping = True
        tasks = [t for t in (self._supervisor, *self._background) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._supervisor = None
        self._background.clear()
        fail_pending_requests(self._ib.wrapper, HINT_STOPPED)
        self._ib.disconnect()
        self._mark_down(ConnectionState.NOT_CONNECTED, HINT_STOPPED)

    def _abort(self) -> None:
        """Synchronous :meth:`stop` for cancellation paths (tasks are cancelled, not awaited)."""
        self._stopping = True
        for task in (self._supervisor, *self._background):
            if task is not None:
                task.cancel()
        self._supervisor = None
        self._background.clear()
        fail_pending_requests(self._ib.wrapper, HINT_STOPPED)
        self._ib.disconnect()
        self._mark_down(ConnectionState.NOT_CONNECTED, HINT_STOPPED)

    async def wait_connected(self, timeout: float | None = None) -> None:
        """Wait until the session is up.

        Raises:
            NotConnectedError: Not connected within ``timeout`` seconds.
        """
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError:
            raise NotConnectedError(self._not_connected_message()) from None

    # --- supervisor ---------------------------------------------------------------------------

    async def _supervise(self) -> None:
        loop = asyncio.get_running_loop()
        delay = INITIAL_BACKOFF
        while not self._stopping:
            if await self._connect_once():
                up_since = loop.time()
                await self._dropped.wait()  # stop() cancels this task before disconnecting
                if loop.time() - up_since >= STABLE_SESSION:
                    delay = INITIAL_BACKOFF
                logger.warning("Gateway connection dropped; reconnecting in %.0fs", delay)
            else:
                logger.info("Retrying the gateway connection in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF)

    async def _connect_once(self) -> bool:
        """Make one connection attempt; return True on success. Never raises (bar cancel)."""
        settings = self._settings
        readonly = not (settings.needs_write_access and settings.allow_live)
        self._session += 1  # ib_async restarts its request ids with every connection
        self._set_state(ConnectionState.CONNECTING, "Connecting to the gateway.")
        self._client_id_clash = False
        self._ibkr_link_down = None
        self._dropped.clear()
        self._ib.RaiseRequestErrors = False  # startup sync must not abort on one bad request
        try:
            await self._connect(readonly)
        except _PeerClosedError as exc:
            self._fail(ConnectionState.NOT_ACCEPTING, self._peer_closed_hint(), exc)
        except ConnectionRefusedError as exc:
            self._fail(ConnectionState.NOT_ACCEPTING, HINT_REFUSED, exc)
        except TimeoutError as exc:
            self._fail(ConnectionState.NOT_ACCEPTING, self._handshake_timeout_hint(), exc)
        except OSError as exc:
            hint = f"Cannot reach the gateway at {settings.ib_host}:{settings.ib_port}: {exc}."
            self._fail(ConnectionState.NOT_ACCEPTING, hint, exc)
        except Exception as exc:
            logger.exception("Unexpected error while connecting to the gateway")
            self._fail(ConnectionState.NOT_CONNECTED, f"Connection failed: {exc!r}.", exc)
        else:
            if self._dropped.is_set():  # dropped before the session could be set up
                self._mark_down(ConnectionState.NOT_CONNECTED, HINT_DROPPED)
                return True
            try:
                await self._on_connected(readonly)
            except Exception as exc:
                logger.exception("Setting up the gateway session failed")
                self._ib.disconnect()
                self._fail(ConnectionState.NOT_CONNECTED, f"Session setup failed: {exc!r}.", exc)
                return False
            return True
        finally:
            self._first_attempt.set()
        return False

    async def _connect(self, readonly: bool) -> None:
        """Run ``connectAsync``, failing fast if the gateway closes the socket meanwhile.

        ib_async waits the full connect timeout for the handshake even when the peer has
        already closed the socket (which is what a relay in front of a stopped gateway
        does), so the socket's ``disconnected`` event is raced against the connect.
        """
        settings = self._settings
        # A read-only connect loads no orders, so ib_async's execution fetch would find
        # no order to attach each fill to; _on_connected fetches executions afterwards.
        fields = StartupFetchALL & ~StartupFetch.EXECUTIONS if readonly else StartupFetchALL
        connect = asyncio.ensure_future(
            self._ib.connectAsync(
                settings.ib_host,
                settings.ib_port,
                clientId=settings.ib_client_id,
                timeout=settings.connect_timeout,
                readonly=readonly,
                account=settings.ib_account or "",
                fetchFields=fields,
            )
        )
        socket_closed = getattr(getattr(self._ib.client, "conn", None), "disconnected", None)
        if socket_closed is None:
            await connect
            return
        closed: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def on_closed(message: str = "") -> None:
            if not closed.done():
                closed.set_result(message)

        socket_closed += on_closed
        try:
            await asyncio.wait({connect, closed}, return_when=asyncio.FIRST_COMPLETED)
            if connect.done():
                connect.result()  # re-raises the connect failure, if any
                return
            connect.cancel()
            await asyncio.gather(connect, return_exceptions=True)
            raise _PeerClosedError(closed.result() or "peer closed the connection")
        finally:
            socket_closed -= on_closed
            if not connect.done():
                connect.cancel()
            closed.cancel()

    async def _on_connected(self, readonly: bool) -> None:
        ib = self._ib
        ib.RaiseRequestErrors = True  # request errors surface as RequestError from here on
        self._orders_synced = not readonly
        self._server_version = ib.client.serverVersion()
        self._connected_since = utc_now()
        self._api_read_only = False
        if self._last_error is not None and self._last_error.code in (-1, CLIENT_ID_IN_USE_CODE):
            self._last_error = None  # it described a failed attempt; this one worked
        self._accounts.refresh(ib.managedAccounts())
        ib.reqMarketDataType(self._market_data_type)
        self._evaluate_trading()
        if self._trading_allowed and readonly:
            synced = await self._sync_orders()
            if self._dropped.is_set():  # dropped while syncing; the supervisor reconnects
                self._mark_down(ConnectionState.NOT_CONNECTED, HINT_DROPPED)
                return
            self._orders_synced = synced
        if readonly:
            await self._fetch_executions()
            if self._dropped.is_set():
                self._mark_down(ConnectionState.NOT_CONNECTED, HINT_DROPPED)
                return
        if self._ibkr_link_down is not None:
            # IBKR reported the link down while the session was being set up.
            code = self._ibkr_link_down
            self._set_state(
                ConnectionState.CONNECTIVITY_LOST, HINT_CONNECTIVITY_LOST.format(code=code)
            )
        else:
            self._set_state(ConnectionState.CONNECTED, None)
        self._connected.set()
        ib.setTimeout(LIVENESS_IDLE)
        logger.info(
            "Connected to the gateway (server version %s, %s session)",
            self._server_version,
            "read-only" if readonly else "read-write",
        )
        if self._connects and self._on_resubscribe is not None:
            self._spawn(self._resubscribe(), "resubscribe-after-reconnect")
        self._connects += 1

    def _evaluate_trading(self) -> None:
        settings = self._settings
        live = self._accounts.live_accounts_in_scope
        if not settings.needs_write_access:
            self._trading_allowed = False
            self._trading_block = "No order, advisor or admin toolset is enabled."
        elif not self._accounts.ready:
            self._trading_allowed = False
            self._trading_block = HINT_NO_ACCOUNTS
            logger.warning("%s", HINT_NO_ACCOUNTS)
        elif live and not settings.allow_live:
            self._trading_allowed = False
            self._trading_block = (
                f"Live account(s) {', '.join(live)} are in scope and IBKR_MCP_ALLOW_LIVE is not "
                "set, so trading is disabled and the session stays read-only. Use a paper "
                "login, or set IBKR_MCP_ALLOW_LIVE=true to trade real money."
            )
            logger.warning("%s", self._trading_block)
        elif not self._accounts.allowed:
            self._trading_allowed = False
            self._trading_block = HINT_NO_ALLOWED_ACCOUNT
            logger.warning("%s", self._trading_block)
        else:
            self._trading_allowed = True
            self._trading_block = None

    async def _sync_orders(self) -> bool:
        """Load open and completed orders, which a read-only connect skips.

        Returns True when both loaded; failures are logged, not raised.
        """
        ib = self._ib
        timeout = self._settings.connect_timeout
        ok = True
        for name, key, request in (
            ("open orders", "openOrders", ib.reqOpenOrdersAsync),
            ("completed orders", "completedOrders", lambda: ib.reqCompletedOrdersAsync(False)),
        ):
            try:
                async with asyncio.timeout(timeout), self.request_lock(key):
                    await request()
            except Exception:
                ok = False
                logger.warning("Syncing %s after connect failed", name, exc_info=True)
        return ok

    async def _fetch_executions(self) -> None:
        """Load the day's executions once the orders are in (read-only connects).

        ib_async attaches each execution to the trade it belongs to, which must be known
        by then; failures are logged, not raised.
        """
        try:
            async with asyncio.timeout(self._settings.connect_timeout):
                await self._ib.reqExecutionsAsync()
        except Exception:
            logger.warning("Loading executions after connect failed", exc_info=True)

    async def _resubscribe(self) -> None:
        if self._on_resubscribe is not None:
            await self._on_resubscribe()

    # --- event handlers ---------------------------------------------------------------------

    def _on_disconnected(self) -> None:
        self._dropped.set()
        if self._on_disconnect is not None:
            try:
                self._on_disconnect()
            except Exception:
                logger.exception("The on_disconnect hook failed")
        if self._stopping or self._state is ConnectionState.CONNECTING:
            return
        self._mark_down(ConnectionState.NOT_CONNECTED, HINT_DROPPED)

    def _on_error(self, req_id: int, code: int, message: str, _contract: object) -> None:
        if code in CONNECTIVITY_LOST_CODES | CONNECTIVITY_RESTORED_CODES | FARM_OK_CODES:
            self._on_connectivity(code)
        elif code == READ_ONLY_CODE and "read-only" in message.lower():
            if not self._api_read_only:
                logger.warning("%s", HINT_READ_ONLY_API)
            self._api_read_only = True
        elif code == CLIENT_ID_IN_USE_CODE:
            self._client_id_clash = True
            self._hint = HINT_CLIENT_ID_IN_USE.format(client_id=self._settings.ib_client_id)
        elif code in NOT_CONNECTED_CODES:
            hint = f"{message} (error {code}). Reconnecting in the background."
            if self._ib.isConnected():
                # ib_async never raises these itself; if the gateway sends one, the session
                # is unusable: close it so the supervisor reconnects. Not from inside the
                # decoder's call stack, though.
                asyncio.get_running_loop().call_soon(self._force_reconnect, hint)
            else:
                self._mark_down(ConnectionState.NOT_CONNECTED, hint)

        if self._is_connection_level(req_id, code):
            self._last_error = ErrorInfo(code=code, message=message, at=utc_now())

    def _on_connectivity(self, code: int) -> None:
        """Track IBKR connectivity notices (1100/2110 lost, 1101/1102 and farm OK back)."""
        if code in CONNECTIVITY_LOST_CODES:
            self._ibkr_link_down = code  # remembered even while the session is being set up
            if self.is_connected:
                self._set_state(
                    ConnectionState.CONNECTIVITY_LOST, HINT_CONNECTIVITY_LOST.format(code=code)
                )
            return
        if code in FARM_OK_CODES and self._ibkr_link_down != LINK_DOWN_WITHOUT_RESTORE_CODE:
            return
        # 1101/1102, or a farm reconnecting after 2110 (which has no "restored" message).
        self._ibkr_link_down = None
        if self._state is ConnectionState.CONNECTIVITY_LOST:
            self._set_state(ConnectionState.CONNECTED, None)
        if code == DATA_LOST_CODE and self._on_resubscribe is not None:
            self._spawn(self._resubscribe(), "resubscribe-after-1101")

    @staticmethod
    def _is_connection_level(req_id: int, code: int) -> bool:
        if code in INFORMATIONAL_CODES:
            return False
        return req_id == -1 or code in (READ_ONLY_CODE, CLIENT_ID_IN_USE_CODE)

    def _on_idle(self, idle: float) -> None:
        """ib_async's ``timeoutEvent``: nothing arrived for ``LIVENESS_IDLE`` seconds."""
        if self._stopping or self._probing or not self._ib.isConnected():
            return
        if self._state is ConnectionState.CONNECTIVITY_LOST and idle < LIVENESS_IDLE_DEGRADED:
            # Cut off from IBKR (1100/2110): silence is expected for a while, but the
            # gateway itself must still answer, so probe after a longer quiet spell.
            self._ib.setTimeout(LIVENESS_IDLE_DEGRADED)
            return
        if self._state not in (ConnectionState.CONNECTED, ConnectionState.CONNECTIVITY_LOST):
            self._ib.setTimeout(LIVENESS_IDLE)
            return
        self._probing = True
        self._spawn(self._probe(idle), "liveness-probe")

    async def _probe(self, idle: float) -> None:
        """Ask for the server time; close the session if the gateway does not answer."""
        try:
            async with asyncio.timeout(LIVENESS_PROBE_TIMEOUT):
                await self.request_current_time()
        except Exception as exc:
            if self._stopping or not self._ib.isConnected():
                return
            hint = HINT_UNRESPONSIVE.format(idle=idle, timeout=LIVENESS_PROBE_TIMEOUT)
            self._last_error = ErrorInfo(
                code=-1, message=f"liveness probe failed: {exc!r}", at=utc_now()
            )
            self._force_reconnect(hint)
        else:
            if self._ib.isConnected():
                self._ib.setTimeout(LIVENESS_IDLE)
        finally:
            self._probing = False

    def _track_current_time_answers(self) -> None:
        """Note when each ``currentTime`` answer arrives, for :meth:`request_current_time`.

        Hooks the wrapper callback rather than the request, so an answer that arrives
        after its caller gave up still counts: the gateway's one-second window restarts
        with every answer it sends.
        """
        wrapper = self._ib.wrapper
        answer = wrapper.currentTime

        def current_time(server_time: int) -> None:
            self._current_time_answered = time.monotonic()
            answer(server_time)

        wrapper.currentTime = current_time  # type: ignore[method-assign,assignment]

    def _force_reconnect(self, hint: str) -> None:
        """Close a session that is up but unusable; the supervisor then reconnects."""
        if self._stopping or not self._ib.isConnected():
            return
        logger.warning("Closing the gateway session: %s", hint)
        # ib_async's disconnect() drops pending request futures without failing them;
        # fail them first so in-flight calls end now instead of at their timeout.
        fail_pending_requests(self._ib.wrapper, hint)
        self._ib.disconnect()  # emits disconnectedEvent, which wakes the supervisor
        self._mark_down(ConnectionState.NOT_CONNECTED, hint)

    # --- helpers -----------------------------------------------------------------------------

    def _set_state(self, state: ConnectionState, hint: str | None) -> None:
        if state is not self._state:
            logger.debug("Connection state %s -> %s", self._state, state)
        self._state = state
        self._hint = hint

    def _mark_down(self, state: ConnectionState, hint: str) -> None:
        self._set_state(state, hint)
        self._connected.clear()
        self._connected_since = None

    def _fail(self, state: ConnectionState, hint: str, exc: BaseException) -> None:
        if not self._client_id_clash:  # else last_error already holds this attempt's 326
            self._last_error = ErrorInfo(code=-1, message=repr(exc), at=utc_now())
        logger.warning("Gateway connection failed: %s", hint)
        self._mark_down(state, hint)

    def _handshake_timeout_hint(self) -> str:
        if self._client_id_clash:
            return HINT_CLIENT_ID_IN_USE.format(client_id=self._settings.ib_client_id)
        return HINT_HANDSHAKE_TIMEOUT

    def _peer_closed_hint(self) -> str:
        if self._client_id_clash:
            return HINT_CLIENT_ID_IN_USE.format(client_id=self._settings.ib_client_id)
        return HINT_PEER_CLOSED

    def _not_connected_message(self) -> str:
        hint = self._hint or "The gateway connection is not up."
        return f"Not connected to the gateway ({self._state.value}). {hint}"

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(_log_task_failure)

    @staticmethod
    def client_version_range() -> str:
        """The TWS API versions ib_async speaks, as ``'min..max'``."""
        return f"{Client.MinClientVersion}..{Client.MaxClientVersion}"


def _log_task_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("Background task %s failed", task.get_name(), exc_info=task.exception())
