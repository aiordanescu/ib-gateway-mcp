"""Every read or write of ib_async 2.1.0 internals, in one place.

The services need a few pieces of ib_async's private request bookkeeping: to resolve a
request whose callback ib_async lacks, to find the request id behind a future, to fail
futures that a manual disconnect would leave pending, and to find the request id of a
streaming ticker or P&L subscription. ``ib-async`` is pinned (``==2.1.0``) because of
this; ``tests/unit/test_ib_compat.py`` checks that every attribute used here still
exists, so a version bump fails there first instead of deep inside a service.
"""

from __future__ import annotations

import asyncio
from collections.abc import Hashable, Mapping
from typing import Any, Final

__all__ = [
    "WRAPPER_INTERNALS",
    "end_request",
    "fail_pending_requests",
    "pending_request_id",
    "requests_for_contract",
    "subscription_request_id",
    "ticker_request_id",
]

WRAPPER_INTERNALS: Final = (
    "_endReq",
    "_futures",
    "_reqId2Contract",
    "ticker2ReqId",
    "pnlKey2ReqId",
    "pnlSingleKey2ReqId",
)
"""The ``ib_async.wrapper.Wrapper`` attributes this module relies on."""


def end_request(wrapper: object, key: Hashable, result: object = None) -> None:
    """Resolve (and forget) ib_async's pending request ``key`` (``Wrapper._endReq``).

    Without ``result`` the request's collected results (or ``[]``) resolve it; a request
    already finished or unknown is ignored.
    """
    end = wrapper._endReq  # type: ignore[attr-defined]
    if result is None:
        end(key)
    else:
        end(key, result)


def _futures(wrapper: object) -> dict[Any, asyncio.Future[Any]]:
    futures = getattr(wrapper, "_futures", None)
    return futures if isinstance(futures, dict) else {}


def pending_request_id(wrapper: object, future: object) -> int | None:
    """The request id ib_async registered ``future`` under, if it is still pending."""
    return next(
        (
            key
            for key, value in _futures(wrapper).items()
            if value is future and isinstance(key, int)
        ),
        None,
    )


def fail_pending_requests(wrapper: object, reason: str) -> int:
    """Fail every pending ib_async request future with ``ConnectionError(reason)``.

    ``IB.disconnect()`` resets the wrapper, which drops the pending futures without
    resolving them (only a socket-side drop fails them), so their callers would wait out
    their full timeout. Call this before a deliberate disconnect. Returns how many
    futures were failed.
    """
    failed = 0
    for future in list(_futures(wrapper).values()):
        if isinstance(future, asyncio.Future) and not future.done():
            future.set_exception(ConnectionError(reason))
            failed += 1
    return failed


def requests_for_contract(wrapper: object, contract: object) -> list[int]:
    """The pending request ids ib_async filed under this exact contract object."""
    pending = getattr(wrapper, "_reqId2Contract", None)
    if not isinstance(pending, dict):
        return []
    return [key for key, value in pending.items() if value is contract and isinstance(key, int)]


def ticker_request_id(wrapper: object, ticker: object, request_key: str) -> int | None:
    """The request id of ``ticker``'s ``request_key`` stream (``Wrapper.ticker2ReqId``)."""
    mapping = getattr(wrapper, "ticker2ReqId", None)
    if not isinstance(mapping, Mapping):
        return None
    by_ticker = mapping.get(request_key)
    if not isinstance(by_ticker, Mapping):
        return None
    req_id = by_ticker.get(ticker)
    return req_id if isinstance(req_id, int) else None


def subscription_request_id(wrapper: object, attribute: str, key: tuple[Any, ...]) -> int | None:
    """The request id of a P&L subscription (``pnlKey2ReqId`` or ``pnlSingleKey2ReqId``)."""
    mapping = getattr(wrapper, attribute, None)
    if isinstance(mapping, dict):
        req_id = mapping.get(key)
        return req_id if isinstance(req_id, int) else None
    return None
