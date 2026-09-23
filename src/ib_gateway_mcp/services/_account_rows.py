"""Row converters and filters for account data and orders on record.

Shared by the account service (values, positions, portfolio, executions, open and
completed orders) and the orders service (:func:`trade_quantities`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from ib_async import (
    AccountValue,
    CommissionReport,
    Contract,
    Fill,
    OrderState,
    PortfolioItem,
    Trade,
)
from ib_async.util import EPOCH

from ib_gateway_mcp._util import clean_float, clean_int, clean_str, contract_to_out, ensure_utc
from ib_gateway_mcp.errors import InvalidRequestError
from ib_gateway_mcp.models.account import (
    AccountValueOut,
    CompletedOrder,
    ExecutionOut,
    OpenOrder,
    PortfolioItemOut,
    PositionOut,
)
from ib_gateway_mcp.models.common import Action
from ib_gateway_mcp.services._ibtime import parse_ib_time

_MAX_LISTED_TAGS = 80
_SIDES: dict[str, Action] = {"BOT": "BUY", "SLD": "SELL", "BUY": "BUY", "SELL": "SELL"}


def _number(value: Any) -> float | None:
    """``clean_float`` for ib_async fields typed ``float | Decimal``."""
    return clean_float(float(value) if isinstance(value, Decimal) else value)


def _price(value: Any) -> float | None:
    """An order price: None when unset (IBKR's max-double marker, NaN) or zero."""
    number = _number(value)
    return number or None


def _quantity(value: Any) -> float:
    number = _number(value)
    return number if number is not None else 0.0


def _value_out(value: AccountValue) -> AccountValueOut:
    return AccountValueOut(
        account=value.account,
        tag=value.tag,
        value=value.value,
        amount=clean_float(value.value),
        currency=clean_str(value.currency),
        model_code=clean_str(value.modelCode),
    )


def _position_row(
    account: str, contract: Contract, position: float, avg_cost: float, model_code: str | None
) -> PositionOut:
    return PositionOut(
        account=account,
        contract=contract_to_out(contract),
        position=_quantity(position),
        avg_cost=clean_float(avg_cost),
        model_code=clean_str(model_code),
    )


def _portfolio_row(item: PortfolioItem) -> PortfolioItemOut:
    return PortfolioItemOut(
        account=item.account,
        contract=contract_to_out(item.contract),
        position=_quantity(item.position),
        market_price=clean_float(item.marketPrice),
        market_value=clean_float(item.marketValue),
        average_cost=clean_float(item.averageCost),
        unrealized_pnl=clean_float(item.unrealizedPNL),
        realized_pnl=clean_float(item.realizedPNL),
    )


def _execution_row(fill: Fill, report: CommissionReport | None) -> ExecutionOut:
    execution = fill.execution
    if report is not None and not report.execId:
        report = None  # an empty placeholder: IBKR has not reported the commission yet
    time = execution.time if execution.time and execution.time != EPOCH else fill.time
    return ExecutionOut(
        account=execution.acctNumber,
        exec_id=execution.execId,
        time=ensure_utc(time) if time and time != EPOCH else None,
        contract=contract_to_out(fill.contract),
        side=_SIDES.get(execution.side.upper()),
        shares=_quantity(execution.shares),
        price=clean_float(execution.price),
        avg_price=clean_float(execution.avgPrice),
        cum_qty=clean_float(execution.cumQty),
        exchange=clean_str(execution.exchange),
        order_id=clean_int(execution.orderId) or None,
        perm_id=clean_int(execution.permId) or None,
        client_id=clean_int(execution.clientId),
        order_ref=clean_str(execution.orderRef),
        model_code=clean_str(execution.modelCode),
        liquidation=bool(execution.liquidation),
        commission=clean_float(report.commission) if report is not None else None,
        commission_currency=clean_str(report.currency) if report is not None else None,
        realized_pnl=clean_float(report.realizedPNL) if report is not None else None,
    )


def trade_quantities(trade: Trade) -> tuple[float, float]:
    """Filled and remaining quantity; before any status message, derived from the order."""
    status = trade.orderStatus
    filled = _quantity(status.filled)
    remaining = _quantity(status.remaining)
    if filled or remaining:
        return filled, remaining
    filled = _number(trade.order.filledQuantity) or 0.0
    return filled, max(_quantity(trade.order.totalQuantity) - filled, 0.0)


def _open_order_row(trade: Trade, own_client_id: int) -> OpenOrder:
    order = trade.order
    status = trade.orderStatus
    filled, remaining = trade_quantities(trade)
    order_id = clean_int(order.orderId)
    client_id = clean_int(order.clientId)
    return OpenOrder(
        account=order.account,
        order_id=order_id if order_id and order_id > 0 else None,
        perm_id=clean_int(order.permId) or None,
        client_id=client_id,
        parent_id=clean_int(order.parentId) or None,
        modifiable=bool(order_id and order_id > 0 and client_id == own_client_id),
        contract=contract_to_out(trade.contract),
        action=order.action,
        quantity=_quantity(order.totalQuantity),
        order_type=order.orderType,
        limit_price=_price(order.lmtPrice),
        aux_price=_price(order.auxPrice),
        trailing_percent=_price(order.trailingPercent),
        trail_stop_price=_price(order.trailStopPrice),
        tif=clean_str(order.tif),
        good_till_date=clean_str(order.goodTillDate),
        outside_rth=bool(order.outsideRth),
        oca_group=clean_str(order.ocaGroup),
        order_ref=clean_str(order.orderRef),
        status=status.status or "Unknown",
        filled=filled,
        remaining=remaining,
        avg_fill_price=_price(status.avgFillPrice),
        why_held=clean_str(status.whyHeld),
    )


def _completed_order_row(trade: Trade, state: OrderState | None) -> CompletedOrder:
    order = trade.order
    completed_time = clean_str(state.completedTime) if state is not None else None
    return CompletedOrder(
        account=order.account,
        perm_id=clean_int(order.permId) or None,
        parent_perm_id=clean_int(order.parentPermId) or None,
        contract=contract_to_out(trade.contract),
        action=order.action,
        quantity=_quantity(order.totalQuantity),
        filled=_number(order.filledQuantity),
        order_type=order.orderType,
        limit_price=_price(order.lmtPrice),
        aux_price=_price(order.auxPrice),
        tif=clean_str(order.tif),
        order_ref=clean_str(order.orderRef),
        status=trade.orderStatus.status or (state.status if state is not None else "") or "Unknown",
        completed_status=clean_str(state.completedStatus) if state is not None else None,
        completed_time=completed_time,
        completed_at=parse_ib_time(completed_time),
    )


def _trade_key(trade: Trade) -> object:
    order = trade.order
    if order.permId:
        return ("perm", order.permId)
    if order.orderId > 0:
        return ("order", order.clientId, order.orderId)
    return ("object", id(trade))


def _sorted_values(values: Iterable[AccountValue]) -> list[AccountValue]:
    return sorted(values, key=lambda v: (v.account, v.tag.lower(), v.currency, v.modelCode))


def _normalize_tags(tags: Sequence[str] | None) -> list[str]:
    return [tag.strip() for tag in tags or () if tag and tag.strip()]


def _filter_tags(values: list[AccountValue], tags: Sequence[str] | None) -> list[AccountValue]:
    """Keep values whose tag is one of ``tags`` (case-insensitive); unknown tags are an error."""
    wanted = _normalize_tags(tags)
    if not wanted:
        return values
    available = {v.tag.lower(): v.tag for v in values}
    unknown = [tag for tag in wanted if tag.lower() not in available]
    if unknown:
        names = sorted(set(available.values()), key=str.lower)
        listed = ", ".join(names[:_MAX_LISTED_TAGS])
        if len(names) > _MAX_LISTED_TAGS:
            listed += f", and {len(names) - _MAX_LISTED_TAGS} more"
        raise InvalidRequestError(
            f"Unknown tag(s) for this account: {', '.join(unknown)}. Available: {listed}."
        )
    keep = {tag.lower() for tag in wanted}
    return [v for v in values if v.tag.lower() in keep]


def _has_values(*values: float) -> bool:
    return any(clean_float(value) is not None for value in values)


# --- service ------------------------------------------------------------------------------
