"""Submit plumbing of the orders service: the live gate, placing, waiting, results."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence

from ib_async import OrderStatus, Trade
from ib_async.objects import TradeLogEntry

from ib_gateway_mcp._util import clean_int, is_informational, utc_now
from ib_gateway_mcp.accounts import account_of, is_paper_account
from ib_gateway_mcp.errors import (
    AccountNotAllowedError,
    ConfigurationError,
    IbGatewayMcpError,
    InvalidRequestError,
    LiveTradingDisabledError,
    NotConnectedError,
    NotFoundError,
)
from ib_gateway_mcp.models.orders import OrderResult, OrderRole, PreviewKind, ResultKind
from ib_gateway_mcp.safety.audit import AuditEvent
from ib_gateway_mcp.safety.policy import OrderSummary
from ib_gateway_mcp.safety.tokens import PreviewRecord
from ib_gateway_mcp.services._account_rows import trade_quantities
from ib_gateway_mcp.services.base import REQUEST_ENDING_WARNINGS, BaseService
from ib_gateway_mcp.services.orders._constants import (
    _MODIFIABLE_FIELDS,
    _ROLE_LABELS,
    MAX_REMEMBERED_ORDERS,
)
from ib_gateway_mcp.services.orders._describe import _num
from ib_gateway_mcp.services.orders._payloads import (
    _contract_from_payload,
    _order_from_payload,
    _order_payload,
    _Payload,
)
from ib_gateway_mcp.services.orders._planning import _modifiable_snapshot
from ib_gateway_mcp.services.orders._trades import (
    _acknowledged,
    _find,
    _is_global_cancel,
    _is_open,
    _is_order_kind,
    _is_paper_action,
    _looks_stale,
    _messages,
    _modify_rejection,
    _order_count,
    _rejection,
    _settled,
    _status_out,
)

logger = logging.getLogger(__name__)


class _SubmitMixin(BaseService):
    """Submit plumbing of :class:`~ib_gateway_mcp.services.orders.OrdersService`:
    the checks before a submit, placing and cancelling, and the results."""

    status_wait: float
    exercise_wait: float
    _perm_ids: OrderedDict[int, int]

    def _check_live(
        self, account: str, *, human_confirmed: bool, paper: bool | None = None
    ) -> None:
        """Refuse a live account without IBKR_MCP_ALLOW_LIVE, or without a human when required."""
        if paper if paper is not None else is_paper_account(account):
            return
        if not self.settings.allow_live:
            raise LiveTradingDisabledError(
                f"Account {account} is a live account and IBKR_MCP_ALLOW_LIVE is not set; "
                "nothing was sent."
            )
        self._require_human(
            paper=False,
            human_confirmed=human_confirmed,
            refusal=(
                "This action goes to a live account and needs a human confirmation, which "
                "this call did not carry. Nothing was sent."
            ),
        )

    def _check_submit(
        self, kind: str, account: str, payload: _Payload, *, human_confirmed: bool
    ) -> None:
        """Every refusal of a submit, without side effects (no token use, no rate slot)."""
        safety = self.safety
        self.accounts.resolve(account)
        if _is_global_cancel(kind, payload):
            self._require_global_scope()
            self._require_global_cancel_allowed()
        count = _order_count(kind, payload)
        if count:  # cancel-all reduces risk: neither halted by the breaker nor rate limited
            safety.breaker.check()
        for summary in payload["summaries"]:
            safety.policy.check(OrderSummary.model_validate(summary))
        self._check_live(
            account,
            human_confirmed=human_confirmed,
            paper=_is_paper_action(self.accounts, kind, account, payload),
        )
        if count:
            safety.rate_limiter.check(count)

    def _order_account(self, trade: Trade, order_id: int) -> str:
        """The allowed account an existing order belongs to; never the default by accident."""
        account = account_of(trade)
        if account is None:
            raise AccountNotAllowedError(
                f"Order {order_id} carries no account, so the account allowlist cannot be "
                "checked; refusing to act on it."
            )
        return self.accounts.resolve(account)

    def _remember(self, trade: Trade) -> None:
        """Keep order id -> perm id for this client's orders (completed ones lose the id)."""
        order = trade.order
        order_id = clean_int(order.orderId)
        perm_id = clean_int(order.permId) or clean_int(trade.orderStatus.permId)
        if not order_id or order_id <= 0 or not perm_id:
            return
        if order.clientId != self.settings.ib_client_id:
            return
        self._perm_ids[order_id] = perm_id
        self._perm_ids.move_to_end(order_id)
        while len(self._perm_ids) > MAX_REMEMBERED_ORDERS:
            self._perm_ids.popitem(last=False)

    async def _resync_open_orders(self) -> bool:
        """Ask IBKR for this client's open orders again; it re-sends each one's status.

        ib_async updates the cached trades in place (status, prices, quantity). Returns
        False when the request failed (the cache stays as it was).
        """
        ib = self.ib
        try:
            await self._call(ib.reqOpenOrdersAsync, what="open orders", exclusive="openOrders")
        except IbGatewayMcpError as exc:
            logger.warning("Could not re-sync open orders: %s", exc)
            return False
        return True

    def _require_global_scope(self) -> None:
        managed = set(self.accounts.managed)
        allowed = self.accounts.allowed
        if not managed or not managed <= allowed:
            raise AccountNotAllowedError(
                f"scope='global' cancels the working orders of every account on this login "
                f"({len(managed)} managed, {len(managed & allowed)} allowed), so it needs every "
                "managed account in IBKR_MCP_ACCOUNTS. Use scope='this_client' instead."
            )

    def _require_global_cancel_allowed(self) -> None:
        """Refuse IBKR's global cancel unless the operator opted in."""
        if not self.settings.allow_global_cancel:
            raise ConfigurationError(
                "scope='global' is IBKR's global cancel: it cancels every working order on "
                "the login, including other programs' protective stops and manual TWS "
                "orders, so it needs IBKR_MCP_ALLOW_GLOBAL_CANCEL=true. Use "
                "scope='this_client' to cancel this server's own orders."
            )

    def _order_record(self, token: str) -> tuple[PreviewRecord, PreviewKind]:
        """Peek at an order token; a token of another kind (an FA replacement) is refused.

        Raises:
            TokenNotFoundError, TokenExpiredError, TokenMismatchError: Unusable token.
            InvalidRequestError: Not an order token; nothing was used or sent.
        """
        record = self.safety.previews.peek(token)
        kind = record.kind
        if not _is_order_kind(kind):
            raise InvalidRequestError(
                f"This token is for a {kind} preview, not an order: "
                + (
                    "pass it to apply_fa_config instead."
                    if kind == "replace_fa"
                    else "it cannot be submitted here."
                )
            )
        return record, kind

    async def _own_trade(self, order_id: int) -> Trade:
        """The trade of order ``order_id`` placed by this server's client id.

        A cached trade that ib_async marked Cancelled because of an error (e.g. a
        rejected modification) is re-synced with IBKR first: the order may still work.
        """
        ib = self.ib
        own = self.settings.ib_client_id

        def mine(trade: Trade) -> bool:
            return bool(trade.order.orderId == order_id and trade.order.clientId == own)

        trade = _find(ib.trades(), mine)
        if trade is not None and _looks_stale(trade):
            await self._resync_open_orders()
        if trade is None:
            refreshed = await self._call(
                ib.reqOpenOrdersAsync, what="open orders", exclusive="openOrders"
            )
            trade = _find([*(refreshed or []), *ib.trades()], mine)
        if trade is not None:
            return trade
        other = _find(ib.trades(), lambda t: bool(t.order.orderId == order_id))
        if other is not None:
            raise InvalidRequestError(
                f"Order {order_id} was placed by API client {other.order.clientId}, not by this "
                f"server (client id {own}). IBKR only lets the client that placed an order "
                "modify or cancel it."
            )
        raise NotFoundError(
            f"No order {order_id} placed by this server (client id {own}) is known. "
            "get_open_orders lists working orders; its modifiable flag marks this server's."
        )

    def _send_cancel(self, trade: Trade) -> None:
        try:
            self.ib.cancelOrder(trade.order)
        except ConnectionError as exc:
            raise NotConnectedError(
                f"Lost the gateway connection while cancelling: {exc}. Check get_health."
            ) from exc

    async def _wait(
        self, trades: Sequence[Trade], ready: Callable[[Trade], bool], timeout: float
    ) -> bool:
        """Wait until ``ready`` holds for every trade (re-checked on each status event)."""
        if all(ready(trade) for trade in trades):
            return True
        changed = asyncio.Event()

        def on_status(*_args: object) -> None:
            if all(ready(trade) for trade in trades):
                changed.set()

        for trade in trades:
            trade.statusEvent += on_status
        try:
            async with asyncio.timeout(timeout):
                await changed.wait()
        except TimeoutError:
            pass
        finally:
            for trade in trades:
                trade.statusEvent -= on_status
        return all(ready(trade) for trade in trades)

    async def _submit_orders(
        self, kind: ResultKind, account: str, payload: _Payload
    ) -> OrderResult:
        ib = self.ib
        placed: list[tuple[OrderRole, Trade]] = []
        try:
            for item in payload["orders"]:
                contract = _contract_from_payload(item["contract"])
                order = _order_from_payload(item["order"])
                order.account = account
                parent = item["parent"]
                if parent is not None:
                    order.parentId = placed[parent][1].order.orderId
                placed.append((item["role"], ib.placeOrder(contract, order)))
        except ConnectionError as exc:
            raise self._placing_interrupted(kind, account, placed, exc) from exc
        trades = [trade for _role, trade in placed]
        settled = await self._wait(trades, lambda t: _settled(t, 0), self.status_wait)
        self._mark_never_placed(trades)
        extra: list[str] = []
        if kind == "bracket":
            extra = await self._unwind_bracket(placed)
        return self._order_result(
            kind,
            account,
            [(role, trade, 0) for role, trade in placed],
            settled=settled,
            extra_messages=extra,
        )

    def _placing_interrupted(
        self,
        kind: str,
        account: str,
        placed: Sequence[tuple[OrderRole, Trade]],
        exc: ConnectionError,
    ) -> NotConnectedError:
        """Report a connection lost part-way through placing, naming what went out.

        A cancel is attempted for each order already placed, but ib_async sends nothing
        without a connection, so it usually fails; the message says which orders may
        still be at the gateway (transmitted ones may be working).
        """
        cancel_sent: list[str] = []
        left: list[str] = []
        for role, trade in placed:
            what = (
                f"order {trade.order.orderId} ({_ROLE_LABELS[role].lower()}, "
                + ("transmitted, may be working" if trade.order.transmit else "not transmitted")
                + ")"
            )
            try:
                self.ib.cancelOrder(trade.order)
            except ConnectionError:
                left.append(what)
            else:
                cancel_sent.append(what)
        self.safety.audit.record(
            AuditEvent.REJECTED,
            stage="ibkr",
            kind=kind,
            account=account,
            reason=f"connection lost while placing: {exc}",
            order_ids=[trade.order.orderId for _role, trade in placed],
            cancel_sent=len(cancel_sent),
        )
        if not placed:
            return NotConnectedError(
                f"Lost the gateway connection before any order was placed: {exc}. Nothing was "
                "sent. Check get_health, then preview again."
            )
        parts = [f"Lost the gateway connection while placing orders: {exc}."]
        if cancel_sent:
            parts.append("Cancel requested for " + ", ".join(cancel_sent) + ".")
        if left:
            parts.append("Could not cancel (no connection): " + ", ".join(left) + ".")
        parts.append(
            "After reconnecting, check get_open_orders and cancel what should not stay "
            "before retrying."
        )
        return NotConnectedError(" ".join(parts))

    def _mark_never_placed(self, trades: Iterable[Trade]) -> None:
        """Mark new orders IBKR refused with 321 Inactive: ib_async leaves them 'working'.

        Only for orders just placed: on a modification, 321 leaves the order working.
        """
        for trade in trades:
            if trade.orderStatus.status == OrderStatus.ValidationError and any(
                entry.errorCode in REQUEST_ENDING_WARNINGS for entry in trade.log
            ):
                trade.orderStatus.status = OrderStatus.Inactive
                trade.log.append(
                    TradeLogEntry(
                        utc_now(),
                        OrderStatus.Inactive,
                        "Not placed: IBKR refused the order (warning 321); marked Inactive.",
                    )
                )

    async def _unwind_bracket(self, placed: Sequence[tuple[OrderRole, Trade]]) -> list[str]:
        """Cancel what is left of a bracket once IBKR rejected any of its orders.

        A rejected take profit or stop loss would otherwise leave the entry working
        without that exit. Cancelling the entry cancels its children at IBKR. An entry
        that has (partly) filled is left alone: its remaining exit still protects it.
        """
        rejected = [(role, trade) for role, trade in placed if _rejection(trade, 0)]
        if not rejected:
            return []
        names = ", ".join(
            f"{_ROLE_LABELS[role].lower()} (order {trade.order.orderId})"
            for role, trade in rejected
        )
        working = [(role, trade) for role, trade in placed if _is_open(trade)]
        if not working:
            return [f"IBKR rejected the bracket's {names}; none of its orders is working."]
        entry = placed[0][1]
        filled, _remaining = trade_quantities(entry)
        if filled > 0:
            left = ", ".join(f"order {trade.order.orderId}" for _role, trade in working)
            return [
                f"IBKR rejected the bracket's {names} after the entry filled {_num(filled)}; "
                f"{left} stay working. Check get_open_orders and add the missing exit."
            ]
        targets = [entry] if _is_open(entry) else [trade for _role, trade in working]
        failed: list[str] = []
        for trade in targets:
            try:
                self.ib.cancelOrder(trade.order)
            except ConnectionError as exc:
                failed.append(f"order {trade.order.orderId}: {exc}")
        self.safety.audit.record(
            AuditEvent.CANCEL,
            reason=f"bracket unwound after IBKR rejected its {names}",
            order_ids=[trade.order.orderId for trade in targets],
        )
        await self._wait(
            [trade for _role, trade in working], lambda t: t.isDone(), self.status_wait
        )
        cancelled = ", ".join(f"order {trade.order.orderId}" for _role, trade in working)
        message = (
            f"IBKR rejected the bracket's {names}, so the rest of the bracket ({cancelled}) "
            "was cancelled: no entry is left working without both exits."
        )
        if failed:
            message += " Cancelling failed for " + "; ".join(failed) + "; check get_open_orders."
        elif any(not trade.isDone() for _role, trade in working):
            message += " IBKR has not confirmed every cancellation yet; check get_open_orders."
        return [message]

    async def _submit_modify(self, account: str, payload: _Payload) -> OrderResult:
        data = payload["modify"]
        order_id = data["order_id"]
        trade = await self._own_trade(order_id)
        if not _is_open(trade):
            raise InvalidRequestError(
                f"Order {order_id} is {trade.orderStatus.status}; it can no longer be modified."
            )
        if _modifiable_snapshot(_order_payload(trade.order)) != _modifiable_snapshot(
            data["original"]
        ):
            raise InvalidRequestError(
                f"Order {order_id} changed since the preview; preview the modification again."
            )
        filled, _remaining = trade_quantities(trade)
        if filled != data["filled"]:
            raise InvalidRequestError(
                f"Order {order_id} has filled {_num(filled)} since the preview (then "
                f"{_num(data['filled'])}); preview the modification again."
            )
        wanted = _order_from_payload(payload["orders"][0]["order"])
        order = trade.order
        before = {name: getattr(order, name) for name in (*_MODIFIABLE_FIELDS, "lmtPriceOffset")}
        for name in _MODIFIABLE_FIELDS:
            setattr(order, name, getattr(wanted, name))
        order.lmtPriceOffset = wanted.lmtPriceOffset
        order.transmit = True
        start = len(trade.log)
        try:
            self.ib.placeOrder(trade.contract, order)
        except ConnectionError as exc:
            for name, value in before.items():
                setattr(order, name, value)
            raise NotConnectedError(
                f"Lost the gateway connection while modifying: {exc}. Check get_order_status."
            ) from exc
        settled = await self._wait_modify(trade, start)
        reason = _modify_rejection(trade, start)
        if reason is None:
            return self._order_result(
                "modify", account, [("modify", trade, start)], settled=settled
            )
        # IBKR keeps the original order: put its terms back and let IBKR re-send its
        # status (ib_async marked the trade Cancelled or ValidationError on the error).
        for name, value in before.items():
            setattr(order, name, value)
        synced = await self._resync_open_orders()
        if not synced:
            note = (
                "IBKR rejected the modification. After a rejected modification IBKR normally "
                "keeps the original order working, but it could not be re-checked now "
                f"(status here: {trade.orderStatus.status}); check get_open_orders before "
                "acting on it."
            )
        elif _is_open(trade):
            note = (
                "IBKR rejected the modification; the original order is still working "
                f"unchanged (status {trade.orderStatus.status})."
            )
        else:
            note = (
                "IBKR rejected the modification and no longer lists the order as working "
                f"(status {trade.orderStatus.status}); check get_order_status."
            )
        return self._order_result(
            "modify",
            account,
            [("modify", trade, start)],
            settled=True,
            rejection=reason,
            extra_messages=[note],
        )

    async def _wait_modify(self, trade: Trade, start: int) -> bool:
        """Wait for IBKR's answer to a modification: a status, an error, or an echo.

        A modification that changes nothing in the order status (e.g. the price of a
        PreSubmitted order) produces no ``orderStatus`` event; IBKR's ``openOrder`` echo
        of the order (``ib.openOrderEvent``) acknowledges it then.
        """
        if _acknowledged(trade, start):
            return True
        ib = self.ib
        answered = asyncio.Event()
        echoed = False

        def on_status(*_args: object) -> None:
            if _acknowledged(trade, start):
                answered.set()

        def on_open_order(echo: Trade) -> None:
            nonlocal echoed
            if echo is trade or (
                echo.order.orderId == trade.order.orderId
                and echo.order.clientId == trade.order.clientId
            ):
                echoed = True
                answered.set()

        trade.statusEvent += on_status
        ib.openOrderEvent += on_open_order
        try:
            async with asyncio.timeout(self.status_wait):
                await answered.wait()
        except TimeoutError:
            pass
        finally:
            trade.statusEvent -= on_status
            ib.openOrderEvent -= on_open_order
        return echoed or _acknowledged(trade, start)

    async def _submit_exercise(self, account: str, payload: _Payload) -> OrderResult:
        ib = self.ib
        data = payload["exercise"]
        contract = _contract_from_payload(data["contract"])
        errors: list[str] = []
        got_error = asyncio.Event()
        req_id = self._new_req_id(ib, "an option exercise")

        def on_error(error_req_id: int, code: int, message: str, _contract: object) -> None:
            if error_req_id == req_id and not is_informational(code):
                errors.append(f"IB error {code}: {message}")
                got_error.set()

        ib.errorEvent += on_error
        try:
            ib.client.exerciseOptions(
                req_id,
                contract,
                data["action_code"],
                data["quantity"],
                account,
                data["override"],
            )
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.exercise_wait):
                    await got_error.wait()
        except ConnectionError as exc:
            raise NotConnectedError(f"Not connected: {exc}. Check get_health.") from exc
        finally:
            ib.errorEvent -= on_error
        breaker = self.safety.breaker
        messages = list(errors)
        if errors:
            self._record_rejection("exercise", account, errors[-1])
        else:
            breaker.record_success()
            messages.append(
                "IBKR does not acknowledge exercise requests; check get_positions and "
                "get_account_values for the result."
            )
        return OrderResult(
            kind="exercise",
            account=account,
            accepted=not errors,
            status="Rejected" if errors else "Sent",
            messages=messages,
        )

    def _order_result(
        self,
        kind: ResultKind,
        account: str,
        placed: Sequence[tuple[OrderRole, Trade, int]],
        *,
        settled: bool,
        rejection: str | None = None,
        extra_messages: Sequence[str] = (),
    ) -> OrderResult:
        """Build the result of placed (or modified) orders and feed the circuit breaker."""
        own = self.settings.ib_client_id
        rejections = [
            reason for _role, trade, start in placed if (reason := _rejection(trade, start))
        ]
        if rejection is not None:
            rejections.insert(0, rejection)
        messages = _messages([(trade, start) for _role, trade, start in placed])
        messages.extend(message for message in extra_messages if message not in messages)
        if rejections:
            self._record_rejection(kind, account, rejections[0])
        elif settled:
            self.safety.breaker.record_success()
        else:
            messages.append(
                f"IBKR sent no status within {self.status_wait:g} s; check get_order_status."
            )
        for _role, trade, _start in placed:
            self._remember(trade)
        outs = [_status_out(trade, own, role) for role, trade, _start in placed]
        primary = outs[0]
        return OrderResult(
            kind=kind,
            account=account,
            accepted=not rejections,
            status=primary.status,
            order_id=primary.order_id,
            perm_id=primary.perm_id,
            filled=primary.filled,
            remaining=primary.remaining,
            avg_fill_price=primary.avg_fill_price,
            order_ids=[out.order_id for out in outs if out.order_id is not None],
            orders=outs,
            messages=messages,
        )

    def _record_rejection(self, kind: str, account: str, reason: str) -> None:
        breaker = self.safety.breaker
        opened = breaker.record_rejection(reason)
        self.safety.audit.record(
            AuditEvent.REJECTED, stage="ibkr", kind=kind, account=account, reason=reason
        )
        if opened:
            self.safety.audit.record(
                AuditEvent.CIRCUIT_OPEN,
                rejections=breaker.consecutive_rejections,
                reason=reason,
            )
