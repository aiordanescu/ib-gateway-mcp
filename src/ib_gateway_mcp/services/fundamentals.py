"""Fundamental reports and Wall Street Horizon (WSH) metadata and events.

ib_async 2.1.0 details handled here:

* ``reqFundamentalDataAsync`` has no timeout of its own and never cancels the request
  at IBKR. On a timeout the service sends ``cancelFundamentalData`` for the request id,
  which it finds by the returned future in ``ib.wrapper._futures``.
* ``getWshMetaDataAsync`` and ``getWshEventDataAsync`` allow one active request each:
  a second call cancels the first one's request, so each kind is serialized with a
  per-key lock. ``getWshEventDataAsync`` cancels its request only after an answer, so
  on a timeout the service cancels it.
* IBKR serves WSH events only on a session that requested the metadata first, so the
  metadata is fetched once per session (and cached for ``get_wsh_metadata``).

IBKR removed ``reqFundamentalData`` from TWS API 10.50; gateways on the stable 10.45
line still answer it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, cast, get_args

from ib_async import IB, Contract, WshEventData

from ib_gateway_mcp._ib_compat import pending_request_id
from ib_gateway_mcp._util import best_effort, clamp_limit, contract_to_out, truncate, utc_now
from ib_gateway_mcp.errors import (
    AccountNotAllowedError,
    IbApiError,
    IbGatewayMcpError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RequestTimeoutError,
)
from ib_gateway_mcp.models.common import ContractSpec
from ib_gateway_mcp.models.fundamentals import (
    FundamentalReport,
    FundamentalReportType,
    WshEventList,
    WshEventQuery,
    WshMetadata,
)
from ib_gateway_mcp.services.base import BaseService, describe_spec

if TYPE_CHECKING:
    from ib_gateway_mcp.gateway import Gateway

__all__ = [
    "DEFAULT_REPORT_CHARS",
    "DEFAULT_WSH_EVENTS",
    "DEFAULT_WSH_METADATA_CHARS",
    "FUNDAMENTAL_REPORT_TYPES",
    "MAX_REPORT_CHARS",
    "MAX_WSH_EVENTS",
    "MAX_WSH_METADATA_CHARS",
    "FundamentalsService",
]

logger = logging.getLogger(__name__)

FUNDAMENTAL_REPORT_TYPES: tuple[str, ...] = get_args(FundamentalReportType)
"""Report types :meth:`FundamentalsService.fundamental_data` accepts."""

DEFAULT_REPORT_CHARS = 50_000
MAX_REPORT_CHARS = 200_000
DEFAULT_WSH_METADATA_CHARS = 30_000
MAX_WSH_METADATA_CHARS = 200_000
DEFAULT_WSH_EVENTS = 50
MAX_WSH_EVENTS = 100
"""Upper bound for the events asked of IBKR in one request (``WshEventData.totalLimit``).

A result that reaches the number asked for counts as truncated: at this cap the service
cannot ask for one extra event to find out whether more exist.
"""

_NO_FUNDAMENTALS = 430
"""IB error 430: no fundamentals data for the security."""
_FUNDAMENTALS_NOT_ALLOWED = 10358
"""IB error 10358: the login has no fundamentals data subscription."""
_WSH_DUPLICATE_REQUEST = frozenset({10278, 10281})
"""IB errors for a second WSH metadata or event request while one is active."""
_WSH_METADATA_NOT_REQUESTED = 10282
"""IB error for an event request on a session that has not requested the metadata."""

_WSH_METADATA_LOCK = "wshMetaData"
_WSH_EVENTS_LOCK = "wshEventData"

_BETWEEN_TAGS = re.compile(r">\s+<")
_WSH_EVENT_TAG = re.compile(r'"(wshe_[a-z0-9_]+)"')
_EVENT_LIST_KEYS = ("events", "data", "results", "items")
_MAX_RAW_EVENT_CHARS = 20_000
_LISTED_EVENT_TYPES = 60

_DEPRECATION_NOTE = (
    "IBKR deprecated fundamental reports (reqFundamentalData was removed in TWS API 10.50): "
    "gateways on the stable 10.45 line still serve them, newer ones may not."
)
_WSH_HINT = (
    "WSH requests need a Wall Street Horizon corporate event data subscription on this "
    "IBKR login; without one IBKR rejects them. Otherwise check the arguments against "
    "get_wsh_metadata."
)
_FILTER_EXAMPLE = '{"watchlist": ["8314"], "wshe_ed": "true"}'


class FundamentalsService(BaseService):
    """Fundamental (Refinitiv) reports and Wall Street Horizon corporate events."""

    def __init__(self, gateway: Gateway) -> None:
        super().__init__(gateway)
        self._wsh_metadata: str | None = None
        self._wsh_metadata_session: datetime | None = None
        self._wsh_metadata_fetched_at: datetime | None = None

    # --- fundamental reports -------------------------------------------------------------

    async def fundamental_data(
        self, contract: ContractSpec, report_type: str, *, max_chars: int | None = None
    ) -> FundamentalReport:
        """Fetch one fundamental (Refinitiv) report for a stock, as XML.

        Deprecated by IBKR: ``reqFundamentalData`` was removed in TWS API 10.50 and only
        gateways on the stable 10.45 line still answer it.

        Args:
            contract: The stock (sec_type STK).
            report_type: One of :data:`FUNDAMENTAL_REPORT_TYPES`.
            max_chars: Cut the XML after this many characters (default
                :data:`DEFAULT_REPORT_CHARS`, capped at :data:`MAX_REPORT_CHARS`).

        Raises:
            InvalidRequestError: Unknown report type, or the contract is not a stock.
            NotFoundError: Unknown contract, or IBKR has no such report for it (error 430).
            AmbiguousContractError: The spec matches several stocks.
            IbApiError: IBKR refused, e.g. no fundamentals subscription (10358).
        """
        if report_type not in FUNDAMENTAL_REPORT_TYPES:
            raise InvalidRequestError(
                f"Unknown report_type {report_type!r}; use one of "
                f"{', '.join(FUNDAMENTAL_REPORT_TYPES)}."
            )
        if contract.sec_type != "STK":
            raise InvalidRequestError(
                "Fundamental reports exist for stocks only (sec_type STK), not "
                f"{contract.sec_type}."
            )
        chars = clamp_limit(max_chars, default=DEFAULT_REPORT_CHARS, maximum=MAX_REPORT_CHARS)
        qualified = await self.qualify(contract)
        if qualified.secType != "STK":
            raise InvalidRequestError(
                f"{describe_spec(contract)} is a {qualified.secType} contract; fundamental "
                "reports exist for stocks only."
            )
        ib = self.ib
        try:
            result = await self._call(
                lambda: _fundamental_request(ib, qualified, report_type),
                what=f"the {report_type} report for {qualified.symbol}",
            )
        except IbApiError as exc:
            raise _fundamentals_error(exc, report_type, qualified.symbol) from exc
        except RequestTimeoutError as exc:
            # A gateway past API 10.50 may not answer the removed request at all.
            raise RequestTimeoutError(f"{exc} {_DEPRECATION_NOTE}") from exc
        xml = result.strip() if isinstance(result, str) else ""
        if not xml:
            raise NotFoundError(
                f"IBKR returned no {report_type} report for {qualified.symbol}. Not every "
                "stock has every report type; ReportSnapshot and ReportsFinSummary are the "
                "most widely available."
            )
        xml = _BETWEEN_TAGS.sub("><", xml)
        return FundamentalReport(
            contract=contract_to_out(qualified),
            report_type=cast(FundamentalReportType, report_type),  # checked above
            xml=xml[:chars],
            total_chars=len(xml),
            truncated=len(xml) > chars,
        )

    # --- Wall Street Horizon -------------------------------------------------------------

    async def wsh_metadata(
        self, *, query: str | None = None, max_chars: int | None = None
    ) -> WshMetadata:
        """Return the Wall Street Horizon metadata: event types and filter fields.

        The metadata is fetched once and then served from memory for the life of the
        process.

        Args:
            query: Keep only the parts mentioning this text (case-insensitive): whole
                list entries, and the object keys leading to them.
            max_chars: Cut the JSON after this many characters (default
                :data:`DEFAULT_WSH_METADATA_CHARS`, capped at :data:`MAX_WSH_METADATA_CHARS`).

        Raises:
            NotFoundError: IBKR sent no metadata, or nothing mentions ``query``.
            IbApiError: IBKR refused, typically for lack of a WSH subscription.
        """
        chars = clamp_limit(
            max_chars, default=DEFAULT_WSH_METADATA_CHARS, maximum=MAX_WSH_METADATA_CHARS
        )
        needle = (query or "").strip() or None
        text, cached = await self._metadata_text(this_session=False)
        body = _filter_metadata(text, needle)
        return WshMetadata(
            query=needle,
            event_types=_event_type_tags(body),
            metadata_json=body[:chars],
            total_chars=len(body),
            truncated=len(body) > chars,
            cached=cached,
            fetched_at=self._wsh_metadata_fetched_at or utc_now(),
        )

    async def wsh_events(
        self,
        *,
        contract: ContractSpec | None = None,
        filter_json: str | Mapping[str, Any] | None = None,
        event_types: Sequence[str] | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        fill_watchlist: bool = False,
        fill_portfolio: bool = False,
        fill_competitors: bool = False,
        limit: int | None = None,
    ) -> WshEventList:
        """Fetch corporate events (earnings, dividends, splits, meetings...) from WSH.

        Without ``filter_json``, a contract alone asks for all its event types; with
        ``event_types`` the service builds the filter
        ``{"watchlist": ["<conId>"], "<type>": "true", ...}``. ``filter_json`` (text sent
        as is, a mapping serialized) overrides ``contract`` and ``event_types``, and a
        note says so. The metadata is requested first on each session, as IBKR requires.

        Args:
            contract: The instrument; qualified to its conId.
            filter_json: A raw WSH filter: a JSON object, or its text.
            event_types: WSH event type tags such as ``wshe_ed`` (earnings date).
            start_date: First day to include.
            end_date: Last day to include.
            fill_watchlist: Add the login's watchlist instruments to the filter.
            fill_portfolio: Add the portfolio's instruments.
            fill_competitors: Add the instruments' competitors.
            limit: Events to return (default :data:`DEFAULT_WSH_EVENTS`, capped at
                :data:`MAX_WSH_EVENTS`).

        Raises:
            InvalidRequestError: Nothing to ask for, a bad filter, dates in the wrong
                order, or event types the metadata does not list.
            AccountNotAllowedError: ``fill_portfolio`` on a login that manages accounts
                outside the allowlist (their holdings would leak into the result).
            NotFoundError: Unknown contract, or WSH has no matching events.
            IbApiError: IBKR refused, typically for lack of a WSH subscription.
        """
        count = clamp_limit(limit, default=DEFAULT_WSH_EVENTS, maximum=MAX_WSH_EVENTS)
        if start_date and end_date and start_date > end_date:
            raise InvalidRequestError(
                f"start_date {start_date.isoformat()} is after end_date {end_date.isoformat()}."
            )
        types = _clean_event_types(event_types)
        raw_filter = _filter_text(filter_json)
        notes: list[str] = []
        if raw_filter is not None:
            ignored = [
                name
                for name, given in (
                    ("contract", contract is not None),
                    ("event_types", bool(types)),
                )
                if given
            ]
            if ignored:
                notes.append(
                    f"filter_json was given, so {' and '.join(ignored)} "
                    f"{'was' if len(ignored) == 1 else 'were'} ignored: the filter alone "
                    "selects the events."
                )
            contract, types = None, []
        elif contract is None and not types and not (fill_watchlist or fill_portfolio):
            raise InvalidRequestError(
                "Say which events to fetch: give a contract, event_types or filter_json, or "
                "set fill_portfolio or fill_watchlist (fill_competitors only adds the "
                "competitors of those instruments)."
            )
        if fill_portfolio:
            self._check_portfolio_scope()
        qualified = await self.qualify(contract) if contract is not None else None
        metadata, _cached = await self._metadata_text(this_session=True)
        _check_event_types(types, metadata)

        con_id, filter_text = _event_target(qualified, types, raw_filter)
        query = WshEventQuery(
            con_id=con_id,
            filter=filter_text or None,
            start_date=start_date,
            end_date=end_date,
            fill_watchlist=fill_watchlist,
            fill_portfolio=fill_portfolio,
            fill_competitors=fill_competitors,
            total_limit=min(count + 1, MAX_WSH_EVENTS),
        )
        events = _parse_events(await self._events_request(_event_data(query)))
        if not events:
            subject = qualified.symbol if qualified is not None else "this filter"
            raise NotFoundError(
                f"Wall Street Horizon returned no events for {subject}. Widen start_date/"
                "end_date, check event_types against get_wsh_metadata, or relax the filter."
            )
        kept, truncated = truncate(events, count)
        return WshEventList(
            contract=contract_to_out(qualified) if qualified is not None else None,
            events=kept,
            total=len(events),
            # IBKR stops at total_limit: reaching it means more events may exist.
            truncated=truncated or len(events) >= query.total_limit,
            request=query,
            notes=notes,
        )

    # --- internals -----------------------------------------------------------------------

    def _check_portfolio_scope(self) -> None:
        """Refuse ``fillPortfolio`` unless every account on the login is allowed.

        IBKR fills in the instruments held across the whole login, so the events would
        reveal positions of accounts this server may not read.
        """
        scope = self.accounts
        if not scope.ready:
            raise NotConnectedError(
                "Accounts are unknown until the gateway connection is up; check get_health."
            )
        hidden = set(scope.managed) - scope.allowed
        if hidden:
            raise AccountNotAllowedError(
                "fill_portfolio adds the instruments held in every account of this IBKR "
                f"login, and {len(hidden)} of them are outside this server's allowlist, so "
                "it is refused. Ask by contract, event_types or filter_json instead; the "
                "operator can allow every managed account in IBKR_MCP_ACCOUNTS."
            )

    def _usable_metadata(self, *, this_session: bool) -> str | None:
        """The cached metadata, if any; with ``this_session`` only if requested this session."""
        if self._wsh_metadata is None:
            return None
        if this_session:
            session = self.connection.connected_since
            if session is None or session != self._wsh_metadata_session:
                return None
        return self._wsh_metadata

    async def _metadata_text(self, *, this_session: bool) -> tuple[str, bool]:
        """Return the metadata JSON and whether it came from the cache."""
        cached = self._usable_metadata(this_session=this_session)
        if cached is not None:
            return cached, True
        ib = self.ib
        session = self.connection.connected_since

        async def request() -> tuple[str, bool]:
            # A concurrent caller may have fetched it while this one waited for the lock.
            fetched = self._usable_metadata(this_session=this_session)
            if fetched is not None:
                return fetched, True
            try:
                return await ib.getWshMetaDataAsync(), False
            except asyncio.CancelledError:
                best_effort(ib.cancelWshMetaData, "cancel the WSH metadata request")
                raise

        try:
            text, cached_now = await self._call(
                request, what="Wall Street Horizon metadata", exclusive=_WSH_METADATA_LOCK
            )
        except IbApiError as exc:
            raise _wsh_error(exc) from exc
        if cached_now:
            return text, True
        if not text.strip():
            raise NotFoundError(f"IBKR returned no Wall Street Horizon metadata. {_WSH_HINT}")
        self._wsh_metadata = text
        self._wsh_metadata_session = session
        self._wsh_metadata_fetched_at = utc_now()
        return text, False

    async def _events_request(self, data: WshEventData) -> str:
        """Send one event request; re-request the metadata once if IBKR says it is missing."""
        try:
            return await self._send_events(data)
        except IbApiError as exc:
            if not _metadata_not_requested(exc):
                raise _wsh_error(exc) from exc
        self._wsh_metadata_session = None
        await self._metadata_text(this_session=True)
        try:
            return await self._send_events(data)
        except IbApiError as exc:
            raise _wsh_error(exc) from exc

    async def _send_events(self, data: WshEventData) -> str:
        ib = self.ib

        async def request() -> str:
            try:
                return await ib.getWshEventDataAsync(data)
            except asyncio.CancelledError:
                # ib_async cancels only after an answer; leave nothing open at IBKR.
                best_effort(ib.cancelWshEventData, "cancel the WSH event request")
                raise

        return await self._call(
            request, what="Wall Street Horizon events", exclusive=_WSH_EVENTS_LOCK
        )


# --- fundamental report helpers ------------------------------------------------------------


async def _fundamental_request(ib: IB, contract: Contract, report_type: str) -> object:
    """Await ``reqFundamentalDataAsync``, cancelling it at IBKR if the wait is cancelled."""
    future = ib.reqFundamentalDataAsync(contract, report_type)
    req_id = _pending_request_id(ib, future)
    try:
        return await future
    except asyncio.CancelledError:
        if req_id is not None:
            best_effort(
                lambda: ib.client.cancelFundamentalData(req_id), "cancel the fundamentals request"
            )
        raise


def _pending_request_id(ib: IB, future: object) -> int | None:
    """The request id ib_async registered ``future`` under, if found."""
    return pending_request_id(ib.wrapper, future)


def _fundamentals_error(exc: IbApiError, report_type: str, symbol: str) -> IbGatewayMcpError:
    message = exc.error_message.rstrip(". ")
    if exc.error_code == _NO_FUNDAMENTALS:
        return NotFoundError(
            f"IBKR has no {report_type} report for {symbol} (IB error 430: {message}). Not "
            "every stock has every report type; ReportSnapshot and ReportsFinSummary are "
            "the most widely available."
        )
    if exc.error_code == _FUNDAMENTALS_NOT_ALLOWED:
        hint = "Fundamental reports need a Refinitiv (Reuters) fundamentals subscription."
    else:
        hint = _DEPRECATION_NOTE
    return exc.with_hint(hint)


# --- WSH helpers ---------------------------------------------------------------------------


def _wsh_error(exc: IbApiError) -> IbApiError:
    if exc.error_code in _WSH_DUPLICATE_REQUEST:
        hint = "Another WSH request is still active on this connection; retry in a few seconds."
    else:
        hint = _WSH_HINT
    return exc.with_hint(hint)


def _metadata_not_requested(exc: IbApiError) -> bool:
    text = exc.error_message.lower()
    return exc.error_code == _WSH_METADATA_NOT_REQUESTED or (
        "meta" in text and "not requested" in text
    )


def _loads(text: str) -> Any:
    """Parse JSON, turning NaN and infinities into None."""
    return json.loads(text, parse_constant=lambda _constant: None)


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _event_type_tags(text: str) -> list[str]:
    """Every WSH event type tag (``wshe_...``) quoted in ``text``, sorted."""
    return sorted(set(_WSH_EVENT_TAG.findall(text)))


def _mentions(node: Any, needle: str) -> bool:
    if isinstance(node, dict):
        return any(needle in str(key).lower() or _mentions(v, needle) for key, v in node.items())
    if isinstance(node, list):
        return any(_mentions(item, needle) for item in node)
    return node is not None and needle in str(node).lower()


def _prune(node: Any, needle: str) -> Any:
    """Keep what mentions ``needle``: whole list entries, and the keys leading to them."""
    if isinstance(node, dict):
        return {
            key: value if needle in str(key).lower() else _prune(value, needle)
            for key, value in node.items()
            if needle in str(key).lower() or _mentions(value, needle)
        }
    if isinstance(node, list):
        return [item for item in node if _mentions(item, needle)]
    return node


def _filter_metadata(text: str, needle: str | None) -> str:
    """The metadata as compact JSON, cut down to what mentions ``needle``."""
    try:
        parsed = _loads(text)
    except ValueError:
        body = text.strip()  # not JSON: pass it through
        if needle is None or needle.lower() in body.lower():
            return body
        raise _no_metadata_match(needle, text) from None
    if needle is None:
        return _compact(parsed)
    lowered = needle.lower()
    if not _mentions(parsed, lowered):
        raise _no_metadata_match(needle, text)
    return _compact(_prune(parsed, lowered))


def _no_metadata_match(needle: str, text: str) -> NotFoundError:
    tags = _event_type_tags(text)
    listed = f" Event types: {', '.join(tags[:_LISTED_EVENT_TYPES])}." if tags else ""
    return NotFoundError(
        f"Nothing in the Wall Street Horizon metadata mentions {needle!r}.{listed} Call "
        "get_wsh_metadata without query to see all of it."
    )


def _clean_event_types(event_types: Sequence[str] | None) -> list[str]:
    cleaned = (value.strip().lower() for value in event_types or ())
    return list(dict.fromkeys(value for value in cleaned if value))


def _check_event_types(types: Sequence[str], metadata: str) -> None:
    known = set(_event_type_tags(metadata))
    unknown = [value for value in types if value not in known]
    if known and unknown:
        raise InvalidRequestError(
            f"Unknown WSH event type(s): {', '.join(unknown)}. Known types: "
            f"{', '.join(sorted(known)[:_LISTED_EVENT_TYPES])}; get_wsh_metadata describes them."
        )


def _filter_text(value: str | Mapping[str, Any] | None) -> str | None:
    """The raw filter to send (a JSON object as text), or None when there is none."""
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            return _compact(dict(value)) if value else None
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError(f"filter_json cannot be sent as JSON: {exc}") from None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise InvalidRequestError(
            f"filter_json is not valid JSON ({exc}). Example: {_FILTER_EXAMPLE}"
        ) from None
    if not isinstance(parsed, dict):
        raise InvalidRequestError(f"filter_json must be a JSON object, e.g. {_FILTER_EXAMPLE}")
    return text


def _event_target(
    contract: Contract | None, types: Sequence[str], raw_filter: str | None
) -> tuple[int | None, str]:
    """The conId and filter to send: a raw filter, a built filter, or just the conId."""
    if raw_filter is not None:
        return None, raw_filter
    if contract is not None and not types:
        return contract.conId, ""
    conditions: dict[str, Any] = {}
    if contract is not None:
        conditions["watchlist"] = [str(contract.conId)]
    conditions.update(dict.fromkeys(types, "true"))
    return None, _compact(conditions) if conditions else ""


def _event_data(query: WshEventQuery) -> WshEventData:
    data = WshEventData(
        filter=query.filter or "",
        fillWatchlist=query.fill_watchlist,
        fillPortfolio=query.fill_portfolio,
        fillCompetitors=query.fill_competitors,
        startDate=query.start_date.strftime("%Y%m%d") if query.start_date else "",
        endDate=query.end_date.strftime("%Y%m%d") if query.end_date else "",
        totalLimit=query.total_limit,
    )
    if query.con_id is not None:
        data.conId = query.con_id
    return data


def _json_values(text: str) -> list[Any]:
    """Parse one JSON document, or several in a row (one per line or comma-separated)."""
    try:
        return [_loads(text)]
    except ValueError:
        pass
    decoder = json.JSONDecoder(parse_constant=lambda _constant: None)
    values: list[Any] = []
    position = 0
    while position < len(text):
        if text[position] in " \t\r\n,":
            position += 1
            continue
        try:
            value, position = decoder.raw_decode(text, position)
        except ValueError:
            values.append({"raw": text[position:][:_MAX_RAW_EVENT_CHARS]})
            break
        values.append(value)
    return values


def _parse_events(raw: str) -> list[dict[str, Any]]:
    """Turn IBKR's event JSON into a list of event objects.

    Accepts an array of events, an object holding one under ``events``/``data``/
    ``results``/``items``, a single event object, or several JSON documents in a row.
    Text that is not JSON comes back as one ``{"raw": ...}`` entry.
    """
    text = raw.strip()
    if not text:
        return []
    items: list[Any] = []
    for value in _json_values(text):
        if isinstance(value, dict):
            inner = next(
                (value[key] for key in _EVENT_LIST_KEYS if isinstance(value.get(key), list)),
                None,
            )
            items.extend(inner if inner is not None else [value])
        elif isinstance(value, list):
            items.extend(value)
        elif value is not None:
            items.append(value)
    return [item if isinstance(item, dict) else {"value": item} for item in items]
