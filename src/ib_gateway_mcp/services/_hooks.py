"""Callbacks ib_async 2.1.0 drops or never implements, hooked on the wrapper instance.

The TWS decoder parses every message and calls ``getattr(wrapper, name, None)`` at
dispatch time, so an attribute set on the ``Wrapper`` *instance* takes effect at once,
even for callbacks the class lacks. The services use this for:

* stub callbacks: ``softDollarTiers``, ``familyCodes`` (advisor), ``positionMulti`` /
  ``positionMultiEnd`` (account, FA model positions);
* callbacks ib_async has no method for: ``displayGroupList`` (admin), ``replaceFAEnd``
  (advisor);
* callbacks that lose information: ``completedOrder``, whose order state the account
  service keeps, and ``tickNews``, which drops the request id (news:
  :class:`TickNewsRouter`).

Short-lived requests hook a callback for their duration with :func:`hooked`; the news
router stays installed for the connection. Two more instance patches live next to their
only user: the admin service routes ``displayGroupUpdated`` to its display-group
subscriptions, and ``ConnectionManager`` patches ``userInfo`` (a compatibility shim).
"""

from __future__ import annotations

import logging
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any

from ib_async import NewsTick
from ib_async.wrapper import Wrapper

__all__ = ["WRAPPER_NEWS_TICKS", "HeadlineRoute", "TickNewsRouter", "hooked"]

logger = logging.getLogger(__name__)

WRAPPER_NEWS_TICKS = 1000
"""ib_async appends every headline to ``wrapper.newsTicks`` forever; keep this many."""

_MISSING = object()


_ACTIVE_HOOKS = "_ib_gateway_mcp_active_hooks"
"""Instance attribute listing the callbacks :func:`hooked` currently replaces."""


@contextmanager
def hooked(wrapper: object, name: str, handler: Callable[..., None]) -> Iterator[None]:
    """Set ``wrapper.<name>`` (an ib_async callback) to ``handler`` for a while.

    Afterwards the previous state comes back: an instance attribute that was there is
    restored; otherwise the instance attribute is removed, so the class's method (if
    any) shows through again. Anything else found by attribute lookup (a test double's
    child mock) is put back as an instance attribute.

    Not re-entrant: one hook per callback name at a time. Callers serialize with the
    request lock of that callback (``connection.request_lock``); overlapping hooks would
    restore each other's handlers in the wrong order, so a second one raises instead.

    Raises:
        RuntimeError: ``name`` is already hooked on this wrapper.
    """
    own = vars(wrapper)
    active: set[str] = own.setdefault(_ACTIVE_HOOKS, set())
    if name in active:
        raise RuntimeError(
            f"ib_async callback {name} is already hooked; hold its request lock around "
            "hooked() so requests using it run one at a time"
        )
    shadowed = name in own
    previous = own[name] if shadowed else getattr(wrapper, name, _MISSING)
    setattr(wrapper, name, handler)
    active.add(name)
    try:
        yield
    finally:
        active.discard(name)
        if shadowed or (previous is not _MISSING and not isinstance(previous, types.MethodType)):
            setattr(wrapper, name, previous)
        else:
            with suppress(AttributeError):  # removed meanwhile
                delattr(wrapper, name)


HeadlineRoute = Callable[[NewsTick], None]
"""Receives the headlines of one live news request."""


class TickNewsRouter:
    """Routes ``tickNews`` callbacks to streams by request id.

    ib_async's ``Wrapper.tickNews`` receives the request id but drops it; this wraps the
    method on one wrapper instance, calls the original (so ``IB.tickNewsEvent`` still
    fires) and hands the headline to the stream that owns the request id. It also caps
    ``wrapper.newsTicks``, which ib_async grows without bound.
    """

    def __init__(self, wrapper: Wrapper) -> None:
        self.wrapper = wrapper
        self._routes: dict[int, HeadlineRoute] = {}
        original = wrapper.tickNews

        def tick_news(req_id: int, *fields: Any) -> None:
            # fields: timeStamp, providerCode, articleId, headline, extraData
            original(req_id, *fields)
            self._trim()
            route = self._routes.get(req_id)
            if route is None:
                return
            try:
                route(NewsTick(*fields))
            except Exception:
                logger.exception("Handling a headline for request %s failed", req_id)

        wrapper.tickNews = tick_news  # type: ignore[method-assign,assignment]

    def add(self, req_id: int, route: HeadlineRoute) -> None:
        """Send the headlines of request ``req_id`` to ``route``."""
        self._routes[req_id] = route

    def remove(self, req_id: int, route: HeadlineRoute | None = None) -> None:
        """Stop routing request ``req_id`` (only if it still goes to ``route``, when given).

        After a reconnect ib_async hands out request ids again from the start, so an old
        stream's id may already route a newer stream's headlines.
        """
        if route is None or self._routes.get(req_id) == route:
            self._routes.pop(req_id, None)

    def _trim(self) -> None:
        ticks = getattr(self.wrapper, "newsTicks", None)
        if isinstance(ticks, list) and len(ticks) > WRAPPER_NEWS_TICKS:
            del ticks[: len(ticks) - WRAPPER_NEWS_TICKS]
