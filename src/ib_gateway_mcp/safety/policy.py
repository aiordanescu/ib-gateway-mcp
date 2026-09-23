"""Order limits: symbol, security-type and currency allowlists, max quantity, max notional.

The orders service describes each order as an :class:`OrderSummary` and calls
:meth:`OrderPolicy.check` twice: at preview and again at submit, so a limit
tightened in between still applies.

Notional rules (all amounts in the order's currency; there is no FX conversion,
so ``max_notional`` is 10,000 GBP for a GBP order and 10,000 USD for a USD one:
set ``allowed_currencies`` to make it a cap in one currency). The notional is
``|quantity| x |price| x multiplier``, and the price must be one the fill cannot
exceed, because the model chooses the order fields:

* **BUY limit orders** (LMT, STP LMT, LIT, LOC, LOO): the limit price. It caps
  the fill price, so it bounds the exposure however far from the market it is.
* **Everything else** (SELL limits, whose limit is a floor; MKT, MOC, STP, MIT,
  TRAIL, REL, PEG...): the larger of the order's own price (the limit price, or
  ``aux_price`` for STP, STP LMT, STP PRT, MIT and LIT, where it is a trigger
  price rather than an offset) and the ``reference_price`` (last price or a
  what-if derived price). Without a reference price the order is refused, since
  nothing bounds its fill.
* **Multipliers:** a missing multiplier counts as 1, except for derivatives
  (OPT, FOP, FUT, CONTFUT, WAR, IOPT, including combo legs), where the order is
  refused: qualify the contract so its multiplier is known. ``priceMagnifier``
  (prices quoted in cents, for example) is not applied: it can only make the
  estimate larger than the real notional, never smaller.
* **Other price conventions:** bonds (BOND) are priced in percent of a face value
  per unit that the order does not carry, and event contracts (EVENT) risk
  ``1 - price`` per contract on a sale, so ``quantity x price`` would understate
  both. While ``max_notional`` is set, orders on them (and combos with such legs)
  are refused.
* **Combos (legs present):** the gross sum of
  ``|ratio x quantity| x |leg price| x leg multiplier``, which needs a price for
  every leg. The combo's own net price never stands in: a net limit (even a BUY
  limit) bounds neither leg, e.g. BUY a cheap stock and SELL an expensive one for
  a 0.01 debit, and a non-guaranteed combo can fill its legs one at a time. So a
  combo with an unpriced leg has no notional and is refused while
  ``max_notional`` is set.
* If ``max_notional`` is set and no bounding price is available, the order is
  refused.

For combos, every leg symbol must be allowed (the combo's own symbol is not
checked), the combo sec type (``BAG``) and every leg sec type must be
allowed, and each leg's effective quantity (``ratio x quantity``) must be
within ``max_quantity`` as well as the combo quantity itself.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Annotated, Final, Protocol

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from ib_gateway_mcp._util import exact_decimal
from ib_gateway_mcp.errors import OrderLimitError

__all__ = [
    "LegSummary",
    "NotionalEstimate",
    "OrderPolicy",
    "OrderSummary",
    "PolicySettings",
    "estimate_notional",
]

# ib_async marks unset prices with sys.float_info.max (ib_async.util.UNSET_DOUBLE).
_UNSET_DOUBLE: Final = sys.float_info.max

#: Order types whose ``aux_price`` is a stop or trigger price, not an offset.
AUX_PRICE_ORDER_TYPES: Final = frozenset({"STP", "STP LMT", "STP PRT", "MIT", "LIT"})

#: Order types whose limit price caps the fill price of a BUY.
CAPPING_LIMIT_ORDER_TYPES: Final = frozenset({"LMT", "STP LMT", "LIT", "LOC", "LOO"})

#: Security types whose notional is meaningless without the contract multiplier.
MULTIPLIER_REQUIRED_SEC_TYPES: Final = frozenset({"OPT", "FOP", "FUT", "CONTFUT", "WAR", "IOPT"})

#: Security types whose notional ``quantity x price x multiplier`` would understate:
#: refused while ``max_notional`` is set (see the module docstring).
UNBOUNDED_NOTIONAL_SEC_TYPES: Final = frozenset({"BOND", "EVENT"})

_MAX_LISTED: Final = 20


def _optional_number(value: object) -> object:
    """Map IBKR's "unset" markers (None, "", NaN, inf, UNSET_DOUBLE) to None."""
    if value is None or value == "":
        return None
    if isinstance(value, float | int | Decimal) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number) or abs(number) >= _UNSET_DOUBLE:
            return None
    return value


def _upper(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


def _strip(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


_Price = Annotated[float | None, BeforeValidator(_optional_number)]
_Multiplier = Annotated[Annotated[float, Field(gt=0)] | None, BeforeValidator(_optional_number)]
_Code = Annotated[str, BeforeValidator(_upper), Field(min_length=1)]
_Symbol = Annotated[str, BeforeValidator(_strip), Field(min_length=1)]


class LegSummary(BaseModel):
    """One leg of a combo (BAG) order, as the policy sees it.

    Attributes:
        symbol: Underlying symbol of the leg.
        sec_type: Leg security type (``STK``, ``OPT``, ``FUT``...).
        action: ``BUY`` or ``SELL``, relative to the combo.
        ratio: Leg units per combo unit.
        multiplier: Contract multiplier; ``None`` counts as 1.
        price: Per-unit reference price of the leg, if known.
        con_id: IBKR contract id, for display.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: _Symbol
    sec_type: _Code
    action: _Code
    ratio: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1.0
    multiplier: _Multiplier = None
    price: _Price = None
    con_id: int | None = None


class OrderSummary(BaseModel):
    """The facts about an order that the limits apply to.

    Unset prices (``None``, NaN, infinity or IBKR's ``UNSET_DOUBLE``) are
    normalized to ``None``, and codes (sec type, action, order type, currency)
    to upper case, so values can be copied straight from ib_async objects.

    Attributes:
        symbol: Symbol of the contract (for combos, informational only).
        sec_type: Security type; ``BAG`` for combos.
        action: ``BUY``, ``SELL`` or ``SSHORT``.
        quantity: Order quantity (combo units for combos).
        order_type: IB order type, e.g. ``LMT``, ``MKT``, ``STP LMT``.
        limit_price: Limit price, if any (may be negative for combos).
        aux_price: IB ``auxPrice``: a stop/trigger price or an offset,
            depending on the order type.
        multiplier: Contract multiplier; ``None`` counts as 1 (combos use the
            leg multipliers instead).
        currency: Currency the notional is expressed in.
        reference_price: Market or what-if derived price, used when the order
            carries no price of its own.
        legs: Combo legs; empty for single-contract orders.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: _Symbol
    sec_type: _Code = "STK"
    action: _Code
    quantity: Annotated[float, Field(allow_inf_nan=False)]
    order_type: _Code
    limit_price: _Price = None
    aux_price: _Price = None
    multiplier: _Multiplier = None
    currency: _Code = "USD"
    reference_price: _Price = None
    legs: tuple[LegSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class NotionalEstimate:
    """An order's notional value and how it was derived."""

    value: Decimal
    basis: str


class PolicySettings(Protocol):
    """The settings fields :meth:`OrderPolicy.from_settings` reads."""

    @property
    def max_notional(self) -> Decimal | float | None:
        """Largest allowed notional per order, in the order's currency."""
        ...

    @property
    def max_quantity(self) -> Decimal | float | None:
        """Largest allowed absolute quantity per order."""
        ...

    @property
    def allowed_symbols(self) -> Iterable[str]:
        """Allowed symbols; empty means any."""
        ...

    @property
    def allowed_sec_types(self) -> Iterable[str]:
        """Allowed security types; empty means any."""
        ...

    @property
    def allowed_currencies(self) -> Iterable[str]:
        """Allowed order currencies; empty means any."""
        ...


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), ",f")


def _fmt_money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _listing(values: frozenset[str]) -> str:
    ordered = sorted(values)
    shown = ", ".join(ordered[:_MAX_LISTED])
    return shown + (f", ... ({len(ordered)} total)" if len(ordered) > _MAX_LISTED else "")


def _price_basis(summary: OrderSummary) -> tuple[Decimal, str] | None:
    """The per-unit price that bounds the order's fill, per the module rules."""
    limit_price = summary.limit_price
    if (
        summary.action == "BUY"
        and summary.order_type in CAPPING_LIMIT_ORDER_TYPES
        and limit_price is not None
    ):
        return abs(exact_decimal(limit_price)), "limit price"
    if summary.reference_price is None:
        return None
    reference = abs(exact_decimal(summary.reference_price))
    own: tuple[Decimal, str] | None = None
    if limit_price is not None:
        own = abs(exact_decimal(limit_price)), "limit price"
    elif summary.aux_price is not None and summary.order_type in AUX_PRICE_ORDER_TYPES:
        own = abs(exact_decimal(summary.aux_price)), "stop/trigger price"
    if own is not None and own[0] > reference:
        return own
    return reference, "reference price"


def estimate_notional(summary: OrderSummary) -> NotionalEstimate | None:
    """Estimate an order's notional per the module rules; ``None`` if no price bounds it.

    A combo needs a price for every leg; its net price is never used (see the module
    docstring).
    """
    quantity = abs(exact_decimal(summary.quantity))
    legs = summary.legs
    if legs:
        total = Decimal(0)
        for leg in legs:
            if leg.price is None:
                return None
            leg_quantity = exact_decimal(leg.ratio) * quantity
            total += (
                leg_quantity * abs(exact_decimal(leg.price)) * exact_decimal(leg.multiplier or 1.0)
            )
        return NotionalEstimate(total, "sum over legs of ratio x quantity x leg price x multiplier")
    basis = _price_basis(summary)
    if basis is None:
        return None
    price, label = basis
    multiplier = exact_decimal(summary.multiplier) if summary.multiplier is not None else Decimal(1)
    return NotionalEstimate(
        quantity * price * multiplier,
        f"{_fmt(quantity)} x {_fmt(price)} {label} x {_fmt(multiplier)} multiplier",
    )


def _unbounded_sec_types(summary: OrderSummary) -> list[str]:
    """Describe each instrument in the order whose notional this policy cannot bound."""
    if summary.legs:
        return [
            f"combo leg {leg.symbol} ({leg.sec_type})"
            for leg in summary.legs
            if leg.sec_type in UNBOUNDED_NOTIONAL_SEC_TYPES
        ]
    if summary.sec_type in UNBOUNDED_NOTIONAL_SEC_TYPES:
        return [f"this {summary.sec_type} contract ({summary.symbol})"]
    return []


def _missing_multipliers(summary: OrderSummary) -> list[str]:
    """Describe each derivative in the order whose multiplier is unknown."""
    if summary.legs:
        return [
            f"combo leg {leg.symbol} ({leg.sec_type})"
            for leg in summary.legs
            if leg.sec_type in MULTIPLIER_REQUIRED_SEC_TYPES and leg.multiplier is None
        ]
    if summary.sec_type in MULTIPLIER_REQUIRED_SEC_TYPES and summary.multiplier is None:
        return [f"this {summary.sec_type} contract ({summary.symbol})"]
    return []


def _limit(name: str, value: Decimal | float | str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(value.strip()) if isinstance(value, str) else exact_decimal(value)
    except InvalidOperation:
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be a positive number or None, got {value!r}")
    return number


def _code_set(values: Iterable[str] | str) -> frozenset[str]:
    items = values.split(",") if isinstance(values, str) else values
    return frozenset(code for item in items if (code := item.strip().upper()))


class OrderPolicy:
    """Per-order limits, enforced at preview and again at submit.

    Every limit is optional; an empty allowlist or ``None`` limit means "no
    restriction".

    Args:
        max_notional: Largest notional per order, in the order's currency.
        max_quantity: Largest absolute quantity per order (and per combo leg).
        allowed_symbols: Allowed symbols, case-insensitive. A single
            comma-separated string is accepted too.
        allowed_sec_types: Allowed security types (include ``BAG`` to permit
            combos).
        allowed_currencies: Allowed order currencies (``USD``, ``EUR``...).

    Raises:
        ValueError: A limit is zero, negative or not finite.
    """

    def __init__(
        self,
        *,
        max_notional: Decimal | float | str | None = None,
        max_quantity: Decimal | float | str | None = None,
        allowed_symbols: Iterable[str] | str = (),
        allowed_sec_types: Iterable[str] | str = (),
        allowed_currencies: Iterable[str] | str = (),
    ) -> None:
        self._max_notional = _limit("max_notional", max_notional)
        self._max_quantity = _limit("max_quantity", max_quantity)
        self._symbols = _code_set(allowed_symbols)
        self._sec_types = _code_set(allowed_sec_types)
        self._currencies = _code_set(allowed_currencies)

    @classmethod
    def from_settings(cls, settings: PolicySettings) -> OrderPolicy:
        """Build a policy from the ``max_*`` and ``allowed_*`` settings."""
        return cls(
            max_notional=settings.max_notional,
            max_quantity=settings.max_quantity,
            allowed_symbols=settings.allowed_symbols,
            allowed_sec_types=settings.allowed_sec_types,
            allowed_currencies=getattr(settings, "allowed_currencies", ()),
        )

    @property
    def max_notional(self) -> Decimal | None:
        """Largest notional per order, or ``None``."""
        return self._max_notional

    @property
    def max_quantity(self) -> Decimal | None:
        """Largest absolute quantity per order, or ``None``."""
        return self._max_quantity

    @property
    def allowed_symbols(self) -> frozenset[str]:
        """Upper-cased allowed symbols; empty means any."""
        return self._symbols

    @property
    def allowed_sec_types(self) -> frozenset[str]:
        """Upper-cased allowed security types; empty means any."""
        return self._sec_types

    @property
    def allowed_currencies(self) -> frozenset[str]:
        """Upper-cased allowed order currencies; empty means any."""
        return self._currencies

    def describe(self) -> str:
        """One line summarizing the active limits, for server instructions and logs."""
        parts: list[str] = []
        if self._max_notional is not None:
            parts.append(f"max notional {_fmt(self._max_notional)} per order (order currency)")
        if self._max_quantity is not None:
            parts.append(f"max quantity {_fmt(self._max_quantity)}")
        if self._symbols:
            parts.append(f"symbols {_listing(self._symbols)}")
        if self._sec_types:
            parts.append(f"security types {_listing(self._sec_types)}")
        if self._currencies:
            parts.append(f"currencies {_listing(self._currencies)}")
        return "; ".join(parts) if parts else "no order limits configured"

    def check(self, summary: OrderSummary) -> None:
        """Raise :class:`OrderLimitError` listing every limit the order breaks.

        Returns ``None`` when the order is within all limits.
        """
        violations = [
            *self._symbol_violations(summary),
            *self._sec_type_violations(summary),
            *self._currency_violations(summary),
            *self._quantity_violations(summary),
            *self._notional_violations(summary),
        ]
        if violations:
            raise OrderLimitError(
                "Order refused by the server's order limits: " + "; ".join(violations) + "."
            )

    def _symbol_violations(self, summary: OrderSummary) -> list[str]:
        if not self._symbols:
            return []
        allowed = _listing(self._symbols)
        if summary.legs:
            return [
                f"combo leg symbol {leg.symbol} is not allowed (allowed symbols: {allowed})"
                for leg in summary.legs
                if leg.symbol.upper() not in self._symbols
            ]
        if summary.symbol.upper() not in self._symbols:
            return [f"symbol {summary.symbol} is not allowed (allowed symbols: {allowed})"]
        return []

    def _sec_type_violations(self, summary: OrderSummary) -> list[str]:
        if not self._sec_types:
            return []
        allowed = _listing(self._sec_types)
        out: list[str] = []
        if summary.sec_type not in self._sec_types:
            what = (
                "combo orders (BAG) are" if summary.legs else f"security type {summary.sec_type} is"
            )
            out.append(f"{what} not allowed (allowed security types: {allowed})")
        out.extend(
            f"combo leg {leg.symbol} has security type {leg.sec_type}, which is not allowed "
            f"(allowed security types: {allowed})"
            for leg in summary.legs
            if leg.sec_type not in self._sec_types
        )
        return out

    def _currency_violations(self, summary: OrderSummary) -> list[str]:
        if not self._currencies or summary.currency in self._currencies:
            return []
        return [
            f"currency {summary.currency} is not allowed "
            f"(allowed currencies: {_listing(self._currencies)})"
        ]

    def _quantity_violations(self, summary: OrderSummary) -> list[str]:
        limit = self._max_quantity
        if limit is None:
            return []
        out: list[str] = []
        quantity = abs(exact_decimal(summary.quantity))
        if quantity > limit:
            out.append(f"quantity {_fmt(quantity)} exceeds the maximum of {_fmt(limit)}")
        for leg in summary.legs:
            leg_quantity = exact_decimal(leg.ratio) * quantity
            # A ratio-1 leg equals the combo quantity, which is reported above.
            if leg_quantity > limit and leg_quantity > quantity:
                out.append(
                    f"combo leg {leg.symbol} quantity {_fmt(leg_quantity)} "
                    f"(ratio {_fmt(exact_decimal(leg.ratio))} x {_fmt(quantity)}) "
                    f"exceeds the maximum of {_fmt(limit)}"
                )
        return out

    def _notional_violations(self, summary: OrderSummary) -> list[str]:
        limit = self._max_notional
        if limit is None:
            return []
        cap = f"a maximum notional of {_fmt(limit)} {summary.currency} is set"
        unchecked = [
            f"{cap}, but the notional of {what} cannot be bounded by quantity x price "
            "(bonds are priced in percent of face value, event contracts pay out a fixed "
            "amount), so it cannot be checked; such orders need IBKR_MCP_MAX_NOTIONAL unset"
            for what in _unbounded_sec_types(summary)
        ]
        unchecked += [
            f"{cap}, but the multiplier of {what} is unknown, so the notional cannot be "
            "checked; qualify the contract first (its details carry the multiplier)"
            for what in _missing_multipliers(summary)
        ]
        if unchecked:
            return unchecked
        estimate = estimate_notional(summary)
        if estimate is None and summary.legs:
            unpriced = dict.fromkeys(
                f"{leg.symbol} ({leg.sec_type})" for leg in summary.legs if leg.price is None
            )
            return [
                f"{cap}, but combo leg {what} has no reference price, so the combo's gross "
                "notional cannot be checked (its net price bounds neither leg); make market "
                "data for every leg available"
                for what in unpriced
            ]
        if estimate is None:
            return [
                f"{cap}, but nothing bounds the fill price of this {summary.action} "
                f"{summary.order_type} order (only a BUY limit price does) and no reference "
                "price was available, so its notional cannot be checked; make market data "
                "for the contract available, or use a BUY limit order"
            ]
        if estimate.value > limit:
            return [
                f"notional {_fmt_money(estimate.value)} {summary.currency} "
                f"({estimate.basis}) exceeds the maximum of {_fmt(limit)} {summary.currency}"
            ]
        return []
