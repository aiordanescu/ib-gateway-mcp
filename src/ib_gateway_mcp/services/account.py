"""Account values, positions, portfolio, P&L, executions and orders on record.

ib_async 2.1.0 behaviours this service works around:

* **Account updates cover one account at a time** (``reqAccountUpdates``): subscribing to
  account B silently unsubscribes A, and ``IB.portfolio()`` only has data for the
  subscribed account. ib_async subscribes the connect-time account (``IB_ACCOUNT`` or
  the only managed account). :meth:`AccountService.portfolio` switches under the
  ``"accountValues"`` lock, reads what IBKR sends for the other account, and switches
  back.
* **The account summary** is one standing ``reqAccountSummary`` subscription (group
  "All", fixed tags) started on first use; IBKR allows two per client, so the first
  request is serialized.
* **Caches mix accounts**: ``accountValues()``, ``accountSummary`` and ``positions()``
  hold every managed account (and model-code rows), so every read is filtered by account.
* **reqExecutions returns fresh ``Fill`` objects without commissions** for executions the
  session has already seen; the commission report lives on the cached fill, so it is
  merged back by execution id.
* **Completed orders drop their ``OrderState``** (completion time and text): the
  ``completedOrder`` callback is wrapped for the duration of the request to keep them.
* **reqPositionsMulti has no handler** (``positionMulti``/``positionMultiEnd`` are
  stubs), so the callbacks are hooked for the duration of one request.
* **P&L is a subscription** (``reqPnL``/``reqPnLSingle``, one per key, asserting there is
  no duplicate): the tools subscribe, wait for the first update and cancel.
* **A read-only API refuses some order reads without a request id**: with the gateway's
  Read-Only API setting on, IBKR answers ``reqOpenOrders`` and ``reqCompletedOrders`` with
  error 321 under request id -1, which ib_async logs as a warning of no request, so the
  request would wait out its whole timeout. The order reads listen for that 321 and fail
  at once with an :class:`IbApiError` (code 321) that says what to change. Once the
  connection knows the API is read-only (``health().api_read_only``, reset on every
  reconnect), :meth:`AccountService.completed_orders` refuses without asking, and
  :meth:`AccountService.open_orders` for this client alone reads ``reqAllOpenOrders``
  (which a read-only API was seen to answer) filtered by client id, saying so in
  ``note``. A 321 while ``reqAllOpenOrders`` is pending gets
  :attr:`AccountService.read_only_grace` seconds for the answer first: without a request
  id it may belong to another request.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ib_async import (
    IB,
    AccountValue,
    Contract,
    ExecutionFilter,
    OrderState,
    OrderStatus,
    PnL,
    PnLSingle,
    PortfolioItem,
    Trade,
)

from ib_gateway_mcp._ib_compat import end_request, subscription_request_id
from ib_gateway_mcp._util import (
    best_effort,
    clamp_limit,
    clean_float,
    clean_int,
    clean_str,
    contract_to_out,
    ensure_utc,
    is_informational,
    truncate,
    utc_now,
)
from ib_gateway_mcp.errors import (
    IbApiError,
    IbGatewayMcpError,
    InvalidRequestError,
    NotFoundError,
    RequestTimeoutError,
)
from ib_gateway_mcp.models.account import (
    AccountPnl,
    AccountSummary,
    AccountValueList,
    CompletedOrder,
    CompletedOrderList,
    ExecutionList,
    ExecutionOut,
    OpenOrderList,
    Portfolio,
    PositionList,
    PositionOut,
    PositionPnl,
)
from ib_gateway_mcp.models.common import Action, ContractSpec
from ib_gateway_mcp.services._account_rows import (
    _completed_order_row,
    _execution_row,
    _filter_tags,
    _has_values,
    _open_order_row,
    _portfolio_row,
    _position_row,
    _sorted_values,
    _trade_key,
    _value_out,
)
from ib_gateway_mcp.services._hooks import hooked
from ib_gateway_mcp.services.base import BaseService, describe_spec

if TYPE_CHECKING:
    from ib_gateway_mcp.gateway import Gateway

__all__ = [
    "ACCOUNT_VALUES_DEFAULT_LIMIT",
    "ACCOUNT_VALUES_MAX_LIMIT",
    "COMPLETED_ORDERS_DEFAULT_LIMIT",
    "COMPLETED_ORDERS_MAX_LIMIT",
    "EXECUTIONS_DEFAULT_LIMIT",
    "EXECUTIONS_MAX_LIMIT",
    "PNL_WAIT",
    "READ_ONLY_API_CODE",
    "READ_ONLY_GRACE",
    "AccountService",
]

logger = logging.getLogger(__name__)

ACCOUNT_VALUES_DEFAULT_LIMIT = 200
ACCOUNT_VALUES_MAX_LIMIT = 1000
EXECUTIONS_DEFAULT_LIMIT = 100
EXECUTIONS_MAX_LIMIT = 1000
COMPLETED_ORDERS_DEFAULT_LIMIT = 100
COMPLETED_ORDERS_MAX_LIMIT = 1000

PNL_WAIT = 3.0
"""Seconds to wait for IBKR's first P&L update (it sends one about every second)."""

READ_ONLY_API_CODE = 321
"""The TWS API code (with "read-only" in its text) for a request a read-only API refuses."""
READ_ONLY_GRACE = 2.0
"""Seconds ``reqAllOpenOrders`` still waits for its answer after a read-only 321 (reqId -1),
which may belong to another request."""

_ACCOUNT_UPDATES_KEY = "accountValues"
"""ib_async's fixed key for ``reqAccountUpdatesAsync`` (see ``SHARED_REQUEST_KEYS``)."""
_ACCOUNT_SUMMARY_KEY = "accountSummary"
"""Serializes the first ``accountSummaryAsync`` call, which opens the standing subscription."""
_ACCOUNT_UPDATES_MULTI_KEY = "accountUpdatesMulti"
"""Prefix of the per-account lock around opening a standing ``reqAccountUpdatesMulti``."""
_POSITIONS_MULTI_KEY = "positionsMulti"
"""Serializes ``reqPositionsMulti`` requests, whose callbacks are hooked per request."""
_OPEN_ORDERS_KEY = "openOrders"
"""ib_async's fixed key for ``reqOpenOrdersAsync`` and ``reqAllOpenOrdersAsync``."""
_COMPLETED_ORDERS_KEY = "completedOrders"
"""ib_async's fixed key for ``reqCompletedOrdersAsync``."""


_SUMMARY_FIELDS = {
    "NetLiquidation": "net_liquidation",
    "TotalCashValue": "total_cash_value",
    "SettledCash": "settled_cash",
    "BuyingPower": "buying_power",
    "AvailableFunds": "available_funds",
    "ExcessLiquidity": "excess_liquidity",
    "EquityWithLoanValue": "equity_with_loan_value",
    "GrossPositionValue": "gross_position_value",
    "InitMarginReq": "init_margin_req",
    "MaintMarginReq": "maint_margin_req",
    "SMA": "sma",
    "Cushion": "cushion",
    "Leverage": "leverage",
}
"""Summary tags surfaced as headline fields of :class:`AccountSummary`."""

# --- helpers ------------------------------------------------------------------------------


class AccountService(BaseService):
    """Account values, positions, portfolio, P&L, executions and orders on record.

    Every method takes an optional ``account`` (resolved through the account scope:
    the default account when omitted, :class:`AccountNotAllowedError` outside the
    allowlist) and returns rows that carry their account.
    """

    pnl_wait: float = PNL_WAIT
    """Seconds :meth:`pnl` and :meth:`position_pnl` wait for IBKR's first update."""

    read_only_grace: float = READ_ONLY_GRACE
    """Seconds ``reqAllOpenOrders`` still waits for its answer after a read-only 321."""

    def __init__(self, gateway: Gateway) -> None:
        super().__init__(gateway)
        self._updates_session: datetime | None = None
        self._updates_account: str | None = None
        self._multi_session: datetime | None = None
        self._multi_opened: set[str] = set()

    # --- summary and values ------------------------------------------------------------

    async def account_summary(
        self, account: str | None = None, *, tags: Sequence[str] | None = None
    ) -> AccountSummary:
        """Return headline balances and margin figures for an account.

        Uses ib_async's standing account-summary subscription (started on first use; IBKR
        refreshes it about every 3 minutes). ``tags`` narrows ``values`` to those tags
        (case-insensitive); the headline fields are always filled.

        Raises:
            InvalidRequestError: A tag is not in the account's summary (valid tags listed).
            NotFoundError: IBKR sent no summary rows for the account.
        """
        acct = self.accounts.resolve(account)
        ib = self.ib
        rows = await self._call(
            lambda: ib.accountSummaryAsync(acct),
            what=f"the account summary of {acct}",
            exclusive=_ACCOUNT_SUMMARY_KEY,
        )
        values = _sorted_values(self.accounts.filter(rows or [], acct))
        if not values:
            raise NotFoundError(
                f"IBKR sent no account summary for {acct}. Right after login the summary can "
                "take a few seconds; try again, or use get_account_values."
            )
        headline: dict[str, Any] = {}
        base_currency: str | None = None
        for value in values:
            if value.modelCode or value.currency == "BASE":
                continue
            field = _SUMMARY_FIELDS.get(value.tag)
            if field is not None and field not in headline:
                headline[field] = clean_float(value.value)
                if value.tag == "NetLiquidation":
                    base_currency = clean_str(value.currency)
            elif value.tag == "DayTradesRemaining" and "day_trades_remaining" not in headline:
                headline["day_trades_remaining"] = clean_int(value.value)
        selected = _filter_tags(values, tags)
        return AccountSummary(
            account=acct,
            base_currency=base_currency,
            values=[_value_out(value) for value in selected],
            as_of=utc_now(),
            **headline,
        )

    async def account_values(
        self,
        account: str | None = None,
        *,
        model_code: str | None = None,
        tags: Sequence[str] | None = None,
        currency: str | None = None,
        limit: int | None = None,
    ) -> AccountValueList:
        """Return the full key/value account data (every tag, per currency).

        Without ``model_code`` the values come from ib_async's standing account-update
        subscriptions (opened for the account on demand if missing). With ``model_code``
        a one-off ``reqAccountUpdatesMulti`` fetches the advisor model's values and is
        cancelled afterwards. Filters: ``tags`` (case-insensitive) and ``currency``
        (e.g. USD, or BASE for base-currency totals).

        Raises:
            InvalidRequestError: A tag matches nothing in the account's data.
            NotFoundError: IBKR sent no values (for that model code).
        """
        acct = self.accounts.resolve(account)
        cap = clamp_limit(
            limit, default=ACCOUNT_VALUES_DEFAULT_LIMIT, maximum=ACCOUNT_VALUES_MAX_LIMIT
        )
        model = clean_str(model_code)
        ib = self.ib
        if model is not None:
            values = await self._model_account_values(ib, acct, model)
        else:
            values = self._cached_account_values(ib, acct)
            if not values:
                values = await self._open_account_values(ib, acct)
        if not values:
            target = f"model {model} in account {acct}" if model else f"account {acct}"
            raise NotFoundError(
                f"IBKR sent no account values for {target}."
                + (" Check the model code (get_fa_config lists advisor setups)." if model else "")
            )
        values = _filter_tags(_sorted_values(values), tags)
        wanted_currency = clean_str(currency)
        if wanted_currency is not None:
            values = [v for v in values if v.currency.upper() == wanted_currency.upper()]
        rows, truncated = truncate(values, cap)
        return AccountValueList(
            account=acct,
            model_code=model,
            values=[_value_out(value) for value in rows],
            total=len(values),
            truncated=truncated,
            as_of=utc_now(),
        )

    def _cached_account_values(self, ib: IB, account: str) -> list[AccountValue]:
        return [
            v for v in self.accounts.filter(ib.accountValues(account), account) if not v.modelCode
        ]

    async def _open_account_values(self, ib: IB, account: str) -> list[AccountValue]:
        """Open a standing ``reqAccountUpdatesMulti`` for ``account`` and read the cache.

        ib_async opens one per account at connect, but only for logins with at most 50
        accounts. The subscription is never cancelled (it keeps the cache current), so it
        is opened at most once per session and account; concurrent callers wait for it.
        """
        async with self.connection.request_lock(f"{_ACCOUNT_UPDATES_MULTI_KEY}:{account}"):
            values = self._cached_account_values(ib, account)
            session = self.connection.connected_since
            if session != self._multi_session:
                self._multi_session = session
                self._multi_opened = set()
            if values or account in self._multi_opened:
                return values
            self._multi_opened.add(account)
            try:
                await self._call(
                    lambda: ib.reqAccountUpdatesMultiAsync(account),
                    what=f"account values of {account}",
                )
            except IbApiError:
                self._multi_opened.discard(account)  # IBKR closed it; a retry may open it
                raise
            return self._cached_account_values(ib, account)

    async def _model_account_values(
        self, ib: IB, account: str, model_code: str
    ) -> list[AccountValue]:
        """One-off ``reqAccountUpdatesMulti`` for an advisor model, cancelled at the end."""
        what = f"account values of model {model_code} in account {account}"
        collected: dict[tuple[str, str, str], AccountValue] = {}

        def on_value(value: AccountValue) -> None:
            if value.modelCode == model_code and value.account == account:
                collected[(value.account, value.tag, value.currency)] = value

        req_id = self._new_req_id(ib, what)
        ib.accountValueEvent += on_value
        try:
            await self._one_off_request(
                ib,
                req_id,
                what=what,
                send=lambda: ib.client.reqAccountUpdatesMulti(req_id, account, model_code, False),
                cancel=lambda: ib.client.cancelAccountUpdatesMulti(req_id),
            )
        finally:
            ib.accountValueEvent -= on_value
        return list(collected.values())

    # --- positions and portfolio ---------------------------------------------------------

    async def positions(
        self, account: str | None = None, *, model_code: str | None = None
    ) -> PositionList:
        """Return an account's positions: contract, quantity and average cost.

        Without ``model_code`` this reads ib_async's streaming position cache (refreshed
        with ``reqPositions`` if it is empty). With ``model_code`` it runs a one-off
        ``reqPositionsMulti`` for the advisor model; only rows of ``account`` are kept.
        """
        acct = self.accounts.resolve(account)
        model = clean_str(model_code)
        ib = self.ib
        if model is not None:
            rows = await self._model_positions(ib, acct, model)
        else:
            cached = ib.positions()
            if not cached:
                cached = await self._call(
                    ib.reqPositionsAsync, what="positions", exclusive="positions"
                )
            latest = {p.contract.conId: p for p in self.accounts.filter(cached or [], acct)}
            rows = [
                _position_row(p.account, p.contract, p.position, p.avgCost, None)
                for p in latest.values()
                if p.position
            ]
        return PositionList(account=acct, model_code=model, positions=rows, as_of=utc_now())

    async def _model_positions(self, ib: IB, account: str, model_code: str) -> list[PositionOut]:
        what = f"positions of model {model_code} in account {account}"
        wrapper = ib.wrapper
        rows: list[PositionOut] = []
        async with self.connection.request_lock(_POSITIONS_MULTI_KEY):
            req_id = self._new_req_id(ib, what)

            def on_position(  # noqa: PLR0917 (the TWS callback's signature)
                row_req_id: int,
                row_account: str,
                row_model: str,
                contract: Contract,
                position: float,
                avg_cost: float,
            ) -> None:
                if row_req_id == req_id and position and row_account == account:
                    rows.append(_position_row(row_account, contract, position, avg_cost, row_model))

            def on_end(end_req_id: int) -> None:
                if end_req_id == req_id:
                    end_request(wrapper, req_id)

            with (
                hooked(wrapper, "positionMulti", on_position),
                hooked(wrapper, "positionMultiEnd", on_end),
            ):
                await self._one_off_request(
                    ib,
                    req_id,
                    what=what,
                    send=lambda: ib.client.reqPositionsMulti(req_id, account, model_code),
                    cancel=lambda: ib.client.cancelPositionsMulti(req_id),
                )
        return rows

    async def portfolio(self, account: str | None = None) -> Portfolio:
        """Return positions with market price, market value and P&L.

        IBKR streams portfolio data for one account at a time. For the account ib_async
        subscribed at connect this reads the live cache; for another allowed account it
        switches the subscription, keeps what IBKR sends, and switches back.
        """
        acct = self.accounts.resolve(account)
        items = await self._portfolio_items(acct)
        rows = [_portfolio_row(item) for item in self.accounts.filter(items, acct)]
        return Portfolio(account=acct, items=rows, as_of=utc_now())

    async def _portfolio_items(self, account: str) -> list[PortfolioItem]:
        ib = self.ib
        async with self.connection.request_lock(_ACCOUNT_UPDATES_KEY):
            if self._streaming_account() == account:
                return list(ib.portfolio(account))
            items = await self._subscribe_account_updates(ib, account)
            home = self._home_account()
            if home is not None and home != account:
                try:
                    await self._subscribe_account_updates(ib, home)
                except IbGatewayMcpError:
                    logger.warning(
                        "Could not switch account updates back to the default account",
                        exc_info=True,
                    )
            return items

    async def _subscribe_account_updates(self, ib: IB, account: str) -> list[PortfolioItem]:
        """Point ``reqAccountUpdates`` at ``account``; return the portfolio IBKR sends.

        The caller holds the ``"accountValues"`` lock.
        """
        collected: dict[int, PortfolioItem] = {}

        def on_item(item: PortfolioItem) -> None:
            if item.account == account:
                collected[item.contract.conId] = item

        self._updates_account = None  # unknown until IBKR confirms the switch
        ib.updatePortfolioEvent += on_item
        try:
            await self._call(
                lambda: ib.reqAccountUpdatesAsync(account),
                what=f"account updates for {account}",
            )
        finally:
            ib.updatePortfolioEvent -= on_item
        self._updates_account = account
        return [item for item in collected.values() if item.position]

    def _streaming_account(self) -> str | None:
        """The account ``reqAccountUpdates`` currently streams, if known."""
        session = self.connection.connected_since
        if session != self._updates_session:
            # A new session: ib_async subscribed the connect-time account again.
            self._updates_session = session
            self._updates_account = self._home_account()
        return self._updates_account

    def _home_account(self) -> str | None:
        """The account ib_async subscribes to account updates at connect, if valid."""
        configured = clean_str(self.settings.ib_account)
        managed = self.accounts.managed
        if configured:
            return configured if configured in managed else None
        return managed[0] if len(managed) == 1 else None

    # --- P&L -------------------------------------------------------------------------------

    async def pnl(self, account: str | None = None, *, model_code: str | None = None) -> AccountPnl:
        """Return the account's (or an advisor model's) daily, unrealized and realized P&L.

        Subscribes with ``reqPnL``, waits up to :attr:`pnl_wait` seconds for IBKR's first
        update, then cancels. Reuses a subscription that is already open.

        Raises:
            RequestTimeoutError: No update arrived in time.
            IbApiError: IBKR rejected the request.
        """
        acct = self.accounts.resolve(account)
        model = clean_str(model_code) or ""
        ib = self.ib
        what = f"P&L of account {acct}" + (f" model {model}" if model else "")

        def matches(entry: PnL) -> bool:
            return entry.account == acct and entry.modelCode == model

        def subscribe() -> int | None:
            ib.reqPnL(acct, model)
            return _req_id_of(ib, "pnlKey2ReqId", (acct, model))

        entry, as_of = await self._pnl_once(
            lock=f"pnl:{acct}:{model}",
            event=ib.pnlEvent,
            matches=matches,
            current=lambda: ib.pnl(acct),
            has_values=lambda e: _has_values(e.dailyPnL, e.unrealizedPnL, e.realizedPnL),
            subscribe=subscribe,
            unsubscribe=lambda: ib.cancelPnL(acct, model),
            what=what,
            hint=_PNL_HINT,
        )
        return AccountPnl(
            account=acct,
            model_code=model or None,
            daily_pnl=clean_float(entry.dailyPnL),
            unrealized_pnl=clean_float(entry.unrealizedPnL),
            realized_pnl=clean_float(entry.realizedPnL),
            as_of=as_of,
        )

    async def position_pnl(
        self,
        contract: ContractSpec,
        account: str | None = None,
        *,
        model_code: str | None = None,
    ) -> PositionPnl:
        """Return the P&L of one position (``reqPnLSingle``, one update, then cancelled).

        Raises:
            InvalidRequestError: ``contract`` is a combo (BAG); IBKR keeps P&L per leg.
            NotFoundError, AmbiguousContractError: The contract cannot be resolved.
            NotFoundError: IBKR reports no position and no P&L for it in the account, or
                sends nothing in time for a contract the account does not hold.
            RequestTimeoutError: No update arrived in time for a position the account holds.
        """
        acct = self.accounts.resolve(account)
        model = clean_str(model_code) or ""
        if contract.sec_type == "BAG":
            raise InvalidRequestError(
                "P&L is kept per position, and a combo (BAG) is not one: ask for each leg by "
                "its con_id (get_positions lists them)."
            )
        qualified = await self.qualify(contract)
        con_id = qualified.conId
        ib = self.ib
        label = describe_spec(contract)
        what = f"P&L of {label} in account {acct}"

        def matches(entry: PnLSingle) -> bool:
            return entry.account == acct and entry.modelCode == model and entry.conId == con_id

        not_found = (
            f"Account {acct} has no position and no P&L today in {label}. "
            "get_positions lists what the account holds."
        )
        held = any(p.contract.conId == con_id for p in ib.positions(acct))

        def subscribe() -> int | None:
            ib.reqPnLSingle(acct, model, con_id)
            return _req_id_of(ib, "pnlSingleKey2ReqId", (acct, model, con_id))

        try:
            entry, as_of = await self._pnl_once(
                lock=f"pnl_single:{acct}:{model}:{con_id}",
                event=ib.pnlSingleEvent,
                matches=matches,
                current=lambda: ib.pnlSingle(acct),
                has_values=lambda e: _has_values(
                    e.dailyPnL, e.unrealizedPnL, e.realizedPnL, e.value
                ),
                subscribe=subscribe,
                unsubscribe=lambda: ib.cancelPnLSingle(acct, model, con_id),
                what=what,
                hint=_PNL_HINT,
            )
        except RequestTimeoutError:
            # IBKR never sends a P&L update for a contract the account neither holds nor
            # traded today, so for one it doesn't hold the silence means there is none.
            if held:
                raise
            raise NotFoundError(not_found) from None
        position = clean_float(entry.position)
        values = (
            clean_float(entry.dailyPnL),
            clean_float(entry.unrealizedPnL),
            clean_float(entry.realizedPnL),
            clean_float(entry.value),
        )
        if not position and all(value is None for value in values):
            raise NotFoundError(not_found)
        daily, unrealized, realized, market_value = values
        return PositionPnl(
            account=acct,
            model_code=model or None,
            contract=contract_to_out(qualified),
            position=position,
            daily_pnl=daily,
            unrealized_pnl=unrealized,
            realized_pnl=realized,
            market_value=market_value,
            as_of=as_of,
        )

    async def _pnl_once[E](
        self,
        *,
        lock: str,
        event: Any,
        matches: Callable[[E], bool],
        current: Callable[[], Iterable[E]],
        has_values: Callable[[E], bool],
        subscribe: Callable[[], int | None],
        unsubscribe: Callable[[], object],
        what: str,
        hint: str,
    ) -> tuple[E, datetime]:
        """One P&L reading: reuse an open subscription with values, or subscribe, wait for
        the first update and unsubscribe. ``reqPnL``/``reqPnLSingle`` refuse a second
        subscription for the same key, so readings of one key are serialized."""
        async with self.connection.request_lock(lock):
            existing = next((entry for entry in current() if matches(entry)), None)
            if existing is not None and has_values(existing):
                return existing, utc_now()
            send: Callable[[], int | None] = subscribe if existing is None else (lambda: None)
            try:
                return await self._first_update(event, matches, send, what=what, hint=hint)
            finally:
                if existing is None:
                    best_effort(unsubscribe, f"cancel {what}")

    async def _first_update[E](
        self,
        event: Any,
        matches: Callable[[E], bool],
        send: Callable[[], int | None],
        *,
        what: str,
        hint: str,
    ) -> tuple[E, datetime]:
        """Send a streaming request and wait for its first update on ``event``.

        ``send`` issues the request (or nothing, to wait on an existing stream) and
        returns its request id, so errors for it fail the wait at once.
        """
        ib = self.ib
        arrived: asyncio.Future[tuple[E, datetime]] = asyncio.get_running_loop().create_future()
        req_ids: list[int] = []

        def on_update(entry: E) -> None:
            if matches(entry) and not arrived.done():
                arrived.set_result((entry, utc_now()))

        def on_error(error_req_id: int, code: int, message: str, _contract: object) -> None:
            if error_req_id in req_ids and not is_informational(code) and not arrived.done():
                arrived.set_exception(IbApiError(code, message, error_req_id))

        def on_disconnected() -> None:
            if not arrived.done():
                arrived.set_exception(ConnectionError("the gateway closed the connection"))

        async def request() -> tuple[E, datetime]:
            req_id = send()
            if req_id is not None:
                req_ids.append(req_id)
            return await arrived

        event += on_update
        ib.errorEvent += on_error
        ib.disconnectedEvent += on_disconnected
        try:
            return await self._call(request, what=what, timeout=self.pnl_wait)
        except RequestTimeoutError as exc:
            raise RequestTimeoutError(f"{exc} {hint}") from None
        finally:
            event -= on_update
            ib.errorEvent -= on_error
            ib.disconnectedEvent -= on_disconnected

    # --- executions and orders -------------------------------------------------------------

    async def executions(
        self,
        account: str | None = None,
        *,
        symbol: str | None = None,
        sec_type: str | None = None,
        side: Action | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> ExecutionList:
        """Return the account's executions (fills), newest first, with commissions.

        IBKR reports the current trading day (up to 7 days when the gateway's trade log
        setting allows it). ``symbol`` and ``sec_type`` are sent to IBKR as a filter;
        ``side`` (BUY/SELL) and ``since`` (naive means UTC) are applied here.
        """
        acct = self.accounts.resolve(account)
        cap = clamp_limit(limit, default=EXECUTIONS_DEFAULT_LIMIT, maximum=EXECUTIONS_MAX_LIMIT)
        wanted_symbol = (clean_str(symbol) or "").upper()
        wanted_type = (clean_str(sec_type) or "").upper()
        after = ensure_utc(since) if since is not None else None
        exec_filter = ExecutionFilter(acctCode=acct, symbol=wanted_symbol, secType=wanted_type)
        ib = self.ib
        fills = await self._call(
            lambda: ib.reqExecutionsAsync(exec_filter), what=f"executions of account {acct}"
        )
        # Fills the session already knew come back without their commission report;
        # the cached fill has it.
        reports = {
            cached.execution.execId: cached.commissionReport
            for cached in ib.fills()
            if cached.commissionReport.execId
        }
        rows: dict[str, ExecutionOut] = {}
        for fill in self.accounts.filter(fills or [], acct):
            if wanted_symbol and fill.contract.symbol.upper() != wanted_symbol:
                continue
            if wanted_type and fill.contract.secType.upper() != wanted_type:
                continue
            report = fill.commissionReport
            if not report.execId:
                report = reports.get(fill.execution.execId, report)
            row = _execution_row(fill, report)
            if side is not None and row.side != side:
                continue
            if after is not None and (row.time is None or row.time < after):
                continue
            rows[row.exec_id] = row
        ordered = sorted(
            rows.values(),
            key=lambda row: row.time or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        selected, truncated = truncate(ordered, cap)
        return ExecutionList(
            account=acct, executions=selected, total=len(ordered), truncated=truncated
        )

    async def open_orders(
        self, account: str | None = None, *, include_other_clients: bool = True
    ) -> OpenOrderList:
        """Return the account's working orders.

        With ``include_other_clients`` (the default) this is ``reqAllOpenOrders``: orders
        of every API client and manual TWS orders, which this server cannot modify or
        cancel (``modifiable`` is False for them). Otherwise ``reqOpenOrders``: only the
        orders this server's client id placed. Filled, cancelled and inactive orders are
        left out.

        A read-only gateway API refuses ``reqOpenOrders`` (error 321, request id -1). This
        client's orders are then read from ``reqAllOpenOrders`` and filtered by client id
        (the same orders), and ``note`` says so; that happens at once when the connection
        already knows the API is read-only, else after IBKR's refusal.

        Raises:
            IbApiError: Code 321 when IBKR refuses ``reqAllOpenOrders`` too (read-only API).
        """
        acct = self.accounts.resolve(account)
        ib = self.ib
        own = self.settings.ib_client_id
        fell_back = False
        if include_other_clients or self._api_read_only():
            fell_back = not include_other_clients
            trades = await self._all_open_orders(ib)
        else:
            try:
                trades = await self._order_records(
                    ib.reqOpenOrdersAsync, key=_OPEN_ORDERS_KEY, what="this server's open orders"
                )
            except IbApiError as exc:
                if exc.error_code != READ_ONLY_API_CODE:
                    raise
                logger.info("reqOpenOrders refused by a read-only API; using reqAllOpenOrders")
                fell_back = True
                trades = await self._all_open_orders(ib)
        unique: dict[object, Trade] = {}
        for trade in self.accounts.filter(trades or [], acct):
            if trade.orderStatus.status in OrderStatus.DoneStates:
                continue
            if not include_other_clients and trade.order.clientId != own:
                continue
            unique[_trade_key(trade)] = trade
        return OpenOrderList(
            account=acct,
            include_other_clients=include_other_clients,
            orders=[_open_order_row(trade, own) for trade in unique.values()],
            note=_OWN_ORDERS_NOTE.format(client_id=own) if fell_back else None,
            as_of=utc_now(),
        )

    async def _all_open_orders(self, ib: IB) -> list[Trade]:
        return await self._order_records(
            ib.reqAllOpenOrdersAsync,
            key=_OPEN_ORDERS_KEY,
            what="open orders",
            grace=self.read_only_grace,
        )

    async def completed_orders(
        self, account: str | None = None, *, api_only: bool = False, limit: int | None = None
    ) -> CompletedOrderList:
        """Return recently filled or cancelled orders, newest first.

        ``api_only`` leaves out orders placed manually in TWS. IBKR decides how far back
        the list reaches (typically the current and recent sessions).

        Raises:
            IbApiError: Code 321 when the gateway's API is read-only, which refuses this
                request: at once when the connection already knows, else on IBKR's refusal.
        """
        acct = self.accounts.resolve(account)
        cap = clamp_limit(
            limit, default=COMPLETED_ORDERS_DEFAULT_LIMIT, maximum=COMPLETED_ORDERS_MAX_LIMIT
        )
        ib = self.ib
        what = "completed orders"
        if self._api_read_only():
            raise IbApiError(READ_ONLY_API_CODE, _KNOWN_READ_ONLY, -1).with_hint(
                _read_only_hint(what), context=what
            )
        states: dict[int, OrderState] = {}

        async def request() -> list[Trade]:
            wrapper = ib.wrapper
            original = wrapper.completedOrder

            def keep_state(contract: Contract, order: Any, state: OrderState) -> None:
                states[id(order)] = state
                original(contract, order, state)

            with hooked(wrapper, "completedOrder", keep_state):
                return await ib.reqCompletedOrdersAsync(api_only)

        trades = await self._order_records(request, key=_COMPLETED_ORDERS_KEY, what=what)
        unique: dict[object, CompletedOrder] = {}
        for trade in self.accounts.filter(trades or [], acct):
            unique[_trade_key(trade)] = _completed_order_row(trade, states.get(id(trade.order)))
        newest = datetime.min.replace(tzinfo=UTC)
        ordered = sorted(unique.values(), key=lambda row: row.completed_at or newest, reverse=True)
        selected, truncated = truncate(ordered, cap)
        return CompletedOrderList(
            account=acct, orders=selected, total=len(ordered), truncated=truncated
        )

    def _api_read_only(self) -> bool:
        """Whether the connection has seen the gateway's API refuse a request as read-only."""
        return self.connection.health().api_read_only

    async def _order_records(
        self,
        send: Callable[[], Awaitable[list[Trade]]],
        *,
        key: str,
        what: str,
        grace: float = 0.0,
    ) -> list[Trade]:
        """Run a string-keyed order-list request; fail it at once if a read-only API refuses it.

        A read-only API answers with error 321 under request id -1, which ib_async never
        ties to the request, so it would wait out its timeout. Such a 321 while the request
        is pending ends it with an :class:`IbApiError` (321), after ``grace`` seconds more
        for the answer (a 321 without a request id can belong to another request). A
        request that ends without its answer is dropped from ib_async's bookkeeping: a
        leftover "openOrders" result list would swallow later ``openOrder`` updates.

        Raises:
            IbApiError: The read-only 321, with a hint, or another gateway error.
            RequestTimeoutError, NotConnectedError: As for :meth:`_call`.
        """
        ib = self.ib
        wrapper = ib.wrapper

        async def request() -> list[Trade]:
            refused: asyncio.Future[IbApiError] = asyncio.get_running_loop().create_future()

            def on_error(req_id: int, code: int, message: str, _contract: object) -> None:
                if not refused.done() and _is_read_only_refusal(req_id, code, message):
                    refused.set_result(IbApiError(code, message, req_id))

            answer: asyncio.Future[list[Trade]] | None = None
            ib.errorEvent += on_error  # only now: a 321 from before was not about this request
            try:
                answer = asyncio.ensure_future(send())
                waiters: set[asyncio.Future[Any]] = {answer, refused}
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if not answer.done() and grace > 0:
                    await asyncio.wait({answer}, timeout=grace)
                if answer.done():
                    return answer.result()
                raise refused.result().with_hint(_read_only_hint(what), context=what)
            finally:
                ib.errorEvent -= on_error
                refused.cancel()
                if answer is not None and not answer.done():
                    best_effort(lambda: end_request(wrapper, key), f"drop the {what} request")
                    answer.cancel()

        return await self._call(request, what=what, exclusive=key)


_PNL_HINT = (
    "IBKR sends P&L about once a second once it has data; right after login it can take a "
    "little longer, so try again. If it never arrives, check get_health and that the account "
    "has positions or trades today."
)

_KNOWN_READ_ONLY = "The gateway's API is in read-only mode, as it reported earlier this session"
"""The message of a request refused here because the connection knows the API is read-only."""

_OWN_ORDERS_NOTE = (
    "The gateway's API is read-only, which refuses the request for this server's orders "
    "alone (reqOpenOrders), so these are every client's open orders filtered to this "
    "server's client id {client_id}: the same orders."
)


def _read_only_hint(what: str) -> str:
    extra = " get_executions still lists recent fills." if what == "completed orders" else ""
    return (
        f"IBKR refuses {what} while the gateway's API is read-only. Untick 'Read-Only API' "
        "in the gateway's API settings (ib-gateway-docker: READ_ONLY_API=no); get_health "
        f"shows api_read_only, which resets when this server reconnects.{extra}"
    )


def _is_read_only_refusal(req_id: int, code: int, message: str) -> bool:
    """IBKR's 321 for a request a read-only API refuses; it carries no request id."""
    return req_id == -1 and code == READ_ONLY_API_CODE and "read-only" in message.lower()


def _req_id_of(ib: IB, attribute: str, key: tuple[Any, ...]) -> int | None:
    """The request id ib_async assigned to a P&L subscription (its wrapper keeps the map)."""
    return subscription_request_id(ib.wrapper, attribute, key)
