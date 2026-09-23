"""Display groups, the gateway's server log level and the order circuit breaker.

ib_async 2.1.0 behaviours this service works around:

* **Display-group callbacks have no handler**: ``displayGroupList`` and
  ``displayGroupUpdated`` exist only in the decoder, which looks them up on the wrapper
  at dispatch time. :meth:`AdminService.list_display_groups` hooks ``displayGroupList``
  for one request; display-group subscriptions share one ``displayGroupUpdated``
  dispatcher on the wrapper that routes by request id.
* ``queryDisplayGroups``, ``subscribeToGroupEvents``, ``updateDisplayGroup``,
  ``unsubscribeFromGroupEvents`` and ``setServerLogLevel`` exist only on ``ib.client``.

Display groups are a TWS window feature (colour-linked windows). IB Gateway has no
windows, so it may report no groups, or not answer at all.

The circuit breaker lives in this service's toolset, not in ``orders``, so the model
whose orders tripped it cannot clear it: the MCP tool asks a human first.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from ib_async import IB

from ib_gateway_mcp._util import contract_to_out, is_informational, utc_now
from ib_gateway_mcp.errors import (
    ConfirmationUnavailableError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    SubscriptionNotFoundError,
)
from ib_gateway_mcp.models.admin import (
    SERVER_LOG_LEVELS,
    CircuitBreakerReset,
    CircuitBreakerStatus,
    DisplayGroupList,
    DisplayGroupSnapshot,
    DisplayGroupUpdate,
    DisplayGroupUpdated,
    ServerLogLevel,
    ServerLogLevelOut,
)
from ib_gateway_mcp.models.common import ContractSpec, SubscriptionOut
from ib_gateway_mcp.safety import AuditEvent
from ib_gateway_mcp.services.base import BaseService
from ib_gateway_mcp.subscriptions import Stream

if TYPE_CHECKING:
    from ib_gateway_mcp.gateway import Gateway

__all__ = ["GROUP_UPDATES_MAX", "AdminService"]

logger = logging.getLogger(__name__)

GROUP_UPDATES_MAX: Final = 50
"""Display-group selections kept per subscription."""

_DISPLAY_GROUP_KIND: Final = "display_group"
_GROUP_LIST_KEY: Final = "displayGroupList"

# ib_async wrapper callbacks hooked here (names, so mypy does not see unknown attributes).
_DISPLAY_GROUP_LIST: Final = "displayGroupList"
_DISPLAY_GROUP_UPDATED: Final = "displayGroupUpdated"

_GROUPS_WHAT: Final = "the display group list (a TWS window feature; IB Gateway may not answer)"

_AUDIT_LOG_LEVEL: Final = "server_log_level"
_AUDIT_DISPLAY_GROUP: Final = "display_group_update"


# --- helpers ------------------------------------------------------------------------------


def _send(what: str, send: Callable[[], None]) -> None:
    """Run a fire-and-forget ``ib.client`` call; a closed socket becomes NotConnectedError.

    ``Client.send`` raises a bare ``ConnectionError`` when the socket is down; outside
    :meth:`BaseService._call` nothing else would translate it.
    """
    try:
        send()
    except ConnectionError as exc:
        raise NotConnectedError(
            f"Lost the gateway connection while sending {what}: {exc}. Check get_health."
        ) from exc


def _group_ids(raw: object) -> list[int]:
    """Parse ``displayGroupList``'s ``"1|2|3"`` into ids."""
    if not isinstance(raw, str):
        return []
    return [int(part) for part in raw.split("|") if part.strip().isdigit()]


def _group_update(contract_info: str) -> DisplayGroupUpdate:
    """Decode ``displayGroupUpdated``'s ``conId@exchange``, ``none`` or ``combo``."""
    text = (contract_info or "").strip()
    lowered = text.lower()
    now = utc_now()
    if lowered in {"", "none"}:
        return DisplayGroupUpdate(time=now, contract_info=text or "none", selection="none")
    if lowered == "combo":
        return DisplayGroupUpdate(time=now, contract_info=text, selection="combo")
    con_id, _, exchange = text.partition("@")
    return DisplayGroupUpdate(
        time=now,
        contract_info=text,
        selection="contract",
        con_id=int(con_id) if con_id.strip().isdigit() else None,
        exchange=exchange.strip() or None,
    )


@dataclass(eq=False)
class _GroupStream:
    """One display-group subscription: its current request id and what it received."""

    ib: IB
    group_id: int
    req_id: int
    updates: deque[DisplayGroupUpdate] = field(
        default_factory=lambda: deque(maxlen=GROUP_UPDATES_MAX)
    )
    error: str | None = None

    def on_update(self, contract_info: str) -> None:
        self.updates.append(_group_update(contract_info))
        self.error = None

    def on_error(self, req_id: int, code: int, message: str, _contract: object) -> None:
        if req_id == self.req_id and not is_informational(code):
            self.error = f"IB error {code}: {message}"

    def snapshot(self) -> DisplayGroupSnapshot:
        updates = list(self.updates)
        return DisplayGroupSnapshot(
            group_id=self.group_id,
            current=updates[-1] if updates else None,
            updates=updates,
            error=self.error,
        )


# --- the service --------------------------------------------------------------------------


class AdminService(BaseService):
    """Display groups, the gateway's server log level and the order circuit breaker."""

    def __init__(self, gateway: Gateway) -> None:
        super().__init__(gateway)
        self._groups_by_req: dict[int, _GroupStream] = {}
        self._groups_by_key: dict[str, _GroupStream] = {}

    # --- circuit breaker ------------------------------------------------------------------

    def circuit_breaker_status(self) -> CircuitBreakerStatus:
        """Return the order circuit breaker's state."""
        breaker = self.safety.breaker
        return CircuitBreakerStatus(
            is_open=breaker.is_open,
            opened_at=breaker.opened_at,
            consecutive_rejections=breaker.consecutive_rejections,
            threshold=breaker.threshold,
            last_reason=breaker.last_reason,
        )

    def reset_circuit_breaker(self, reason: str, *, human_confirmed: bool) -> CircuitBreakerReset:
        """Close the order circuit breaker and clear its rejection count. Audited.

        When the breaker is closed and has counted no rejections, nothing changes and no
        confirmation is needed.

        Args:
            reason: Why trading may resume; recorded in the audit log.
            human_confirmed: A human approved the reset. The MCP tool asks through
                elicitation; library callers pass True for themselves.

        Raises:
            InvalidRequestError: ``reason`` is blank.
            ConfirmationUnavailableError: There is something to reset and no human
                confirmed it.
        """
        text = " ".join(reason.split())
        if not text:
            raise InvalidRequestError(
                "reason is empty; say why order submission may resume (it is audited)."
            )
        before = self.circuit_breaker_status()
        if not before.needs_reset:
            return CircuitBreakerReset(
                reset=False, before=before, reason=text, human_confirmed=human_confirmed
            )
        if not human_confirmed:
            raise ConfirmationUnavailableError(
                "Resetting the order circuit breaker needs a human's confirmation; nothing "
                "was changed. Stop placing orders and ask the user."
            )
        self.safety.breaker.reset()
        self.safety.audit.record(
            AuditEvent.CIRCUIT_RESET, reason=text, human_confirmed=True, before=before
        )
        logger.warning("Order circuit breaker reset by a human: %s", text)
        return CircuitBreakerReset(reset=True, before=before, reason=text, human_confirmed=True)

    # --- server log level -----------------------------------------------------------------

    def set_server_log_level(self, level: ServerLogLevel) -> ServerLogLevelOut:
        """Set the verbosity of the gateway's own API log (``setServerLogLevel``). Audited.

        IBKR sends no acknowledgement.

        Raises:
            InvalidRequestError: Unknown level.
            NotConnectedError, ConfigurationError, LiveTradingDisabledError: Changes are
                not allowed right now (``require_trading``).
        """
        self.connection.require_trading()
        code = SERVER_LOG_LEVELS.get(level)
        if code is None:
            raise InvalidRequestError(
                f"Unknown log level {level!r}; use one of {', '.join(SERVER_LOG_LEVELS)}."
            )
        ib = self.ib
        _send("the server log level", lambda: ib.client.setServerLogLevel(code))
        self.safety.audit.record(_AUDIT_LOG_LEVEL, level=level, code=code)
        return ServerLogLevelOut(level=level, code=code)

    # --- display groups -------------------------------------------------------------------

    async def list_display_groups(self) -> DisplayGroupList:
        """Return the ids of the TWS display groups (``queryDisplayGroups``).

        Raises:
            NotFoundError: The gateway reports none (typical for IB Gateway).
            RequestTimeoutError: No answer (IB Gateway may ignore the request).
            IbApiError, NotConnectedError: As for ``_call``.
        """
        ib = self.ib
        async with self.connection.request_lock(_GROUP_LIST_KEY):
            req_id = self._new_req_id(ib, "display groups")

            def on_list(list_req_id: int, groups: str) -> str | None:
                return groups if list_req_id == req_id else None

            raw = await self._hooked_request(
                req_id,
                callback=_DISPLAY_GROUP_LIST,
                on_callback=on_list,
                send=lambda: ib.client.queryDisplayGroups(req_id),
                what=_GROUPS_WHAT,
            )
        groups = _group_ids(raw)
        if not groups:
            raise NotFoundError(
                "The gateway reports no display groups. They are a TWS window feature; IB "
                "Gateway normally has none."
            )
        return DisplayGroupList(groups=groups)

    async def subscribe_display_group(self, group_id: int) -> SubscriptionOut:
        """Follow the contract a display group shows (``subscribeToGroupEvents``).

        The stream's snapshot is a :class:`DisplayGroupSnapshot`. Subscribing to the same
        group again returns the existing handle.

        Raises:
            InvalidRequestError: ``group_id`` below 1.
            SubscriptionLimitError: No subscription slot is free.
        """
        if group_id < 1:
            raise InvalidRequestError(
                "group_id must be 1 or more; list_display_groups shows the ids."
            )
        key = f"group:{group_id}"

        def open_stream() -> Stream:
            ib = self.ib
            stream = _GroupStream(
                ib=ib, group_id=group_id, req_id=self._new_req_id(ib, "a display group")
            )
            self._install_group_dispatch(ib)
            self._groups_by_req[stream.req_id] = stream
            self._groups_by_key[key] = stream
            ib.errorEvent += stream.on_error
            try:
                _send(
                    "a display group subscription",
                    lambda: ib.client.subscribeToGroupEvents(stream.req_id, group_id),
                )
            except BaseException:
                self._forget_group(stream, key)
                raise
            return Stream(
                cancel=lambda: self._cancel_group(stream, key),
                snapshot=stream.snapshot,
                resubscribe=lambda: self._resubscribe_group(stream),
            )

        return await self._subscribe(
            _DISPLAY_GROUP_KIND, key, opener=open_stream, meta={"group_id": group_id}
        )

    async def update_display_group(
        self, subscription_id: str, contract: ContractSpec
    ) -> DisplayGroupUpdated:
        """Make a subscribed display group show ``contract`` (``updateDisplayGroup``). Audited.

        Raises:
            SubscriptionNotFoundError: No such subscription.
            InvalidRequestError: The subscription is not a display group, or the
                contract is a combo.
            NotFoundError, AmbiguousContractError: The contract cannot be resolved.
            NotConnectedError, ConfigurationError, LiveTradingDisabledError: Changes are
                not allowed right now (``require_trading``).
        """
        self.connection.require_trading()
        info = self.subs.get(subscription_id)
        if info.kind != _DISPLAY_GROUP_KIND:
            raise InvalidRequestError(
                f"Subscription {subscription_id} streams {info.kind}, not a display group; "
                "subscribe_display_group returns the handle to use."
            )
        stream = self._groups_by_key.get(info.key)
        if stream is None:
            raise SubscriptionNotFoundError(
                f"Display group subscription {subscription_id} is no longer open; subscribe again."
            )
        if contract.sec_type == "BAG":
            raise InvalidRequestError("Display groups show single contracts, not combos (BAG).")
        qualified = await self.qualify(contract)
        contract_info = f"{qualified.conId}@{qualified.exchange or 'SMART'}"
        ib = self.ib
        _send(
            "the display group update",
            lambda: ib.client.updateDisplayGroup(stream.req_id, contract_info),
        )
        self.safety.audit.record(
            _AUDIT_DISPLAY_GROUP,
            subscription_id=subscription_id,
            group_id=stream.group_id,
            contract_info=contract_info,
        )
        return DisplayGroupUpdated(
            subscription_id=subscription_id,
            group_id=stream.group_id,
            contract=contract_to_out(qualified),
            contract_info=contract_info,
        )

    # --- internals ------------------------------------------------------------------------

    def _install_group_dispatch(self, ib: IB) -> None:
        """Route ``displayGroupUpdated`` to the subscription owning the request id."""
        wrapper = ib.wrapper
        if vars(wrapper).get(_DISPLAY_GROUP_UPDATED) != self._on_group_updated:
            setattr(wrapper, _DISPLAY_GROUP_UPDATED, self._on_group_updated)

    def _on_group_updated(self, req_id: int, contract_info: str) -> None:
        stream = self._groups_by_req.get(req_id)
        if stream is not None:
            stream.on_update(contract_info)

    def _forget_group(self, stream: _GroupStream, key: str) -> None:
        if self._groups_by_req.get(stream.req_id) is stream:
            del self._groups_by_req[stream.req_id]
        if self._groups_by_key.get(key) is stream:
            del self._groups_by_key[key]
        stream.ib.errorEvent -= stream.on_error

    def _cancel_group(self, stream: _GroupStream, key: str) -> None:
        self._forget_group(stream, key)
        try:
            ib = self.ib
        except NotConnectedError:
            return  # the subscription ended with the session
        with suppress(ConnectionError):  # the subscription ends with the session anyway
            ib.client.unsubscribeFromGroupEvents(stream.req_id)

    def _resubscribe_group(self, stream: _GroupStream) -> None:
        """Re-request a group subscription after IBKR lost it or after a reconnect."""
        ib = self.ib
        if self._groups_by_req.get(stream.req_id) is stream:
            del self._groups_by_req[stream.req_id]
        stream.req_id = self._new_req_id(ib, "a display group")
        stream.error = None
        self._groups_by_req[stream.req_id] = stream
        self._install_group_dispatch(ib)
        _send(
            "a display group subscription",
            lambda: ib.client.subscribeToGroupEvents(stream.req_id, stream.group_id),
        )
