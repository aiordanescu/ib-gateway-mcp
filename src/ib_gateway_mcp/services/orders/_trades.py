"""Reading ib_async trades: state, status models and the rejection rules."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING, TypeGuard

from ib_async import Fill, OrderStatus, Trade
from ib_async.objects import TradeLogEntry

from ib_gateway_mcp._util import clean_float, clean_int, clean_str, contract_to_out, ensure_utc
from ib_gateway_mcp.accounts import is_paper_account
from ib_gateway_mcp.models.orders import (
    FillOut,
    OrderLogEntry,
    OrderRole,
    OrderStatusOut,
    PreviewKind,
)
from ib_gateway_mcp.services._account_rows import trade_quantities
from ib_gateway_mcp.services.base import REQUEST_ENDING_WARNINGS
from ib_gateway_mcp.services.orders._constants import (
    _CANCELLED_STATES,
    _MODIFY_ACK,
    _MODIFY_REJECTING_WARNINGS,
    _NOT_REJECTIONS,
    _ORDER_KINDS,
    _WAITING_STATES,
)
from ib_gateway_mcp.services.orders._describe import _price, _quantity
from ib_gateway_mcp.services.orders._payloads import _Body, _CancelEntry

if TYPE_CHECKING:
    from ib_gateway_mcp.accounts import AccountScope


def _find(trades: Iterable[Trade], match: Callable[[Trade], bool]) -> Trade | None:
    return next((trade for trade in trades if match(trade)), None)


def _kept_current(trade: Trade, own_client_id: int) -> bool:
    """Whether ib_async keeps this cached trade up to date: only this client's orders.

    IBKR streams status updates to the client that placed an order. Other clients'
    and manual TWS orders enter the cache from a ``reqAllOpenOrders`` snapshot and are
    never updated after it (short of the gateway's Master API client id), so a cached
    copy of one can show Submitted long after it filled.
    """
    order = trade.order
    order_id = clean_int(order.orderId)
    return order.clientId == own_client_id and order_id is not None and order_id > 0


# --- trade state ---------------------------------------------------------------------------


def _never_placed(trade: Trade) -> bool:
    """A new order IBKR refused with warning 321: ib_async leaves it ValidationError."""
    return (
        trade.orderStatus.status == OrderStatus.ValidationError
        and not trade.order.permId
        and any(entry.errorCode in REQUEST_ENDING_WARNINGS for entry in trade.log)
    )


def _is_open(trade: Trade) -> bool:
    """Working as far as this session knows: not done, and not a never-placed order."""
    return not trade.isDone() and not _never_placed(trade)


def _looks_stale(trade: Trade) -> bool:
    """Cancelled by ib_async because of an error message, not by an IBKR status.

    ib_async marks a trade Cancelled on any error code, including one that only rejects a
    modification while the original order keeps working. Such a trade needs a re-sync
    (``reqOpenOrders``) before the service trusts it.
    """
    if trade.orderStatus.status != OrderStatus.Cancelled or not trade.log:
        return False
    code = trade.log[-1].errorCode
    return bool(code) and code not in _NOT_REJECTIONS


# --- order status -------------------------------------------------------------------------


def _fill_out(fill: Fill) -> FillOut:
    execution = fill.execution
    report = fill.commissionReport
    reported = report is not None and bool(report.execId)
    return FillOut(
        exec_id=execution.execId,
        time=ensure_utc(fill.time) if isinstance(fill.time, datetime) else None,
        shares=_quantity(execution.shares),
        price=clean_float(execution.price),
        exchange=clean_str(execution.exchange),
        commission=clean_float(report.commission) if reported else None,
        commission_currency=clean_str(report.currency) if reported else None,
        realized_pnl=clean_float(report.realizedPNL) if reported else None,
    )


def _log_out(entry: TradeLogEntry) -> OrderLogEntry:
    return OrderLogEntry(
        time=ensure_utc(entry.time) if isinstance(entry.time, datetime) else None,
        status=entry.status or "",
        message=clean_str(entry.message),
        error_code=entry.errorCode or None,
    )


def _status_out(trade: Trade, own_client_id: int, role: OrderRole | None = None) -> OrderStatusOut:
    order = trade.order
    status = trade.orderStatus
    order_id = clean_int(order.orderId)
    client_id = clean_int(order.clientId)
    filled, remaining = trade_quantities(trade)
    return OrderStatusOut(
        account=clean_str(order.account),
        order_id=order_id if order_id and order_id > 0 else None,
        perm_id=clean_int(order.permId) or clean_int(status.permId) or None,
        client_id=client_id,
        parent_id=clean_int(order.parentId) or None,
        role=role,
        placed_by_this_server=bool(order_id and order_id > 0 and client_id == own_client_id),
        contract=contract_to_out(trade.contract),
        action=order.action,
        quantity=_quantity(order.totalQuantity),
        order_type=order.orderType,
        limit_price=_price(order.lmtPrice),
        aux_price=_price(order.auxPrice),
        tif=clean_str(order.tif),
        oca_group=clean_str(order.ocaGroup),
        order_ref=clean_str(order.orderRef),
        status=status.status or "Unknown",
        filled=filled,
        remaining=remaining,
        avg_fill_price=_price(status.avgFillPrice) if filled > 0 else None,
        last_fill_price=_price(status.lastFillPrice) if filled > 0 else None,
        why_held=clean_str(status.whyHeld),
        fills=[_fill_out(fill) for fill in trade.fills],
        log=[_log_out(entry) for entry in trade.log],
    )


def _order_errors(trade: Trade, start: int) -> list[TradeLogEntry]:
    return [
        entry
        for entry in trade.log[start:]
        if entry.errorCode and entry.errorCode not in _NOT_REJECTIONS
    ]


def _settled(trade: Trade, start: int) -> bool:
    """IBKR answered a new order: a real status, or a request-ending warning (321)."""
    status = trade.orderStatus.status
    if status == OrderStatus.ValidationError:
        return any(entry.errorCode in REQUEST_ENDING_WARNINGS for entry in trade.log[start:])
    return status not in _WAITING_STATES


def _acknowledged(trade: Trade, start: int) -> bool:
    """IBKR answered a modification (anything after ib_async's own 'Modify' entry)."""
    return any(entry.message != _MODIFY_ACK for entry in trade.log[start:]) or trade.isDone()


def _modify_rejection(trade: Trade, start: int) -> str | None:
    """Why IBKR refused a modification sent at log entry ``start``, or None.

    Besides the error codes ib_async turns into Cancelled, the warnings in
    :data:`_MODIFY_REJECTING_WARNINGS` refuse a modification too.
    """
    errors = _order_errors(trade, start)
    refusing = [entry for entry in errors if entry.errorCode in _MODIFY_REJECTING_WARNINGS]
    if refusing:
        return refusing[-1].message or f"IBKR error {refusing[-1].errorCode}"
    return _rejection(trade, start)


def _rejection(trade: Trade, start: int) -> str | None:
    """Why IBKR rejected the order since log entry ``start``, or None."""
    status = trade.orderStatus.status
    errors = _order_errors(trade, start)
    reason = errors[-1].message if errors else None
    if status == OrderStatus.Inactive:
        return reason or "IBKR set the order to Inactive (not accepted)"
    if status in _CANCELLED_STATES and errors:
        return reason
    if status == OrderStatus.ValidationError and any(
        entry.errorCode in REQUEST_ENDING_WARNINGS for entry in errors
    ):
        return reason
    return None


def _messages(trades: Iterable[tuple[Trade, int]]) -> list[str]:
    messages: list[str] = []
    for trade, start in trades:
        for entry in trade.log[start:]:
            if entry.errorCode and entry.message and entry.message not in messages:
                messages.append(entry.message)
        if trade.advancedError and trade.advancedError not in messages:
            messages.append(trade.advancedError)
    return messages


# --- token kinds ----------------------------------------------------------------------


def _is_order_kind(kind: str) -> TypeGuard[PreviewKind]:
    """Whether a token of this kind is an order action (not, e.g., an FA replacement)."""
    return kind in _ORDER_KINDS


def _is_paper_action(accounts: AccountScope, kind: str, account: str, payload: _Body) -> bool:
    """Whether a token's action reaches paper accounts only.

    A global cancel reaches every account of the login, whichever one it was made for.
    """
    if _is_global_cancel(kind, payload):
        return accounts.all_paper is True
    return is_paper_account(account)


def _is_global_cancel(kind: str, payload: _Body) -> bool:
    cancel_all = payload.get("cancel_all")
    return kind == "cancel_all" and cancel_all is not None and cancel_all["scope"] == "global"


def _order_count(kind: str, payload: _Body) -> int:
    """Orders an action places, for the rate limit: 0 for a cancel-all (risk reducing)."""
    if kind == "cancel_all":
        return 0
    if kind in ("modify", "exercise"):
        return 1
    return max(len(payload.get("orders", [])), 1)


def _same_order(trade: Trade, entry: _CancelEntry) -> bool:
    """Whether ``trade`` is the order a cancel-all payload ``entry`` lists."""
    order = trade.order
    perm_id = entry["perm_id"]
    if perm_id:
        return bool(order.permId == perm_id)
    return bool(order.orderId == entry["order_id"] and order.clientId == entry["client_id"])
