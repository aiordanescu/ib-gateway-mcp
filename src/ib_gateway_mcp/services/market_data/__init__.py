"""Quote snapshots and streaming quotes, depth, tick-by-tick data and bars.

Snapshots (:meth:`MarketDataService.quotes`) use ``reqTickersAsync``, which keys its
request as ``"snapshot"`` in ib_async's ticker bookkeeping, so it never disturbs a
streaming ``reqMktData`` for the same contract. Streams go through the
:class:`~ib_gateway_mcp.subscriptions.SubscriptionRegistry`: one stream per contract and
kind (ib_async keeps one ``Ticker`` per contract, and a second request would cut the
first one loose), opened only when the registry has a free slot.

ib_async 2.1.0 behaviour handled here:

* Errors on a streaming request (no market data permission, too many depth streams...)
  never fail anything: ib_async only emits ``errorEvent``. Each stream listens for errors
  on its own request id, waits briefly after opening for the first data or an error
  (:attr:`MarketDataService.settle_seconds`), and turns an early error into a clear tool
  error; later errors show up in the stream's snapshot (``active``, ``error``,
  ``notices``).
* ``ticker.ticks`` and ``ticker.tickByTicks`` are cleared on every incoming packet, so
  tick-by-tick ring buffers are filled from ``ticker.updateEvent``. ib_async stamps
  ticks with the packet's arrival time, not IBKR's tick time.
* ``RealTimeBarList`` and live ``BarDataList`` objects grow without bound; the streams
  here consume or trim them.
* A ``keepUpToDate`` bar request whose backfill is empty never updates (ib_async ignores
  updates to an empty list), so such a subscription is refused.
* After a reconnect ib_async has forgotten every ticker and subscription; resubscribing
  re-requests the stream and follows the new ``Ticker`` or bar list.

IBKR paces real-time bars and bar backfills like historical requests, and a violation
(162, 420) blocks the history tools too, so both openers go through
the gateway's :class:`~ib_gateway_mcp.services._pacing.HistoricalPacing` first, and a
backfill holds one of its open-request slots while it loads. The
backfill can take up to ``IB_REQUEST_TIMEOUT``, so it runs outside the registry's open
lock (``slow_open``) and does not hold up other subscribe calls.

The package splits the work: :mod:`.service` holds the service, ``_streams`` one class
per stream kind, ``_convert`` the converters from ib_async objects to the models, and
``_constants`` the limits and the hints for IBKR's error codes.
"""

from ib_gateway_mcp.services.market_data._constants import (
    BARS_KEPT_MAX,
    DATA_LIMIT_DEFAULT,
    DATA_LIMIT_MAX,
    DEPTH_ROWS_MAX,
    MAX_QUOTE_CONTRACTS,
    REALTIME_BUFFER_MAX,
    TICK_BUFFER_MAX,
)
from ib_gateway_mcp.services.market_data.service import MarketDataService

__all__ = [
    "BARS_KEPT_MAX",
    "DATA_LIMIT_DEFAULT",
    "DATA_LIMIT_MAX",
    "DEPTH_ROWS_MAX",
    "MAX_QUOTE_CONTRACTS",
    "REALTIME_BUFFER_MAX",
    "TICK_BUFFER_MAX",
    "MarketDataService",
]
