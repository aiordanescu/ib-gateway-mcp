"""Order previews (what-if), token-bound submission, modification, cancellation and exercise.

The flow is two-step. A ``preview_*`` method resolves the account, qualifies the
contracts, checks the order limits (:class:`~ib_gateway_mcp.safety.OrderPolicy`), runs
IBKR's what-if check and stores the exact orders behind a single-use token
(:class:`~ib_gateway_mcp.safety.PreviewStore`). :meth:`OrdersService.submit` takes only
that token: it re-checks the limits, the circuit breaker, the live-account gate and the
rate limit, places exactly what was stored, waits briefly for IBKR's first status and
reports it. Every step is written to the audit log.

ib_async 2.1.0 details handled here:

* ``whatIfOrderAsync`` picks its own request id and only answers once IBKR sends an
  order state with a margin value; warning 321 (e.g. a read-only API) never ends it.
  The what-if is sent with a private copy of the contract, and an ``errorEvent``
  carrying that exact contract object for 321 fails the check at once.
* ``placeOrder`` stamps the order id and client id on new orders; bracket children get
  their ``parentId`` from the placed parent. Only the last order of a bracket transmits.
* Modifications change the live ``Trade.order`` in place and re-send it (``transmit`` set
  again, since a bracket child was placed with ``transmit=False``).
* IBKR rejections surface on the trade (status Cancelled or Inactive with an error in
  its log; ``ValidationError`` for warnings, of which 321 means "not placed"); they count
  towards the circuit breaker. A new order refused with 321 stays ``ValidationError``
  (not a done state) in ib_async's cache, so it is marked Inactive here.
* A rejected *modification* is the trap: ib_async marks the trade Cancelled (error
  codes) or ValidationError (warnings 105, 110, 321, 329, 434) although IBKR keeps the
  original order working. The service restores the old terms on the cached order and
  re-syncs with ``reqOpenOrders`` (IBKR re-sends each working order's status), and
  cancel/modify/status re-sync before trusting a trade that an error marked Cancelled.
* A modification that changes nothing in the order status (e.g. the price of a
  PreSubmitted stop) produces no ``orderStatus`` event, only an ``openOrder`` echo, so
  the modify submit also listens to ``openOrderEvent``.
* Completed orders loaded from IBKR (``reqCompletedOrders``, and the sync after a
  reconnect) carry no order id; the service remembers the perm id of each order it
  placed so ``order_status(order_id=...)`` still finds them.
* Only this client's orders stay current in ib_async's trade cache. Other clients'
  and manual orders are read from a fresh ``reqAllOpenOrders`` answer (then
  ``reqCompletedOrders``) every time, never from the cache, whose copies of them stop
  at the snapshot that brought them in.
* ``exerciseOptions`` is fire-and-forget and hides its request id, so exercises go
  through ``ib.client`` with an id from ``client.getReqId()`` to attribute errors.

The package splits the work: :mod:`.service` holds the public methods, and the
``_preview``, ``_submit`` and ``_cancel`` mixins the plumbing behind them. ``_planning``
builds ib_async orders from specs, ``_payloads`` turns them into the JSON a token
stores, ``_trades`` reads trade state and ``_describe`` writes the one-line
descriptions.
"""

from ib_gateway_mcp.services.orders._constants import (
    EXERCISE_WAIT,
    MAX_LISTED_ORDERS,
    MAX_REMEMBERED_ORDERS,
    REFERENCE_MAX_AGE,
    REFERENCE_PRICE_TIMEOUT,
    STATUS_WAIT,
)
from ib_gateway_mcp.services.orders.service import OrdersService

__all__ = [
    "EXERCISE_WAIT",
    "MAX_LISTED_ORDERS",
    "MAX_REMEMBERED_ORDERS",
    "REFERENCE_MAX_AGE",
    "REFERENCE_PRICE_TIMEOUT",
    "STATUS_WAIT",
    "OrdersService",
]
