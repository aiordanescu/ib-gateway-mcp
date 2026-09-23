"""Constants of the orders service: waits, limits and the fields a token stores."""

from __future__ import annotations

from typing import Final, get_args

from ib_async import OrderStatus

from ib_gateway_mcp.models.orders import PreviewKind
from ib_gateway_mcp.safety.audit import AuditEvent

STATUS_WAIT: Final = 3.0
"""Seconds a submit or cancel waits for IBKR's first status before answering."""
EXERCISE_WAIT: Final = 3.0
"""Seconds an exercise watches for an error: IBKR sends no acknowledgement."""
REFERENCE_PRICE_TIMEOUT: Final = 12.0
"""Upper bound (seconds) for the market snapshot behind a reference price; IBKR ends a
snapshot within about 11 s."""
REFERENCE_MAX_AGE: Final = 60.0
"""A streaming ticker updated within this many seconds supplies the reference price."""
MAX_LISTED_ORDERS: Final = 50
"""How many orders a cancel-all preview lists individually."""
MAX_REMEMBERED_ORDERS: Final = 10_000
"""How many order id -> perm id pairs the service keeps for :meth:`OrdersService.order_status`."""

_PAYLOAD_VERSION: Final = 1
_ORDER_KINDS: Final = frozenset(get_args(PreviewKind))
"""Token kinds :meth:`OrdersService.submit` executes (an FA replacement has its own)."""
_LIVE_DATA: Final = 1
"""``Ticker.marketDataType`` of live (real-time) market data."""
_WAITING_STATES: Final = frozenset({"", OrderStatus.PendingSubmit, OrderStatus.ApiPending})
_CANCELLED_STATES: Final = frozenset({OrderStatus.Cancelled, OrderStatus.ApiCancelled})
_NOT_REJECTIONS: Final = frozenset({202})
"""Error codes on an order that confirm a cancellation rather than reject the order."""
_OPTION_TYPES: Final = frozenset({"OPT", "FOP"})
_DELIVERED_SEC_TYPES: Final[dict[str, str]] = {"OPT": "STK", "FOP": "FUT"}
"""What exercising an option delivers when its details do not say (``underSecType``)."""
_CASH_SETTLED_UNDERLYINGS: Final = frozenset({"IND"})
"""Underlyings whose options settle in cash: exercising them delivers no position."""
_MODIFY_ACK: Final = "Modify"
"""ib_async's log message for a modification it just sent."""
_MODIFY_REJECTING_WARNINGS: Final = frozenset({105, 110, 321, 329, 434})
"""Codes ib_async treats as warnings that mean IBKR refused a modification: 105 order
does not match the original, 110 price off the tick grid, 321 validation error, 329
modify failed, 434 zero size. The order keeps its previous terms."""
_AUX_IS_PRICE: Final = frozenset({"STP", "STP LMT", "MIT", "LIT"})
"""Order types whose ``auxPrice`` is an absolute (stop or trigger) price."""

_SUBMIT_EVENTS: Final[dict[str, AuditEvent]] = {
    "order": AuditEvent.SUBMIT,
    "bracket": AuditEvent.SUBMIT,
    "oca": AuditEvent.SUBMIT,
    "combo": AuditEvent.SUBMIT,
    "modify": AuditEvent.MODIFY,
    "exercise": AuditEvent.EXERCISE,
    "cancel_all": AuditEvent.CANCEL_ALL,
}
_ROLE_LABELS: Final[dict[str, str]] = {
    "order": "Order",
    "entry": "Entry",
    "take_profit": "Take profit",
    "stop_loss": "Stop loss",
    "oca_member": "OCA member",
    "combo": "Combo",
    "modify": "Modified order",
    "exercise": "Exercise",
    "cancel": "Cancel",
}
_OCA_TYPES: Final[dict[int, str]] = {
    1: "a fill cancels the others",
    2: "a fill reduces the others",
    3: "a fill reduces the others, no overfill protection",
}

# Order fields a preview token stores and a submit sends.
_ORDER_FIELDS: Final = (
    "action",
    "orderType",
    "tif",
    "goodTillDate",
    "goodAfterTime",
    "outsideRth",
    "allOrNone",
    "hidden",
    "displaySize",
    "orderRef",
    "algoStrategy",
    "modelCode",
    "ocaGroup",
    "ocaType",
    "transmit",
    "account",
)
_ORDER_PRICE_FIELDS: Final = (
    "lmtPrice",
    "auxPrice",
    "trailingPercent",
    "trailStopPrice",
    "lmtPriceOffset",
)
_ORDER_TAG_FIELDS: Final = ("algoParams", "smartComboRoutingParams")
_MODIFIABLE_FIELDS: Final = (
    "totalQuantity",
    "lmtPrice",
    "auxPrice",
    "trailingPercent",
    "trailStopPrice",
    "tif",
    "goodTillDate",
    "outsideRth",
)
_CONTRACT_FIELDS: Final = (
    "conId",
    "symbol",
    "secType",
    "lastTradeDateOrContractMonth",
    "right",
    "multiplier",
    "exchange",
    "primaryExchange",
    "currency",
    "localSymbol",
    "tradingClass",
)
