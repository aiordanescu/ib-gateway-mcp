"""Models for order specs, previews, submissions, modifications and cancellations.

Inputs (``*Spec``) are validated per order type, so a spec that passes validation has
every price its order type needs and none it cannot use. Outputs describe what a
preview would send (:class:`OrderPreview`), what a submit did (:class:`OrderResult`)
and where one order stands (:class:`OrderStatusOut`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    model_validator,
)

from ib_gateway_mcp.models._base import _Spec, invalid_request_on
from ib_gateway_mcp.models.algos import (
    AdaptiveAlgo,
    AlgoSpec,
    ArrivalPxAlgo,
    ClosePxAlgo,
    PctVolAlgo,
    TwapAlgo,
    VwapAlgo,
)
from ib_gateway_mcp.models.common import Action, ContractOut, ContractSpec, PlainText

__all__ = [
    "AUX_PRICE_TYPES",
    "LIMIT_PRICE_TYPES",
    "REQUIRED_AUX_PRICE_TYPES",
    "REQUIRED_LIMIT_PRICE_TYPES",
    "TRAILING_TYPES",
    "AdaptiveAlgo",
    "AlgoSpec",
    "ArrivalPxAlgo",
    "BracketEntryType",
    "BracketSpec",
    "BracketTimeInForce",
    "CancelScope",
    "ClosePxAlgo",
    "ComboLegInput",
    "ComboOrderLegSpec",
    "ComboOrderType",
    "ComboSpec",
    "ExerciseAction",
    "ExerciseSpec",
    "FillOut",
    "ModifySpec",
    "OcaSpec",
    "OcaType",
    "OrderLine",
    "OrderLogEntry",
    "OrderPreview",
    "OrderResult",
    "OrderRole",
    "OrderSpec",
    "OrderStatusOut",
    "OrderType",
    "PctVolAlgo",
    "PreviewKind",
    "ResultKind",
    "SoftDollarTierRef",
    "TimeInForce",
    "TwapAlgo",
    "VwapAlgo",
    "WhatIfOut",
    "invalid_request_on",
    "order_price_problems",
    "tif_problems",
]

OrderType = Literal[
    "MKT",
    "LMT",
    "STP",
    "STP LMT",
    "TRAIL",
    "TRAIL LIMIT",
    "REL",
    "MIT",
    "LIT",
    "MOC",
    "LOC",
    "MIDPRICE",
    "PEG MID",
    "PEG MKT",
]
"""IBKR order types this server places (``Order.orderType``)."""

TimeInForce = Literal["DAY", "GTC", "IOC", "GTD", "OPG", "FOK"]
"""Time in force. GTD needs ``good_till_date``; OPG (at the open) only with MKT or LMT."""

OcaType = Literal[1, 2, 3]
"""OCA behaviour: 1 cancel the rest (with block), 2 reduce the rest (with block),
3 reduce the rest (without block)."""

BracketEntryType = Literal["LMT", "MKT", "STP", "STP LMT"]
"""Order types for a bracket's entry order."""

BracketTimeInForce = Literal["DAY", "GTC", "GTD"]
"""Time in force for all three orders of a bracket."""

ComboOrderType = Literal["LMT", "MKT", "REL"]
"""Order types for combo (BAG) orders."""

ExerciseAction = Literal["exercise", "lapse"]
"""Exercise the options, or let them lapse (expire unexercised)."""

CancelScope = Literal["this_client", "global"]
"""Which working orders ``preview_cancel_all_orders`` targets."""

PreviewKind = Literal["order", "bracket", "oca", "combo", "modify", "exercise", "cancel_all"]
"""What a preview token submits."""

ResultKind = Literal[
    "order", "bracket", "oca", "combo", "modify", "exercise", "cancel_all", "cancel"
]
"""What an :class:`OrderResult` reports on."""

OrderRole = Literal[
    "order",
    "entry",
    "take_profit",
    "stop_loss",
    "oca_member",
    "combo",
    "modify",
    "exercise",
    "cancel",
]
"""The part an order plays in a previewed action."""

# --- per-type price rules -------------------------------------------------------------

REQUIRED_LIMIT_PRICE_TYPES = frozenset({"LMT", "STP LMT", "LIT", "LOC"})
"""Order types that need ``limit_price``."""
LIMIT_PRICE_TYPES = REQUIRED_LIMIT_PRICE_TYPES | {"REL", "MIDPRICE", "TRAIL LIMIT", "PEG MID"}
"""Order types that accept ``limit_price`` (REL, MIDPRICE, PEG MID: an optional price cap)."""
REQUIRED_AUX_PRICE_TYPES = frozenset({"STP", "STP LMT", "MIT", "LIT"})
"""Order types that need ``aux_price`` as their stop or trigger price."""
TRAILING_TYPES = frozenset({"TRAIL", "TRAIL LIMIT"})
"""Trailing order types: ``aux_price`` (trailing amount) or ``trailing_percent``."""
AUX_PRICE_TYPES = REQUIRED_AUX_PRICE_TYPES | TRAILING_TYPES | {"REL", "PEG MID", "PEG MKT"}
"""Order types that accept ``aux_price`` (REL, PEG MID, PEG MKT: the offset from the peg)."""

_TIF_OPEN_TYPES = frozenset({"MKT", "LMT"})
_AT_CLOSE_TYPES = frozenset({"MOC", "LOC"})


def order_price_problems(
    order_type: str,
    *,
    limit_price: float | None,
    aux_price: float | None,
    trailing_percent: float | None = None,
    trail_stop_price: float | None = None,
    limit_price_offset: float | None = None,
) -> list[str]:
    """List what is wrong with an order's prices for its type (empty when fine)."""
    problems: list[str] = []
    if order_type in REQUIRED_LIMIT_PRICE_TYPES and limit_price is None:
        problems.append(f"{order_type} orders need limit_price")
    if order_type not in LIMIT_PRICE_TYPES and limit_price is not None:
        problems.append(f"{order_type} orders take no limit_price")
    if order_type in REQUIRED_AUX_PRICE_TYPES:
        if aux_price is None:
            problems.append(f"{order_type} orders need aux_price (the stop or trigger price)")
        elif aux_price <= 0:
            problems.append(f"{order_type} orders need a positive aux_price")
    if order_type not in AUX_PRICE_TYPES and aux_price is not None:
        problems.append(f"{order_type} orders take no aux_price")
    problems += _trailing_problems(
        order_type,
        limit_price=limit_price,
        aux_price=aux_price,
        trailing_percent=trailing_percent,
        trail_stop_price=trail_stop_price,
        limit_price_offset=limit_price_offset,
    )
    return problems


def _trailing_problems(
    order_type: str,
    *,
    limit_price: float | None,
    aux_price: float | None,
    trailing_percent: float | None,
    trail_stop_price: float | None,
    limit_price_offset: float | None,
) -> list[str]:
    problems: list[str] = []
    if order_type in TRAILING_TYPES:
        if (aux_price is None) == (trailing_percent is None):
            problems.append(
                f"{order_type} orders need exactly one of aux_price (trailing amount) and "
                "trailing_percent"
            )
        elif aux_price is not None and aux_price <= 0:
            problems.append("the trailing amount (aux_price) must be positive")
    else:
        if trailing_percent is not None:
            problems.append("trailing_percent is only for TRAIL and TRAIL LIMIT orders")
        if trail_stop_price is not None:
            problems.append("trail_stop_price is only for TRAIL and TRAIL LIMIT orders")
    if order_type == "TRAIL LIMIT":
        if (limit_price is None) == (limit_price_offset is None):
            problems.append(
                "TRAIL LIMIT orders need exactly one of limit_price and limit_price_offset"
            )
        if trail_stop_price is None:
            # IBKR rejects a TRAIL LIMIT without one (321 "Please enter a stop price").
            problems.append("TRAIL LIMIT orders need trail_stop_price (the initial stop price)")
    elif limit_price_offset is not None:
        problems.append("limit_price_offset is only for TRAIL LIMIT orders")
    return problems


def tif_problems(order_type: str, tif: str, good_till_date: object | None) -> list[str]:
    """List what is wrong with an order's time in force (empty when fine).

    ``good_till_date`` only matters by its presence (a datetime, or IBKR's string form).
    """
    problems: list[str] = []
    has_date = good_till_date is not None and good_till_date != ""
    if tif == "GTD" and not has_date:
        problems.append("tif GTD needs good_till_date")
    if tif != "GTD" and has_date:
        problems.append("good_till_date is only used with tif GTD")
    if tif == "OPG" and order_type not in _TIF_OPEN_TYPES:
        problems.append("tif OPG (at the open) only works with MKT or LMT orders")
    if order_type in _AT_CLOSE_TYPES and tif != "DAY":
        problems.append(f"{order_type} orders need tif DAY")
    return problems


def _raise_problems(problems: list[str]) -> None:
    if problems:
        raise ValueError("; ".join(problems))


_PositivePrice = Annotated[float, Field(gt=0, allow_inf_nan=False)]
_Quantity = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class SoftDollarTierRef(_Spec):
    """A soft dollar tier, as ``get_soft_dollar_tiers`` lists it."""

    name: PlainText = Field(min_length=1, max_length=64, description="The tier's name.")
    value: PlainText = Field(min_length=1, max_length=64, description="The tier's value.")


MODEL_CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$"
"""Model codes are identifiers: letters, digits, space, dot, underscore and hyphen."""

_ModelCode = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=MODEL_CODE_PATTERN,
        description=(
            "Advisor model portfolio to trade within the account (FA logins; "
            "get_positions/get_account_values take the same model_code). Letters, "
            "digits, space, dot, underscore and hyphen."
        ),
    ),
]

_ALGO_ORDER_TYPES = frozenset({"MKT", "LMT"})


# --- order specs ----------------------------------------------------------------------


class OrderSpec(_Spec):
    """One order: the instrument, side, size, type, prices and time in force.

    Prices by ``order_type``:

    * MKT, MOC: no prices.
    * LMT, LOC: ``limit_price``.
    * STP: ``aux_price`` = stop price. STP LMT: ``aux_price`` (stop) + ``limit_price``.
    * MIT: ``aux_price`` = trigger. LIT: ``aux_price`` (trigger) + ``limit_price``.
    * TRAIL: ``aux_price`` (trailing amount) or ``trailing_percent``; optional
      ``trail_stop_price`` (initial stop).
    * TRAIL LIMIT: as TRAIL, but ``trail_stop_price`` is required, plus ``limit_price``
      or ``limit_price_offset``.
    * REL: optional ``aux_price`` (offset from the peg) and ``limit_price`` (cap).
    * MIDPRICE: optional ``limit_price`` (cap).
    * PEG MID: optional ``aux_price`` (offset from the midpoint) and ``limit_price``
      (cap). PEG MKT: optional ``aux_price`` (offset from the market).

    Not supported yet: order conditions, allocation across an FA group (fa_group,
    fa_method), cash quantity, scale, hedge and adjustable-stop orders, and the order
    types PEG BEST, PEG STK, PEG BENCH, MKT PRT, STP PRT, MTL, BOX TOP, VOL and SNAP.
    """

    contract: ContractSpec = Field(description="The instrument (not a combo).")
    action: Action = Field(description="BUY or SELL.")
    quantity: _Quantity = Field(description="Number of shares or contracts (positive).")
    order_type: OrderType = Field(
        description=(
            "MKT, LMT, STP, STP LMT, TRAIL, TRAIL LIMIT, REL, MIT, LIT, MOC, LOC, MIDPRICE, "
            "PEG MID, PEG MKT."
        )
    )
    limit_price: _PositivePrice | None = Field(
        None,
        description="Limit price (LMT, STP LMT, LIT, LOC; a cap for REL, MIDPRICE, PEG MID).",
    )
    aux_price: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = Field(
        None,
        description=(
            "Stop price (STP, STP LMT), trigger price (MIT, LIT), trailing amount (TRAIL, "
            "TRAIL LIMIT) or peg offset (REL, PEG MID, PEG MKT)."
        ),
    )
    trailing_percent: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] | None = Field(
        None, description="Trailing distance in percent (TRAIL, TRAIL LIMIT), e.g. 2.5."
    )
    trail_stop_price: _PositivePrice | None = Field(
        None,
        description="Initial stop price of a trailing order (optional for TRAIL, required for "
        "TRAIL LIMIT).",
    )
    limit_price_offset: _PositivePrice | None = Field(
        None, description="TRAIL LIMIT: distance of the limit from the trailing stop."
    )
    tif: TimeInForce = Field("DAY", description="DAY, GTC, IOC, GTD, OPG or FOK.")
    good_till_date: AwareDatetime | None = Field(
        None, description="Expiry for tif GTD, ISO 8601 with a time zone."
    )
    good_after_time: AwareDatetime | None = Field(
        None,
        description="Do not start working the order before this time (ISO 8601 with a zone).",
    )
    outside_rth: bool = Field(False, description="Allow filling outside regular trading hours.")
    all_or_none: bool = Field(False, description="Fill the whole quantity at once or not at all.")
    hidden: bool = Field(
        False, description="Do not show the order in the book (NASDAQ-routed orders only)."
    )
    display_size: int | None = Field(
        None,
        ge=1,
        description="Iceberg: show only this many shares at a time (less than quantity).",
    )
    algo: AlgoSpec | None = Field(
        None, description="Optional IBKR algo (MKT or LMT orders, SMART routing)."
    )
    model_code: _ModelCode | None = None
    soft_dollar_tier: SoftDollarTierRef | None = Field(
        None, description="Soft dollar tier for the commission (from get_soft_dollar_tiers)."
    )
    order_ref: PlainText | None = Field(
        None, max_length=64, description="Free-text reference stored with the order (one line)."
    )

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        problems = order_price_problems(
            self.order_type,
            limit_price=self.limit_price,
            aux_price=self.aux_price,
            trailing_percent=self.trailing_percent,
            trail_stop_price=self.trail_stop_price,
            limit_price_offset=self.limit_price_offset,
        )
        problems += tif_problems(self.order_type, self.tif, self.good_till_date)
        if self.algo is not None and self.order_type not in _ALGO_ORDER_TYPES:
            problems.append(f"algos work with MKT or LMT orders, not {self.order_type}")
        if self.contract.sec_type == "BAG":
            problems.append("combos (BAG) go through preview_combo_order")
        if self.display_size is not None:
            if self.display_size >= self.quantity:
                problems.append("display_size must be less than quantity (it is the shown part)")
            if self.hidden:
                problems.append("hidden orders show nothing, so they take no display_size")
        if (
            self.good_after_time is not None
            and self.good_till_date is not None
            and self.good_after_time >= self.good_till_date
        ):
            problems.append("good_after_time must be before good_till_date")
        _raise_problems(problems)
        return self


class BracketSpec(_Spec):
    """An entry order with a take-profit limit and a stop-loss stop attached.

    The children only work once the entry fills; when one of them fills, IBKR cancels
    the other. For a BUY the stop loss must be below the take profit (and below the
    entry price when there is one); for a SELL the other way round.
    """

    contract: ContractSpec
    action: Action
    quantity: _Quantity
    entry_type: BracketEntryType = "LMT"
    entry_price: _PositivePrice | None = Field(
        None, description="Limit price (LMT, STP LMT) or stop price (STP) of the entry."
    )
    entry_stop_price: _PositivePrice | None = Field(
        None, description="Stop (trigger) price of an STP LMT entry."
    )
    take_profit_price: _PositivePrice
    stop_loss_price: _PositivePrice
    tif: BracketTimeInForce = "DAY"
    good_till_date: AwareDatetime | None = None
    outside_rth: bool = False
    model_code: _ModelCode | None = None
    soft_dollar_tier: SoftDollarTierRef | None = Field(
        None, description="Soft dollar tier for all three orders (from get_soft_dollar_tiers)."
    )
    order_ref: PlainText | None = Field(None, max_length=64)

    @model_validator(mode="after")
    def _check_bracket(self) -> Self:
        problems: list[str] = []
        if self.entry_type == "MKT" and self.entry_price is not None:
            problems.append("a MKT entry takes no entry_price")
        if self.entry_type != "MKT" and self.entry_price is None:
            problems.append(f"a {self.entry_type} entry needs entry_price")
        if (self.entry_type == "STP LMT") != (self.entry_stop_price is not None):
            problems.append("entry_stop_price is required for, and only used by, STP LMT entries")
        if self.contract.sec_type == "BAG":
            problems.append("combos (BAG) cannot be bracketed here")
        problems += tif_problems("LMT", self.tif, self.good_till_date)
        low, high = (
            (self.stop_loss_price, self.take_profit_price)
            if self.action == "BUY"
            else (self.take_profit_price, self.stop_loss_price)
        )
        low_name, high_name = (
            ("stop_loss_price", "take_profit_price")
            if self.action == "BUY"
            else ("take_profit_price", "stop_loss_price")
        )
        if low >= high:
            problems.append(f"for a {self.action} bracket {low_name} must be below {high_name}")
        elif self.entry_price is not None and not low < self.entry_price < high:
            problems.append(
                f"for a {self.action} bracket entry_price must lie between {low_name} and "
                f"{high_name}"
            )
        _raise_problems(problems)
        return self


class OcaSpec(_Spec):
    """2-10 orders in one One-Cancels-All group."""

    orders: list[OrderSpec] = Field(min_length=2, max_length=10)
    oca_type: OcaType = 1


class ComboOrderLegSpec(_Spec):
    """One leg of a combo order, described by its contract."""

    contract: ContractSpec = Field(description="The leg's instrument (qualified to a con_id).")
    ratio: int = Field(1, ge=1, le=1000, description="Units of this leg per combo unit.")
    action: Action = Field(description="BUY or SELL for this leg when the combo is bought.")

    @model_validator(mode="after")
    def _check_leg(self) -> Self:
        if self.contract.sec_type == "BAG":
            raise ValueError("a combo leg cannot itself be a combo")
        return self


ComboLegInput = ComboOrderLegSpec
"""Former name of :class:`ComboOrderLegSpec`, kept as an alias."""


class ComboSpec(_Spec):
    """A multi-leg (BAG) order: spreads, strangles, pairs."""

    legs: list[ComboOrderLegSpec] = Field(min_length=2, max_length=8)
    action: Action
    quantity: _Quantity
    order_type: ComboOrderType = "LMT"
    limit_price: Annotated[float, Field(allow_inf_nan=False)] | None = Field(
        None, description="Net price per combo unit; negative for a credit."
    )
    tif: Literal["DAY", "GTC", "IOC"] = "DAY"
    outside_rth: bool = False
    non_guaranteed: bool = Field(
        False,
        description=(
            "Let IBKR fill the legs separately (needed for stock pairs and legs on different "
            "underlyings); a leg may then fill without the others."
        ),
    )
    model_code: _ModelCode | None = None
    soft_dollar_tier: SoftDollarTierRef | None = Field(
        None, description="Soft dollar tier for the commission (from get_soft_dollar_tiers)."
    )
    order_ref: PlainText | None = Field(None, max_length=64)

    @model_validator(mode="after")
    def _check_combo(self) -> Self:
        problems: list[str] = []
        if self.order_type == "LMT" and self.limit_price is None:
            problems.append("LMT combo orders need limit_price")
        if self.order_type == "MKT" and self.limit_price is not None:
            problems.append("MKT combo orders take no limit_price")
        _raise_problems(problems)
        return self


class ModifySpec(_Spec):
    """Changes to a working order; unset fields keep their current value."""

    quantity: _Quantity | None = Field(None, description="New total quantity.")
    limit_price: _PositivePrice | None = Field(None, description="New limit price.")
    aux_price: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = Field(
        None, description="New stop/trigger price, trailing amount or offset."
    )
    trailing_percent: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] | None = None
    trail_stop_price: _PositivePrice | None = None
    tif: TimeInForce | None = None
    good_till_date: AwareDatetime | None = None
    outside_rth: bool | None = None

    @model_validator(mode="after")
    def _check_changes(self) -> Self:
        if not self.model_fields_set or all(
            getattr(self, name) is None for name in self.model_fields_set
        ):
            raise ValueError("give at least one field to change")
        return self


class ExerciseSpec(_Spec):
    """Exercise (or let lapse) option contracts held in the account."""

    contract: ContractSpec
    action: ExerciseAction
    quantity: int = Field(ge=1, description="Number of contracts.")
    override: bool = Field(
        False,
        description="Override IBKR's automatic action (e.g. exercise out of the money).",
    )


# --- outputs --------------------------------------------------------------------------


class WhatIfOut(BaseModel):
    """IBKR's what-if check: margin and equity impact and the commission estimate.

    Margin and equity values are in the account's base currency; null means IBKR did not
    report the value.
    """

    status: str | None = Field(None, description="Status the order would get (what-if).")
    init_margin_change: float | None = None
    maint_margin_change: float | None = None
    equity_with_loan_change: float | None = None
    init_margin_after: float | None = None
    maint_margin_after: float | None = None
    equity_with_loan_after: float | None = None
    commission: float | None = None
    min_commission: float | None = None
    max_commission: float | None = None
    commission_currency: str | None = None
    warning_text: str | None = Field(None, description="IBKR's warning about this order.")


class OrderLine(BaseModel):
    """One order (or action) inside a preview, as it would be sent."""

    role: OrderRole
    description: str = Field(description="One-line summary, e.g. 'BUY 10 AAPL STK LMT 150.00'.")
    contract: ContractOut
    action: str | None = None
    quantity: float | None = None
    order_type: str | None = None
    limit_price: float | None = None
    aux_price: float | None = None
    trailing_percent: float | None = None
    trail_stop_price: float | None = None
    limit_price_offset: float | None = None
    tif: str | None = None
    good_till_date: str | None = Field(None, description="As sent to IBKR (UTC).")
    good_after_time: str | None = Field(None, description="As sent to IBKR (UTC).")
    outside_rth: bool = False
    all_or_none: bool = False
    hidden: bool = False
    display_size: int | None = None
    algo_strategy: str | None = None
    algo_params: dict[str, str] = Field(default_factory=dict)
    model_code: str | None = None
    soft_dollar_tier: str | None = Field(None, description="Name of the soft dollar tier.")
    oca_group: str | None = None
    order_id: int | None = Field(None, description="The existing order (modify, cancel).")
    reference_price: float | None = Field(
        None, description="Market price used for the notional check, when one was needed."
    )
    notional: float | None = Field(
        None, description="Estimated notional in the contract's currency (order limits)."
    )
    what_if: WhatIfOut | None = None


class OrderPreview(BaseModel):
    """A validated, what-if checked action waiting for ``submit_order(token)``."""

    token: str = Field(description="Pass to submit_order; single-use.")
    expires_at: datetime = Field(description="The token is refused after this time (UTC).")
    account: str
    is_paper: bool = Field(description="False means real money: a human may have to confirm.")
    kind: PreviewKind
    summary: str = Field(description="What submit_order will do, in one line.")
    orders: list[OrderLine] = Field(description="Each order (or cancellation) in the action.")
    what_if: WhatIfOut | None = Field(
        None, description="What-if of the main order (entry, first OCA member, the combo)."
    )
    details: list[str] = Field(
        default_factory=list, description="Lines shown to a human who confirms a live order."
    )
    warnings: list[str] = Field(default_factory=list)


class FillOut(BaseModel):
    """One execution of an order."""

    exec_id: str
    time: datetime | None = None
    shares: float
    price: float | None = None
    exchange: str | None = None
    commission: float | None = Field(None, description="Null until IBKR reports it.")
    commission_currency: str | None = None
    realized_pnl: float | None = None


class OrderLogEntry(BaseModel):
    """A status change or message in an order's life."""

    time: datetime | None = None
    status: str
    message: str | None = None
    error_code: int | None = None


class OrderStatusOut(BaseModel):
    """Where one order stands: status, fills, remaining quantity and its log."""

    account: str | None = None
    order_id: int | None = Field(None, description="Id within the placing API client.")
    perm_id: int | None = Field(None, description="IBKR's permanent id (unique per login).")
    client_id: int | None = None
    parent_id: int | None = None
    role: OrderRole | None = None
    placed_by_this_server: bool = Field(
        description="Only orders placed by this server's client id can be modified or cancelled."
    )
    contract: ContractOut
    action: str
    quantity: float
    order_type: str
    limit_price: float | None = None
    aux_price: float | None = None
    tif: str | None = None
    oca_group: str | None = None
    order_ref: str | None = None
    status: str = Field(
        description=(
            "PendingSubmit, PreSubmitted, Submitted, Filled, PendingCancel, Cancelled, "
            "Inactive (rejected or not accepted), ValidationError (IBKR warned; see log)."
        )
    )
    filled: float
    remaining: float
    avg_fill_price: float | None = None
    last_fill_price: float | None = None
    why_held: str | None = None
    fills: list[FillOut] = Field(default_factory=list)
    log: list[OrderLogEntry] = Field(default_factory=list)


class OrderResult(BaseModel):
    """What ``submit_order`` or ``cancel_order`` did."""

    kind: ResultKind
    account: str
    accepted: bool = Field(description="False when IBKR rejected or refused the action.")
    status: str = Field(
        description=(
            "Status of the main order; Sent/Rejected for exercises; for a cancel-all: "
            "Cancelled (all of them), PartlyCancelled (some filled or ended otherwise first; "
            "see messages), PendingCancel or NothingCancelled."
        )
    )
    order_id: int | None = None
    perm_id: int | None = None
    filled: float | None = None
    remaining: float | None = None
    avg_fill_price: float | None = None
    order_ids: list[int] = Field(default_factory=list)
    orders: list[OrderStatusOut] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list, description="IBKR errors and warnings.")
