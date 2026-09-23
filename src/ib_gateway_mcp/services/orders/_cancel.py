"""Cancel-all plumbing of the orders service: the targets, the cancels, the outcome."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Iterable, Sequence

from ib_async import OrderStatus, Trade

from ib_gateway_mcp.accounts import account_of
from ib_gateway_mcp.errors import IbGatewayMcpError, NotConnectedError
from ib_gateway_mcp.models.orders import CancelScope, OrderResult, OrderStatusOut
from ib_gateway_mcp.services.orders._constants import _CANCELLED_STATES
from ib_gateway_mcp.services.orders._payloads import _CancelEntry, _Payload
from ib_gateway_mcp.services.orders._submit import _SubmitMixin
from ib_gateway_mcp.services.orders._trades import (
    _find,
    _is_open,
    _kept_current,
    _looks_stale,
    _messages,
    _order_errors,
    _same_order,
    _status_out,
)


class _CancelAllMixin(_SubmitMixin):
    """Cancel-all plumbing of :class:`~ib_gateway_mcp.services.orders.OrdersService`:
    which working orders a cancel-all covers, cancelling them, and where they stand."""

    async def _cancel_all_targets(self, scope: CancelScope, account: str) -> list[Trade]:
        """The working orders a cancel-all of ``scope`` covers, from IBKR's fresh answer.

        ``this_client``: this server's orders in ``account``. ``global``: every working
        order on the login (refused unless allowed, see :meth:`_require_global_scope`).
        """
        own = self.settings.ib_client_id
        ib = self.ib
        if scope == "global":
            self._require_global_scope()
            self._require_global_cancel_allowed()
            trades = await self._call(
                ib.reqAllOpenOrdersAsync, what="all open orders", exclusive="openOrders"
            )
            # IBKR's fresh answer, plus this client's own orders (current in the cache);
            # cached copies of other clients' orders may be long finished.
            pool: list[Trade] = [
                *(trades or []),
                *(trade for trade in ib.openTrades() if _kept_current(trade, own)),
            ]
            return self._working(pool, lambda _trade: True)
        trades = await self._call(ib.reqOpenOrdersAsync, what="open orders", exclusive="openOrders")
        pool = [*(trades or []), *ib.openTrades()]
        return self._working(
            pool, lambda trade: trade.order.clientId == own and account_of(trade) == account
        )

    def _working(self, trades: Iterable[Trade], keep: Callable[[Trade], bool]) -> list[Trade]:
        unique: dict[object, Trade] = {}
        for trade in trades:
            order = trade.order
            if not _is_open(trade) or not keep(trade):
                continue
            key: object = (
                ("perm", order.permId) if order.permId else ("order", order.clientId, order.orderId)
            )
            unique.setdefault(key, trade)
        return list(unique.values())

    async def _submit_cancel_all(self, account: str, payload: _Payload) -> OrderResult:
        data = payload["cancel_all"]
        entries = data["orders"]
        messages: list[str] = []
        others: list[_CancelEntry] = []
        if data["scope"] == "global":
            targets, others = self._global_cancel(entries)
        else:
            targets = await self._cancel_listed(entries, messages)
        trades = [trade for trade, _start in targets]
        started = asyncio.get_running_loop().time()
        await self._wait(trades, lambda t: t.isDone(), self.status_wait)
        own = self.settings.ib_client_id
        outs = [_status_out(trade, own, "cancel") for trade in trades]
        if others:
            remaining = self.status_wait - (asyncio.get_running_loop().time() - started)
            outs += await self._others_after_global_cancel(others, remaining, messages)
        errors = [entry for trade, start in targets for entry in _order_errors(trade, start)]
        for message in _messages(targets):
            if message not in messages:
                messages.append(message)
        for out in outs:
            if out.status == OrderStatus.Filled:
                which = out.order_id if out.order_id is not None else f"perm_id {out.perm_id}"
                messages.append(f"Order {which} filled before it could be cancelled.")
        pending = [out for out in outs if out.status not in OrderStatus.DoneStates]
        if pending:
            messages.append(
                f"{len(pending)} order(s) not confirmed cancelled within "
                f"{self.status_wait:g} s; check get_open_orders."
            )
        if not outs:
            status = "NothingCancelled"
        elif pending:
            status = "PendingCancel"
        elif all(out.status in _CANCELLED_STATES for out in outs):
            status = "Cancelled"
        else:
            status = "PartlyCancelled"
        return OrderResult(
            kind="cancel_all",
            account=account,
            accepted=not errors,
            status=status,
            order_ids=[out.order_id for out in outs if out.order_id is not None],
            orders=outs,
            messages=messages,
        )

    def _global_cancel(
        self, entries: Sequence[_CancelEntry]
    ) -> tuple[list[tuple[Trade, int]], list[_CancelEntry]]:
        """Send IBKR's global cancel.

        Returns this client's listed orders, found in the cache (IBKR streams their
        status to this client), and the entries of every other listed order, whose
        outcome only a fresh request can tell (:meth:`_others_after_global_cancel`).
        """
        self._require_global_scope()
        self._require_global_cancel_allowed()
        ib = self.ib
        own = self.settings.ib_client_id
        cache = [trade for trade in ib.trades() if _kept_current(trade, own)]
        targets: list[tuple[Trade, int]] = []
        others: list[_CancelEntry] = []
        for entry in entries:
            trade = next((t for t in cache if _same_order(t, entry)), None)
            if trade is None:
                others.append(entry)
            else:
                targets.append((trade, len(trade.log)))
        try:
            ib.reqGlobalCancel()
        except ConnectionError as exc:
            raise NotConnectedError(f"Not connected: {exc}. Check get_health.") from exc
        return targets, others

    async def _others_after_global_cancel(
        self, entries: Sequence[_CancelEntry], wait: float, messages: list[str]
    ) -> list[OrderStatusOut]:
        """Where other clients' and manual orders stand after a global cancel.

        Their status reaches only the client that placed them, so IBKR is asked: the
        open orders (again every half second while any is still open, for up to
        ``wait`` seconds), then the completed orders for the ones no longer open.
        """
        ib = self.ib
        own = self.settings.ib_client_id
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(wait, 0.0)
        found: dict[int, Trade] = {}
        try:
            while True:
                opened = await self._call(
                    ib.reqAllOpenOrdersAsync, what="open orders", exclusive="openOrders"
                )
                found = {
                    index: trade
                    for index, entry in enumerate(entries)
                    if (trade := _find(opened or [], functools.partial(_same_order, entry=entry)))
                    is not None
                    and _is_open(trade)
                }
                if not found or loop.time() >= deadline:
                    break
                await asyncio.sleep(min(0.5, max(deadline - loop.time(), 0.0)))
            completed: list[Trade] = []
            if len(found) < len(entries):
                completed = list(
                    await self._call(
                        lambda: ib.reqCompletedOrdersAsync(False),
                        what="completed orders",
                        exclusive="completedOrders",
                    )
                    or []
                )
        except IbGatewayMcpError as exc:
            messages.append(
                f"Could not check the {len(entries)} order(s) of other clients after the "
                f"global cancel ({exc}); check get_open_orders."
            )
            return []
        outs: list[OrderStatusOut] = []
        for index, entry in enumerate(entries):
            trade = found.get(index) or _find(
                completed, functools.partial(_same_order, entry=entry)
            )
            if trade is None:
                messages.append(
                    f"Order perm_id {entry['perm_id']} is no longer open, and IBKR did "
                    "not list it among recently completed orders."
                )
                continue
            outs.append(_status_out(trade, own, "cancel"))
        return outs

    async def _cancel_listed(
        self, entries: Sequence[_CancelEntry], messages: list[str]
    ) -> list[tuple[Trade, int]]:
        """Cancel this client's listed orders one by one; note the ones skipped."""
        ib = self.ib
        own = self.settings.ib_client_id

        def lookup(order_id: object) -> Trade | None:
            return _find(
                ib.trades(),
                lambda t: bool(t.order.orderId == order_id and t.order.clientId == own),
            )

        found = [lookup(entry["order_id"]) for entry in entries]
        if any(trade is not None and _looks_stale(trade) for trade in found):
            await self._resync_open_orders()
        targets: list[tuple[Trade, int]] = []
        for entry, trade in zip(entries, found, strict=True):
            if trade is None or not _is_open(trade):
                state = trade.orderStatus.status if trade is not None else "gone"
                messages.append(f"Order {entry['order_id']} was skipped: it is {state}.")
                continue
            start = len(trade.log)
            self._send_cancel(trade)
            targets.append((trade, start))
        return targets
