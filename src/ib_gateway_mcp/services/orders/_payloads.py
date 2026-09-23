"""What a preview token stores: orders and contracts as plain JSON.

The payload stays a plain dict (the token store digests its canonical JSON); the
TypedDicts below only let mypy check every key the submit path reads. Orders and
contracts are stored field by field (``_ORDER_FIELDS``, ``_CONTRACT_FIELDS``), so they
stay ``dict[str, Any]``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Required, TypedDict, cast

from ib_async import ComboLeg, Contract, Order, SoftDollarTier, TagValue, Trade
from ib_async.util import UNSET_DOUBLE

from ib_gateway_mcp._util import clean_float, clean_int
from ib_gateway_mcp.services.orders._constants import (
    _CONTRACT_FIELDS,
    _ORDER_FIELDS,
    _ORDER_PRICE_FIELDS,
    _ORDER_TAG_FIELDS,
)
from ib_gateway_mcp.services.orders._describe import _price, _quantity

if TYPE_CHECKING:
    from ib_gateway_mcp.models.orders import CancelScope, ExerciseSpec, OrderRole
    from ib_gateway_mcp.safety.tokens import PreviewRecord


class _OrderEntry(TypedDict):
    """One order to place: its role, contract, order and parent (an index into the list)."""

    role: OrderRole
    contract: dict[str, Any]
    order: dict[str, Any]
    parent: int | None


class _ModifyData(TypedDict):
    """The working order a modification changes, as the preview saw it."""

    order_id: int
    perm_id: int | None
    original: dict[str, Any]
    filled: float


class _ExerciseData(TypedDict):
    """The arguments of ``exerciseOptions``."""

    contract: dict[str, Any]
    action_code: int
    quantity: int
    override: int


class _CancelEntry(TypedDict):
    """One working order a cancel-all listed."""

    order_id: int | None
    client_id: int | None
    perm_id: int | None


class _CancelAllData(TypedDict):
    scope: CancelScope
    orders: list[_CancelEntry]


class _Body(TypedDict, total=False):
    """What a preview adds to a token besides its description.

    ``orders`` for new orders and modifications, plus ``modify`` for a modification;
    ``exercise`` for an exercise; ``cancel_all`` for a cancel-all. ``summaries`` are the
    :class:`~ib_gateway_mcp.safety.policy.OrderSummary` dumps a submit re-checks.
    """

    summaries: Required[list[dict[str, Any]]]
    orders: list[_OrderEntry]
    modify: _ModifyData
    exercise: _ExerciseData
    cancel_all: _CancelAllData


class _Payload(_Body, total=False):
    """A stored order token: format version, description, details and the body."""

    v: Required[int]
    description: Required[str]
    details: Required[list[str]]


def _stored(record: PreviewRecord) -> _Payload:
    """The payload of an order token (the store hands back plain JSON)."""
    return cast("_Payload", record.payload)


def _order_payload(order: Order) -> dict[str, Any]:
    data: dict[str, Any] = {name: getattr(order, name) for name in _ORDER_FIELDS}
    data["totalQuantity"] = _quantity(order.totalQuantity)
    for name in _ORDER_PRICE_FIELDS:
        data[name] = _price(getattr(order, name))
    for name in _ORDER_TAG_FIELDS:
        data[name] = [[str(tag.tag), str(tag.value)] for tag in getattr(order, name) or []]
    tier = order.softDollarTier
    data["softDollarTier"] = [tier.name, tier.val, tier.displayName] if tier else None
    return data


def _order_from_payload(data: dict[str, Any]) -> Order:
    order = Order()
    for name in _ORDER_FIELDS:
        setattr(order, name, data[name])
    order.totalQuantity = float(data["totalQuantity"])
    for name in _ORDER_PRICE_FIELDS:
        value = data[name]
        setattr(order, name, UNSET_DOUBLE if value is None else float(value))
    for name in _ORDER_TAG_FIELDS:
        setattr(order, name, [TagValue(tag, value) for tag, value in data[name]])
    if data["softDollarTier"]:
        order.softDollarTier = SoftDollarTier(*data["softDollarTier"])
    return order


def _contract_payload(contract: Contract) -> dict[str, Any]:
    data: dict[str, Any] = {name: getattr(contract, name) for name in _CONTRACT_FIELDS}
    data["strike"] = clean_float(contract.strike) or 0.0
    data["comboLegs"] = [
        [leg.conId, leg.ratio, leg.action, leg.exchange] for leg in contract.comboLegs or []
    ]
    return data


def _contract_from_payload(data: dict[str, Any]) -> Contract:
    contract = Contract(**{name: data[name] for name in _CONTRACT_FIELDS}, strike=data["strike"])
    contract.comboLegs = [
        ComboLeg(conId=con_id, ratio=ratio, action=action, exchange=exchange)
        for con_id, ratio, action, exchange in data["comboLegs"]
    ]
    return contract


def _exercise_data(spec: ExerciseSpec, contract: Contract) -> _ExerciseData:
    return {
        "contract": _contract_payload(contract),
        "action_code": 1 if spec.action == "exercise" else 2,
        "quantity": spec.quantity,
        "override": 1 if spec.override else 0,
    }


def _cancel_all_data(scope: CancelScope, targets: Sequence[Trade]) -> _CancelAllData:
    """The orders a cancel-all covers, by order, client and perm id."""
    return {
        "scope": scope,
        "orders": [
            {
                "order_id": clean_int(trade.order.orderId),
                "client_id": clean_int(trade.order.clientId),
                "perm_id": clean_int(trade.order.permId) or None,
            }
            for trade in targets
        ],
    }
