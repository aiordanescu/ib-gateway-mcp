"""Descriptions of orders and contracts, what-if results, reference prices and
price increments (market rules)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TypeGuard

from ib_async import Contract, Order, OrderState, Ticker
from ib_async.objects import PriceIncrement

from ib_gateway_mcp._util import clean_float, clean_str, ensure_utc, exact_decimal, utc_now
from ib_gateway_mcp.models.common import MARKET_DATA_TYPE_NAMES, quoted
from ib_gateway_mcp.models.orders import TRAILING_TYPES, WhatIfOut, order_price_problems
from ib_gateway_mcp.safety.policy import CAPPING_LIMIT_ORDER_TYPES
from ib_gateway_mcp.services.orders._constants import _AUX_IS_PRICE, _LIVE_DATA, REFERENCE_MAX_AGE

# --- small conversions ------------------------------------------------------------------


def _price(value: object) -> float | None:
    """A price or amount, or None for IBKR's unset markers, NaN and blanks."""
    if isinstance(value, Decimal):
        value = float(value)
    if value is None or isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    return clean_float(value)


def _quantity(value: object) -> float:
    return _price(value) or 0.0


def _num(value: float) -> str:
    """Compact number for descriptions: 10, 0.5, 1234.5678."""
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _money(value: float) -> str:
    """A price for descriptions: two decimals unless more are significant."""
    return f"{value:.2f}" if round(value, 2) == value else _num(value)


def _amount(value: float) -> str:
    return f"{value:,.2f}"


def _symbol(contract: Contract) -> str:
    return (
        clean_str(contract.symbol) or clean_str(contract.localSymbol) or f"con_id {contract.conId}"
    )


def _multiplier(contract: Contract) -> float | None:
    value = clean_float(contract.multiplier or None)
    return value if value is not None and value > 0 else None


def _contract_label(contract: Contract) -> str:
    """``AAPL STK``, ``SPY OPT 20261218 500 C``, ``ES FUT 202612``."""
    parts = [_symbol(contract), contract.secType or "STK"]
    if contract.lastTradeDateOrContractMonth:
        parts.append(contract.lastTradeDateOrContractMonth)
    strike = clean_float(contract.strike)
    if strike:
        parts.append(f"{strike:g}")
    if contract.right:
        parts.append(contract.right)
    if contract.currency and contract.currency != "USD":
        parts.append(contract.currency)
    return " ".join(parts)


def _price_words(order: Order) -> list[str]:
    order_type = order.orderType
    limit = _price(order.lmtPrice)
    aux = _price(order.auxPrice)
    words: list[str] = []
    if order_type in TRAILING_TYPES:
        percent = _price(order.trailingPercent)
        if aux is not None:
            words.append(f"trail {_money(aux)}")
        elif percent is not None:
            words.append(f"trail {_num(percent)}%")
        stop = _price(order.trailStopPrice)
        if stop is not None:
            words.append(f"stop {_money(stop)}")
        offset = _price(order.lmtPriceOffset)
        if offset is not None:
            words.append(f"limit offset {_money(offset)}")
        elif limit is not None:
            words.append(f"limit {_money(limit)}")
        return words
    if aux is not None:
        label = {"STP": "stop", "STP LMT": "stop", "MIT": "trigger", "LIT": "trigger"}.get(
            order_type, "offset"
        )
        words.append(f"{label} {_money(aux)}")
    if limit is not None:
        words.append(_money(limit) if order_type in ("LMT", "LOC") else f"limit {_money(limit)}")
    return words


def _describe_order(contract: Contract, order: Order, *, label: str | None = None) -> str:
    """``BUY 10 AAPL STK LMT 150.00 DAY``: the order in one line."""
    parts = [
        order.action,
        _num(_quantity(order.totalQuantity)),
        label or _contract_label(contract),
        order.orderType,
        *_price_words(order),
        order.tif or "DAY",
    ]
    if order.goodTillDate:
        parts.append(f"until {order.goodTillDate} UTC")
    if order.goodAfterTime:
        parts.append(f"from {order.goodAfterTime} UTC")
    if order.outsideRth:
        parts.append("outside RTH")
    if order.allOrNone:
        parts.append("all-or-none")
    if order.hidden:
        parts.append("hidden")
    if order.displaySize:
        parts.append(f"display {order.displaySize}")
    if order.algoStrategy:
        parts.append(f"algo {order.algoStrategy}")
    # Text the requester chose (model code, tier name) is quoted, so it cannot pass
    # for words of this server in a confirmation prompt.
    if order.modelCode:
        parts.append(f"model {quoted(order.modelCode)}")
    if order.softDollarTier:
        parts.append(f"soft dollar tier {quoted(order.softDollarTier.name)}")
    return " ".join(parts)


def _what_if_out(state: OrderState) -> WhatIfOut:
    return WhatIfOut(
        status=clean_str(state.status),
        init_margin_change=clean_float(state.initMarginChange),
        maint_margin_change=clean_float(state.maintMarginChange),
        equity_with_loan_change=clean_float(state.equityWithLoanChange),
        init_margin_after=clean_float(state.initMarginAfter),
        maint_margin_after=clean_float(state.maintMarginAfter),
        equity_with_loan_after=clean_float(state.equityWithLoanAfter),
        commission=clean_float(state.commission),
        min_commission=clean_float(state.minCommission),
        max_commission=clean_float(state.maxCommission),
        commission_currency=clean_str(state.commissionCurrency),
        warning_text=clean_str(state.warningText),
    )


def _what_if_lines(what_if: WhatIfOut) -> list[str]:
    lines: list[str] = []
    for label, value in (
        ("Initial margin change", what_if.init_margin_change),
        ("Maintenance margin change", what_if.maint_margin_change),
        ("Equity with loan change", what_if.equity_with_loan_change),
    ):
        if value is not None:
            lines.append(f"{label}: {_amount(value)}")
    currency = f" {what_if.commission_currency}" if what_if.commission_currency else ""
    low, high = what_if.min_commission, what_if.max_commission
    if what_if.commission is not None:
        lines.append(f"Commission: {_amount(what_if.commission)}{currency}")
    elif low is not None and high is not None and low != high:
        lines.append(f"Commission: {_amount(low)} to {_amount(high)}{currency}")
    elif low is not None or high is not None:
        lines.append(f"Commission: {_amount(low if low is not None else high or 0.0)}{currency}")
    if what_if.warning_text:
        lines.append(f"IBKR warning: {what_if.warning_text}")
    return lines


@dataclass(frozen=True, slots=True)
class _Reference:
    """A reference price for the notional check, and what kind of data it came from."""

    price: float
    data_type: int
    """``Ticker.marketDataType``: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen."""
    close_only: bool = False
    """Only the previous close was available (no last, bid, ask or mark price)."""

    @property
    def is_live(self) -> bool:
        return self.data_type == _LIVE_DATA and not self.close_only

    def describe(self) -> str:
        """``delayed price 100.50``, ``previous close 99.00``."""
        kind = MARKET_DATA_TYPE_NAMES.get(self.data_type, f"type {self.data_type}")
        kind = kind.replace("_", "-")
        if self.close_only:
            close = f"previous close {_money(self.price)}"
            return close if self.data_type == _LIVE_DATA else f"{close} ({kind} data)"
        return f"{kind} price {_money(self.price)}"


def _ticker_price(ticker: Ticker) -> _Reference | None:
    """The highest current price a ticker shows (conservative for a notional bound)."""
    data_type = ticker.marketDataType if isinstance(ticker.marketDataType, int) else _LIVE_DATA
    values = [clean_float(v) for v in (ticker.last, ticker.ask, ticker.bid, ticker.markPrice)]
    prices = [v for v in values if v is not None and v > 0]
    if prices:
        return _Reference(max(prices), data_type)
    close = clean_float(ticker.close)
    if close is None or close <= 0:
        return None
    return _Reference(close, data_type, close_only=True)


def _is_fresh(ticker: object) -> TypeGuard[Ticker]:
    """True for a ticker updated within :data:`REFERENCE_MAX_AGE` seconds.

    ib_async keeps every ticker it ever created (snapshots included), so a cached one
    may hold prices from long ago; only a recently updated one (a live stream) counts.
    """
    if not isinstance(ticker, Ticker) or not isinstance(ticker.time, datetime):
        return False
    return (utc_now() - ensure_utc(ticker.time)).total_seconds() <= REFERENCE_MAX_AGE


def _needs_reference(order: Order) -> bool:
    """False when the order's own limit price caps its fill (BUY limit types)."""
    return not (
        order.action == "BUY"
        and order.orderType in CAPPING_LIMIT_ORDER_TYPES
        and _price(order.lmtPrice) is not None
    )


def _unset_or_zero(value: object) -> float | None:
    """A price as the order-type rules see it: IBKR's unset markers and 0 count as unset."""
    price = _price(value)
    return None if price is None or price == 0 else price


def _price_problems(order: Order) -> list[str]:
    """:func:`order_price_problems` for an ib_async order (0 and unset mean no price)."""
    return order_price_problems(
        order.orderType,
        limit_price=_unset_or_zero(order.lmtPrice),
        aux_price=_unset_or_zero(order.auxPrice),
        trailing_percent=_unset_or_zero(order.trailingPercent),
        trail_stop_price=_unset_or_zero(order.trailStopPrice),
        limit_price_offset=_unset_or_zero(order.lmtPriceOffset),
    )


# --- price increments (market rules) -----------------------------------------------------


def _step_at(ladder: Sequence[PriceIncrement], price: Decimal) -> Decimal | None:
    """The price increment a market-rule ladder sets for ``price``."""
    step: Decimal | None = None
    for rung in sorted(ladder, key=lambda rung: rung.lowEdge):
        if exact_decimal(rung.lowEdge) <= price:
            step = exact_decimal(rung.increment)
    return step if step is not None and step > 0 else None


def _tick_prices(order: Order) -> list[tuple[str, float]]:
    """The absolute prices of an order (offsets and trailing amounts are left out)."""
    prices: list[tuple[str, float]] = []
    limit = _price(order.lmtPrice)
    if limit is not None:
        prices.append(("limit price", limit))
    aux = _price(order.auxPrice)
    if aux is not None and order.orderType in _AUX_IS_PRICE:
        label = "stop price" if order.orderType.startswith("STP") else "trigger price"
        prices.append((label, aux))
    trail_stop = _price(order.trailStopPrice)
    if trail_stop is not None:
        prices.append(("trailing stop price", trail_stop))
    return prices


def _tick_problem(
    label: str, price: float, ladders: Sequence[Sequence[PriceIncrement]]
) -> str | None:
    """Why ``price`` is off every candidate ladder's grid, or None when it fits one."""
    value = abs(exact_decimal(price))
    steps = [step for ladder in ladders if (step := _step_at(ladder, value)) is not None]
    if not steps or any(value % step == 0 for step in steps):
        return None
    step = steps[0]
    below = (value // step) * step
    return (
        f"{label} {_num(price)} is not a multiple of the price increment "
        f"{format(step.normalize(), 'f')} at that price (e.g. {format(below.quantize(step), 'f')}"
        f" or {format((below + step).quantize(step), 'f')})"
    )
