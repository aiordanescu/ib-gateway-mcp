"""Building ib_async orders from specs and modifications, before their checks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ib_async import Contract, ContractDetails, Order, SoftDollarTier, TagValue, Trade
from ib_async.util import UNSET_DOUBLE

from ib_gateway_mcp._util import clean_float, clean_int, clean_str, contract_to_out
from ib_gateway_mcp.models.orders import (
    AUX_PRICE_TYPES,
    LIMIT_PRICE_TYPES,
    TRAILING_TYPES,
    CancelScope,
    ExerciseSpec,
    ModifySpec,
    OrderLine,
    OrderRole,
    OrderSpec,
    SoftDollarTierRef,
    WhatIfOut,
    tif_problems,
)
from ib_gateway_mcp.safety.policy import OrderSummary
from ib_gateway_mcp.services._ibtime import ib_utc_stamp
from ib_gateway_mcp.services.orders._constants import (
    _CASH_SETTLED_UNDERLYINGS,
    _DELIVERED_SEC_TYPES,
    _MODIFIABLE_FIELDS,
    MAX_LISTED_ORDERS,
)
from ib_gateway_mcp.services.orders._describe import (
    _contract_label,
    _describe_order,
    _money,
    _multiplier,
    _num,
    _price,
    _quantity,
    _Reference,
    _symbol,
)


@dataclass
class _Planned:
    """One order of a previewed action, before and after its checks."""

    role: OrderRole
    contract: Contract
    order: Order
    description: str
    parent: int | None = None
    legs: list[Contract] = field(default_factory=list)
    reference_price: float | None = None
    reference: _Reference | None = None
    """Where ``reference_price`` came from (live or not), when one was fetched."""
    summary: OrderSummary | None = None
    notional: float | None = None
    what_if: WhatIfOut | None = None
    order_id: int | None = None
    details: ContractDetails | None = None
    """Contract details, when known: their market rules check the price increments."""

    def line(self) -> OrderLine:
        return _order_line(
            self.role,
            self.description,
            self.contract,
            self.order,
            order_id=self.order_id,
            reference_price=self.reference_price,
            notional=self.notional,
            what_if=self.what_if,
        )


def _order_line(
    role: OrderRole,
    description: str,
    contract: Contract,
    order: Order,
    *,
    order_id: int | None = None,
    reference_price: float | None = None,
    notional: float | None = None,
    what_if: WhatIfOut | None = None,
) -> OrderLine:
    """An ib_async order as a preview line: every term the human is asked about."""
    return OrderLine(
        role=role,
        description=description,
        contract=contract_to_out(contract),
        action=order.action,
        quantity=_quantity(order.totalQuantity),
        order_type=order.orderType,
        limit_price=_price(order.lmtPrice),
        aux_price=_price(order.auxPrice),
        trailing_percent=_price(order.trailingPercent),
        trail_stop_price=_price(order.trailStopPrice),
        limit_price_offset=_price(order.lmtPriceOffset),
        tif=clean_str(order.tif),
        good_till_date=clean_str(order.goodTillDate),
        good_after_time=clean_str(order.goodAfterTime),
        outside_rth=bool(order.outsideRth),
        all_or_none=bool(order.allOrNone),
        hidden=bool(order.hidden),
        display_size=order.displaySize or None,
        algo_strategy=clean_str(order.algoStrategy),
        algo_params={str(tag.tag): str(tag.value) for tag in order.algoParams or []},
        model_code=clean_str(order.modelCode),
        soft_dollar_tier=clean_str(order.softDollarTier.name) if order.softDollarTier else None,
        oca_group=clean_str(order.ocaGroup),
        order_id=order_id,
        reference_price=reference_price,
        notional=notional,
        what_if=what_if,
    )


def _new_order(
    *,
    action: str,
    quantity: float,
    order_type: str,
    account: str,
    tif: str,
    good_till_date: datetime | None = None,
    outside_rth: bool = False,
    order_ref: str | None = None,
    limit_price: float | None = None,
    aux_price: float | None = None,
    transmit: bool = True,
    model_code: str | None = None,
    soft_dollar_tier: SoftDollarTierRef | None = None,
) -> Order:
    order = Order(
        action=action,
        totalQuantity=quantity,
        orderType=order_type,
        tif=tif,
        outsideRth=outside_rth,
        orderRef=order_ref or "",
        account=account,
        transmit=transmit,
        modelCode=(model_code or "").strip(),
    )
    if good_till_date is not None:
        order.goodTillDate = ib_utc_stamp(good_till_date)
    if limit_price is not None:
        order.lmtPrice = limit_price
    if aux_price is not None:
        order.auxPrice = aux_price
    if soft_dollar_tier is not None:
        order.softDollarTier = SoftDollarTier(soft_dollar_tier.name, soft_dollar_tier.value)
    return order


def _order_from_spec(spec: OrderSpec, account: str) -> Order:
    order = _new_order(
        action=spec.action,
        quantity=spec.quantity,
        order_type=spec.order_type,
        account=account,
        tif=spec.tif,
        good_till_date=spec.good_till_date,
        outside_rth=spec.outside_rth,
        order_ref=spec.order_ref,
        limit_price=spec.limit_price,
        aux_price=spec.aux_price,
        model_code=spec.model_code,
        soft_dollar_tier=spec.soft_dollar_tier,
    )
    if spec.good_after_time is not None:
        order.goodAfterTime = ib_utc_stamp(spec.good_after_time)
    order.allOrNone = spec.all_or_none
    order.hidden = spec.hidden
    if spec.display_size is not None:
        order.displaySize = spec.display_size
    if spec.trailing_percent is not None:
        order.trailingPercent = spec.trailing_percent
    if spec.trail_stop_price is not None:
        order.trailStopPrice = spec.trail_stop_price
    if spec.limit_price_offset is not None:
        order.lmtPriceOffset = spec.limit_price_offset
    if spec.algo is not None:
        order.algoStrategy = spec.algo.strategy
        order.algoParams = [TagValue(tag, value) for tag, value in spec.algo.tag_values()]
    return order


def _modify_problems(order: Order, changes: ModifySpec) -> list[str]:
    """What a modification cannot change on an order of this type."""
    order_type = order.orderType
    problems: list[str] = []
    if changes.limit_price is not None and order_type not in LIMIT_PRICE_TYPES:
        problems.append(f"{order_type} orders have no limit price to change")
    if changes.aux_price is not None and order_type not in AUX_PRICE_TYPES:
        problems.append(f"{order_type} orders have no aux price to change")
    if order_type not in TRAILING_TYPES and (
        changes.trailing_percent is not None or changes.trail_stop_price is not None
    ):
        problems.append("trailing_percent and trail_stop_price only apply to trailing orders")
    if changes.aux_price is not None and changes.trailing_percent is not None:
        problems.append("give either aux_price (trailing amount) or trailing_percent, not both")
    if changes.tif is not None or changes.good_till_date is not None:
        tif = changes.tif or order.tif or "DAY"
        date: object | None = changes.good_till_date
        if date is None and tif == "GTD":
            date = order.goodTillDate or None
        problems.extend(tif_problems(order_type, tif, date))
    return problems


def _apply_changes(order: Order, changes: ModifySpec) -> None:
    if changes.quantity is not None:
        order.totalQuantity = changes.quantity
    if changes.limit_price is not None:
        order.lmtPrice = changes.limit_price
        if order.orderType == "TRAIL LIMIT":  # a fixed limit replaces a limit offset
            order.lmtPriceOffset = UNSET_DOUBLE
    if changes.aux_price is not None:
        order.auxPrice = changes.aux_price
        if order.orderType in TRAILING_TYPES:  # a trailing amount replaces a percentage
            order.trailingPercent = UNSET_DOUBLE
    if changes.trailing_percent is not None:
        order.trailingPercent = changes.trailing_percent
        order.auxPrice = UNSET_DOUBLE
    if changes.trail_stop_price is not None:
        order.trailStopPrice = changes.trail_stop_price
    if changes.tif is not None:
        order.tif = changes.tif
        if changes.tif != "GTD":
            order.goodTillDate = ""
    if changes.good_till_date is not None:
        order.goodTillDate = ib_utc_stamp(changes.good_till_date)
    if changes.outside_rth is not None:
        order.outsideRth = changes.outside_rth


def _modifiable_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    keys = ("action", "orderType", *_MODIFIABLE_FIELDS)
    return {key: data[key] for key in keys}


def _delivered_summary(
    contract: Contract, details: ContractDetails, quantity: int, action: str
) -> OrderSummary | None:
    """What exercising ``quantity`` options delivers, as the order limits see it.

    OPT delivers ``quantity x multiplier`` of the underlying stock, FOP ``quantity``
    futures, at the strike. Cash-settled (index) options deliver no position: None.
    """
    under = (details.underSecType or "").strip().upper() or _DELIVERED_SEC_TYPES.get(
        contract.secType, ""
    )
    if not under or under in _CASH_SETTLED_UNDERLYINGS:
        return None
    strike = clean_float(contract.strike)
    multiplier = _multiplier(contract)
    if under == "FUT":
        size, unit_multiplier = float(quantity), multiplier
    else:
        size, unit_multiplier = float(quantity) * (multiplier or 1.0), None
    return OrderSummary(
        symbol=_symbol(contract),
        sec_type=under,
        action=action,
        quantity=size,
        order_type="LMT",
        limit_price=strike,
        reference_price=strike,
        multiplier=unit_multiplier,
        currency=contract.currency or "USD",
    )


# --- exercise and cancel-all ----------------------------------------------------------------


def _exercise_summary(contract: Contract, quantity: int) -> OrderSummary:
    """An exercise as the order limits see it: ``quantity`` options traded at the strike.

    A call exercise buys the underlying, a put exercise sells it.
    """
    strike = clean_float(contract.strike)
    return OrderSummary(
        symbol=_symbol(contract),
        sec_type=contract.secType,
        action="BUY" if contract.right.upper().startswith("C") else "SELL",
        quantity=quantity,
        order_type="LMT",
        limit_price=strike,
        reference_price=strike,
        multiplier=_multiplier(contract),
        currency=contract.currency or "USD",
    )


def _exercise_text(spec: ExerciseSpec, contract: Contract, action: str) -> tuple[str, list[str]]:
    """The one-line description of an exercise or lapse, and the details under it."""
    strike = clean_float(contract.strike)
    multiplier = _multiplier(contract)
    verb = "EXERCISE" if spec.action == "exercise" else "LAPSE"
    description = f"{verb} {spec.quantity} {_contract_label(contract)}"
    if multiplier:
        description += f" (x{_num(multiplier)})"
    if spec.override:
        description += " with override"
    details = ["Irreversible: IBKR sends no confirmation; check positions afterwards."]
    if spec.action == "exercise" and strike and multiplier:
        underlying = "buys" if action == "BUY" else "sells"
        details.append(
            f"Exercising {underlying} {_num(spec.quantity * multiplier)} "
            f"{_symbol(contract)} at {_money(strike)} {contract.currency}."
        )
    if spec.action == "lapse":
        details.append("The options expire unexercised; any in-the-money value is lost.")
    if spec.override:
        details.append("Override: IBKR's automatic exercise decision is overridden.")
    return description, details


def _cancel_lines(targets: Sequence[Trade]) -> list[OrderLine]:
    """One preview line per working order a cancel-all would cancel."""
    return [
        _order_line(
            "cancel",
            "CANCEL " + _describe_order(trade.contract, trade.order),
            trade.contract,
            trade.order,
            order_id=clean_int(trade.order.orderId) or None,
        )
        for trade in targets
    ]


def _cancel_all_text(
    scope: CancelScope, count: int, own: int, account: str
) -> tuple[str, list[str]]:
    """The description of a cancel-all and its warnings (the list is cut at
    :data:`MAX_LISTED_ORDERS`)."""
    if scope == "global":
        description = (
            f"CANCEL ALL {count} working orders on this login (every account, every API "
            "client and manual TWS orders; IBKR global cancel)"
        )
    else:
        description = (
            f"CANCEL {count} working order(s) placed by this server (client id {own}) in "
            f"account {account}"
        )
    warnings: list[str] = []
    if count > MAX_LISTED_ORDERS:
        warnings.append(f"Only the first {MAX_LISTED_ORDERS} of {count} orders are listed.")
    if scope == "global":
        warnings.append(
            "Global cancel also cancels orders placed after this preview and orders of other "
            "programs using this login."
        )
    return description, warnings
