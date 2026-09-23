"""Base class shared by every domain service."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from ib_async import IB, Contract, ContractDetails
from ib_async.wrapper import RequestError

from ib_gateway_mcp._ib_compat import end_request
from ib_gateway_mcp._util import best_effort, contract_from_spec, subscription_out
from ib_gateway_mcp.errors import (
    ConfirmationUnavailableError,
    IbApiError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RequestTimeoutError,
)
from ib_gateway_mcp.models.common import (
    ContractOut,
    ContractSpec,
    SubscriptionDataOut,
    SubscriptionKind,
    SubscriptionOut,
)
from ib_gateway_mcp.services._hooks import hooked
from ib_gateway_mcp.services._resolve import (
    MAX_LISTED_CANDIDATES,
    NO_SECURITY_DEFINITION,
    describe_spec,
    details_request,
    not_found_message,
    pick_details,
)
from ib_gateway_mcp.subscriptions import OpenFn, Stream

if TYPE_CHECKING:
    from ib_gateway_mcp.accounts import AccountScope
    from ib_gateway_mcp.config import Settings
    from ib_gateway_mcp.connection import ConnectionManager
    from ib_gateway_mcp.gateway import Gateway
    from ib_gateway_mcp.safety import SafetyRails
    from ib_gateway_mcp.subscriptions import SubscriptionRegistry

__all__ = [
    "MAX_LISTED_CANDIDATES",
    "REQUEST_ENDING_WARNINGS",
    "SHARED_REQUEST_KEYS",
    "BaseService",
    "describe_spec",
    "market_rule_key",
]


SHARED_REQUEST_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "reqCurrentTimeAsync": "currentTime",
        "reqAccountUpdatesAsync": "accountValues",
        "reqOpenOrdersAsync": "openOrders",
        "reqAllOpenOrdersAsync": "openOrders",
        "reqCompletedOrdersAsync": "completedOrders",
        "reqPositionsAsync": "positions",
        "reqMktDepthExchangesAsync": "mktDepthExchanges",
        "reqScannerParametersAsync": "scannerParams",
        "reqNewsProvidersAsync": "newsProviders",
        "requestFAAsync": "requestFA",
    }
)
"""ib_async 2.1.0 requests keyed by a fixed string rather than a request id.

A second concurrent call replaces the first one's future, so the first caller would
hang until the timeout. Call them through ``_call(lambda: ..., exclusive=KEY)`` with the
key from this table (both open-order requests share ``"openOrders"``).
``reqMarketRuleAsync`` is keyed per rule id: use :func:`market_rule_key`.
"""


def market_rule_key(rule_id: int) -> str:
    """ib_async's fixed key for ``reqMarketRuleAsync(rule_id)`` (one per rule id).

    Use it as ``_call(..., exclusive=market_rule_key(rule_id))`` wherever market rules
    are requested, so concurrent requests for the same rule share one lock.
    """
    return f"marketRule-{rule_id}"


REQUEST_ENDING_WARNINGS = frozenset({321})
"""Codes ib_async treats as warnings (so the request never completes) that, for a
request id we are waiting on, mean the request was rejected. 321 is "error validating
request", e.g. a what-if order on a gateway whose API is read-only."""

type Request[T] = Awaitable[T] | Callable[[], Awaitable[T]]


class BaseService:
    """Gives a domain service access to the connection, accounts and subscriptions.

    Services are the library API: they call ib_async (``*Async`` methods and cache
    reads only), convert results into pydantic models from :mod:`ib_gateway_mcp.models`,
    and raise errors from :mod:`ib_gateway_mcp.errors`. Wrap every gateway request in
    :meth:`_call` so it is time-limited and its failures are translated. Every method
    that changes state at IBKR (orders, FA configuration, admin settings) starts with
    ``self.connection.require_trading()``.
    """

    def __init__(self, gateway: Gateway) -> None:
        self._gateway = gateway

    @property
    def gateway(self) -> Gateway:
        """The gateway this service belongs to."""
        return self._gateway

    @property
    def connection(self) -> ConnectionManager:
        """The connection manager (health, trading gate)."""
        return self._gateway.connection

    @property
    def ib(self) -> IB:
        """The connected ``IB`` instance; raises :class:`NotConnectedError` when down."""
        return self._gateway.connection.ib

    def _session(self) -> int:
        """The connection's current session (see ``ConnectionManager.session``)."""
        return self._gateway.connection.session

    @property
    def accounts(self) -> AccountScope:
        """Default account, allowlist and record filtering."""
        return self._gateway.accounts

    @property
    def settings(self) -> Settings:
        """The active settings."""
        return self._gateway.settings

    @property
    def subs(self) -> SubscriptionRegistry:
        """The streaming subscription registry."""
        return self._gateway.subscriptions

    @property
    def safety(self) -> SafetyRails:
        """Preview tokens, order limits, rate limiter, circuit breaker and audit log."""
        return self._gateway.safety

    # --- contracts ---------------------------------------------------------------------

    async def qualify_details(self, spec: ContractSpec) -> ContractDetails:
        """Resolve ``spec`` to exactly one instrument and return its contract details.

        Sends one ``reqContractDetails``. With a ``con_id`` only the id is sent (plus
        ``sec_type`` and ``exchange`` when set explicitly), so the spec's defaults cannot
        contradict it. When ``spec.exchange`` is SMART and the instrument can be
        SMART-routed, the contract's exchange is SMART; ``includeExpired`` follows the spec.

        Raises:
            NotFoundError: IBKR knows no such instrument (error 200 or no rows).
            AmbiguousContractError: More than one contract id matches; up to
                :data:`MAX_LISTED_CANDIDATES` candidates are attached and listed.
            InvalidRequestError: ``spec`` is a combo (BAG), which has no details.
            IbApiError, RequestTimeoutError, NotConnectedError: As for :meth:`_call`.
        """
        _contract, details = await self._lookup(spec)
        return details

    async def qualify(self, spec: ContractSpec) -> Contract:
        """Resolve ``spec`` to IBKR's canonical contract (with its ``conId``).

        Market data, order and PnL requests need a qualified contract. A combo (BAG) is
        returned as built: its legs already name contract ids and are not looked up.

        Raises:
            NotFoundError, AmbiguousContractError: See :meth:`qualify_details`.
        """
        if spec.sec_type == "BAG":
            return contract_from_spec(spec)
        contract, _details = await self._lookup(spec)
        return contract

    async def _lookup(self, spec: ContractSpec) -> tuple[Contract, ContractDetails]:
        if spec.sec_type == "BAG":
            raise InvalidRequestError(
                "Combos (BAG) have no contract details; look up each leg by its con_id."
            )
        request = details_request(spec)
        ib = self.ib
        try:
            rows = await self._call(
                ib.reqContractDetailsAsync(request),
                what=f"contract details for {describe_spec(spec)}",
            )
        except IbApiError as exc:
            if exc.error_code == NO_SECURITY_DEFINITION:
                raise NotFoundError(not_found_message(spec)) from exc
            raise
        return pick_details(spec, request, rows or [])

    async def qualify_many(self, specs: Sequence[ContractSpec]) -> list[Contract]:
        """Qualify several specs concurrently; results are in input order.

        Every lookup runs to completion; if any failed, the first failure in input order
        is raised.
        """
        results = await asyncio.gather(
            *(self.qualify(spec) for spec in specs), return_exceptions=True
        )
        contracts: list[Contract] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            contracts.append(result)
        return contracts

    # --- subscriptions -----------------------------------------------------------------

    async def _subscribe(
        self,
        kind: SubscriptionKind,
        key: str,
        *,
        opener: OpenFn,
        contract: ContractOut | None = None,
        meta: Mapping[str, Any] | None = None,
        slow_open: bool = False,
    ) -> SubscriptionOut:
        """Open (or reuse) a stream through the registry and describe it.

        ``opener`` runs only if ``(kind, key)`` is not open yet; otherwise the existing
        handle comes back with ``deduplicated=True``. Qualify the contract first: for
        ticker streams (quotes, depth, tick-by-tick, news) ``key`` must start with the
        ``conId``. ``contract`` is kept in the entry's ``meta["contract"]``.
        ``slow_open`` runs an opener that waits on IBKR (a backfill) outside the
        registry's open lock (see :meth:`SubscriptionRegistry.add`).

        Raises:
            SubscriptionLimitError: No slot is free; nothing was opened.
        """
        opened = False

        async def open_stream() -> Stream:
            nonlocal opened
            opened = True
            result = opener()
            return await result if inspect.isawaitable(result) else result

        entry_meta = dict(meta or {})
        if contract is not None:
            entry_meta["contract"] = contract.model_dump(mode="json")
        info = await self.subs.add(
            kind, key, opener=open_stream, meta=entry_meta, slow_open=slow_open
        )
        return subscription_out(
            info, idle_ttl_s=self.settings.subscription_idle_ttl, deduplicated=not opened
        )

    def _subscription_data(self, subscription_id: str) -> SubscriptionDataOut:
        """Read a subscription's snapshot (counts as a read for the idle reaper).

        ``last_read_at`` in the result is the previous read, before this one.

        Raises:
            SubscriptionNotFoundError: No such subscription (expired or cancelled).
        """
        info = self.subs.get(subscription_id)
        previous_read = info.last_read_at
        snapshot = self.subs.latest(subscription_id)
        return SubscriptionDataOut(
            subscription_id=info.id,
            kind=info.kind,
            stale=info.stale,
            created_at=info.created_at,
            last_read_at=previous_read,
            data=snapshot.model_dump(mode="json"),
        )

    # --- requests ----------------------------------------------------------------------

    async def _call[T](
        self,
        request: Request[T],
        *,
        what: str,
        timeout: float | None = None,
        exclusive: str | None = None,
        req_id: int | None = None,
    ) -> T:
        """Await one gateway request with a timeout, translating its failures.

        Args:
            request: The awaitable returned by an ib_async ``*Async`` method, or a
                zero-argument callable that returns it (required with ``exclusive``,
                so the request is only sent once the lock is held).
            what: Short description for error messages, e.g. ``"contract details for AAPL"``.
            timeout: Seconds to wait, including any wait for ``exclusive``; defaults to
                ``IB_REQUEST_TIMEOUT``.
            exclusive: The fixed ib_async key of a string-keyed request (see
                :data:`SHARED_REQUEST_KEYS`); concurrent calls with the same key run
                one at a time.
            req_id: The TWS request id (for orders and what-ifs: the order id), when the
                caller knows it. A :data:`REQUEST_ENDING_WARNINGS` error for that id then
                fails the call at once instead of letting it time out.

        Raises:
            RequestTimeoutError: No answer in time.
            IbApiError: The gateway rejected the request (TWS API error code attached).
            NotConnectedError: The connection went away mid-request.
        """
        if exclusive is not None and not callable(request):
            raise TypeError("_call(exclusive=...) needs a callable that creates the request")
        seconds = timeout if timeout is not None else self.settings.request_timeout
        try:
            async with asyncio.timeout(seconds):
                if exclusive is None:
                    return await self._send(request, req_id)
                async with self.connection.request_lock(exclusive):
                    return await self._send(request, req_id)
        except TimeoutError:
            raise RequestTimeoutError(self._timeout_message(what, seconds)) from None
        except RequestError as exc:
            raise IbApiError(exc.code, exc.message, exc.reqId) from exc
        except ConnectionError as exc:
            raise NotConnectedError(
                f"Lost the gateway connection while requesting {what}: {exc}. Check get_health."
            ) from exc

    async def _send[T](self, request: Request[T], req_id: int | None) -> T:
        if req_id is None:
            return await (request() if callable(request) else request)

        def ended(
            error_req_id: int, code: int, message: str, _contract: object
        ) -> IbApiError | None:
            if error_req_id == req_id and code in REQUEST_ENDING_WARNINGS:
                return IbApiError(code, message, req_id)
            return None

        return await self._await_or_reject(request, ended)

    async def _await_or_reject[R, E: BaseException](
        self,
        request: Request[R],
        reject: Callable[[int, int, str, object], E | None],
        *,
        on_reject: Callable[[E], None] | None = None,
    ) -> R:
        """Await ``request``, or fail at once when a gateway error rejects it.

        ib_async treats some rejections as warnings (e.g. 321) and never ends the
        request, so it would only time out. ``reject(req_id, code, message, contract)``
        sees every ``errorEvent`` while the request is open and returns the exception to
        raise for one that rejects it, or None. ``on_reject`` runs with that exception
        before it is raised (e.g. to drop ib_async's bookkeeping of the request).
        Pass ``request`` as a zero-argument callable to send it only once the error
        listener is in place.
        """
        ib = self.ib
        rejected: asyncio.Future[E] = asyncio.get_running_loop().create_future()

        def on_error(req_id: int, code: int, message: str, contract: object) -> None:
            if rejected.done():
                return
            error = reject(req_id, code, message, contract)
            if error is not None:
                rejected.set_result(error)

        answer: asyncio.Future[R] | None = None
        ib.errorEvent += on_error
        try:
            answer = asyncio.ensure_future(request() if callable(request) else request)
            waiters: set[asyncio.Future[Any]] = {answer, rejected}
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if answer.done():
                return answer.result()
            error = rejected.result()
        finally:
            ib.errorEvent -= on_error
            rejected.cancel()
            if answer is not None:
                answer.cancel()  # no-op when it finished
        if on_reject is not None:
            on_reject(error)
        raise error

    async def _hooked_request[R](
        self,
        key: int | str,
        *,
        callback: str,
        on_callback: Callable[..., R | None],
        send: Callable[[], object],
        what: str,
    ) -> R:
        """Send a request ib_async has no ``*Async`` method for, and await its answer.

        ``key`` is the request id (or the fixed key of a request IBKR answers without
        one). ``callback`` names the ib_async wrapper callback that carries the answer;
        it is hooked for the request's duration (:func:`~ib_gateway_mcp.services._hooks.hooked`,
        not re-entrant: hold the callback's request lock around this call).
        ``on_callback`` receives its arguments and returns the result, or None for a
        callback about another request. ``send()`` sends the request. Time limit and
        error translation are those of :meth:`_call`; with a request id, a
        request-ending warning for it fails the request at once.
        """
        wrapper = self.ib.wrapper
        future = wrapper.startReq(key)

        def handler(*args: Any) -> None:
            result = on_callback(*args)
            if result is not None:
                end_request(wrapper, key, result)

        async def request() -> R:
            send()
            answer: R = await future
            return answer

        with hooked(wrapper, callback, handler):
            return await self._call(
                request, what=what, req_id=key if isinstance(key, int) else None
            )

    async def _one_off_request(
        self,
        ib: IB,
        req_id: int,
        *,
        what: str,
        send: Callable[[], None],
        cancel: Callable[[], None],
    ) -> None:
        """Send a request ib_async has no ``*Async`` wrapper for, wait for its end, cancel it.

        The wait uses ib_async's per-request future (``wrapper.startReq``), so request
        errors and a dropped connection fail it like any ``*Async`` call.
        """
        future = ib.wrapper.startReq(req_id)

        async def request() -> object:
            send()
            return await future

        try:
            await self._call(request, what=what, req_id=req_id)
        finally:
            best_effort(cancel, f"cancel {what}")

    def _require_human(self, *, paper: bool, human_confirmed: bool, refusal: str) -> None:
        """Refuse a live-account action a human did not confirm while ``live_confirm`` is on.

        The last gate behind the MCP layer's confirmation (:mod:`ib_gateway_mcp.mcp.confirm`),
        so a wiring mistake in a tool cannot skip the human.

        Raises:
            ConfirmationUnavailableError: With ``refusal`` as the message.
        """
        if not paper and self.settings.live_confirm and not human_confirmed:
            raise ConfirmationUnavailableError(refusal)

    def _new_req_id(self, ib: IB, what: str) -> int:
        """A fresh TWS request id, for requests sent through ``ib.client`` directly.

        Raises:
            NotConnectedError: The gateway is not connected.
        """
        try:
            return ib.client.getReqId()
        except ConnectionError as exc:
            raise NotConnectedError(
                f"Not connected to the gateway, so {what} cannot be requested. Check get_health."
            ) from exc

    def _timeout_message(self, what: str, seconds: float) -> str:
        message = f"Timed out after {seconds:g}s waiting for {what}."
        health = self.connection.health()
        if health.hint:
            message += f" Gateway state: {health.state.value}. {health.hint}"
        if health.api_read_only:
            message += (
                " The gateway's API is in read-only mode (error 321), which rejects orders "
                "and what-if checks; see get_health."
            )
        return message
