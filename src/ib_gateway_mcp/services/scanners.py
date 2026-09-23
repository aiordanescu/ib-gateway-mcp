"""Market scanner parameters, one-off scans and scanner subscriptions.

IBKR describes its scanners in one large XML document (1-2 MB): the scan codes, the
instrument types, the locations (markets and exchanges) and the filter tags. It is
fetched once per gateway session, parsed, and served in slices.

Scans go through ``IB.reqScannerSubscription`` even when only one answer is wanted:
``reqScannerDataAsync`` leaves the subscription open at IBKR when its wait is cancelled
(IBKR allows 10 at a time), so :meth:`ScannersService.run_scanner` waits for the first
result set itself and always cancels. ib_async does not fail scanner requests on
errors (they are subscriptions, not requests), so errors for the scan's request id
are read from ``errorEvent`` here.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ib_async import IB, Contract, ScanData, ScanDataList, ScannerSubscription, TagValue

from ib_gateway_mcp._util import (
    clamp_limit,
    clean_str,
    contract_to_out,
    is_informational,
    truncate,
    utc_now,
)
from ib_gateway_mcp.errors import (
    IbApiError,
    IbGatewayMcpError,
    InvalidRequestError,
    NotFoundError,
    SubscriptionLimitError,
)
from ib_gateway_mcp.models.common import SubscriptionOut
from ib_gateway_mcp.models.scanners import (
    ScanCodeInfo,
    ScannerFilterChoice,
    ScannerFilterInfo,
    ScannerInstrumentInfo,
    ScannerLocationInfo,
    ScannerParameters,
    ScannerParameterSection,
    ScannerRow,
    ScannerSnapshot,
    ScannerSpec,
    ScanResult,
)
from ib_gateway_mcp.services.base import BaseService
from ib_gateway_mcp.subscriptions import Stream

if TYPE_CHECKING:
    from ib_gateway_mcp.gateway import Gateway

__all__ = [
    "DEFAULT_PARAMETER_LIMIT",
    "MAX_PARAMETER_LIMIT",
    "MAX_SCANNER_SUBSCRIPTIONS",
    "ScannerCatalog",
    "ScannersService",
    "parse_scanner_parameters",
]

logger = logging.getLogger(__name__)

DEFAULT_PARAMETER_LIMIT = 50
MAX_PARAMETER_LIMIT = 500
MAX_SCANNER_SUBSCRIPTIONS = 10
"""IBKR allows 10 scanner subscriptions per client at a time."""

_MAX_CHOICES = 50
_NO_MATCHES = 165
"""IBKR's "no items retrieved" message for a scan (ib_async treats it as a warning)."""
_NON_FATAL_CODES = frozenset({10167, 10090})
"""Delayed or partial market data notices; the scan itself carries on."""
_SCAN_HINT = (
    "Check scan_code, instrument and location_code with get_scanner_parameters; IBKR "
    f"allows {MAX_SCANNER_SUBSCRIPTIONS} scanner subscriptions at once"
)
_VALUE_TYPES = {
    "double": "number",
    "int": "integer",
    "integer": "integer",
    "long": "integer",
    "string": "text",
    "combo": "choice",
    "date": "date",
    "boolean": "boolean",
    "bool": "boolean",
}


# --- the catalogue -------------------------------------------------------------------------


@dataclass(frozen=True)
class ScannerCatalog:
    """IBKR's scanner parameters, parsed."""

    scan_codes: tuple[ScanCodeInfo, ...]
    instruments: tuple[ScannerInstrumentInfo, ...]
    locations: tuple[ScannerLocationInfo, ...]
    filters: tuple[ScannerFilterInfo, ...]
    instrument_filters: dict[str, frozenset[str]]
    """Filter group ids that apply to each instrument type (keys upper-cased)."""
    fetched_at: datetime


def _text(element: ET.Element, tag: str) -> str | None:
    """The stripped text of ``element``'s direct child ``tag``, or None."""
    return clean_str(element.findtext(tag))


def _codes(value: str | None) -> list[str]:
    """Split IBKR's comma-separated code lists."""
    return [code.strip() for code in (value or "").split(",") if code.strip()]


def _value_type(field: ET.Element) -> str | None:
    """``scanner.filter.DoubleField`` becomes ``number``, and so on."""
    kind = field.get("type") or ""
    name = kind.rsplit(".", 1)[-1].removesuffix("Field").lower()
    if not name:
        return None
    return _VALUE_TYPES.get(name, name)


def _locations(tree: ET.Element, parent: str | None) -> Iterator[ScannerLocationInfo]:
    for location in tree.findall("Location"):
        code = _text(location, "locationCode")
        if code:
            yield ScannerLocationInfo(
                code=code,
                name=_text(location, "displayName"),
                instruments=_codes(location.findtext("instruments")),
                parent=parent,
            )
        for subtree in location.findall("LocationTree"):
            yield from _locations(subtree, code or parent)


def _filters(filter_list: ET.Element) -> Iterator[ScannerFilterInfo]:
    for group in filter_list:
        filter_id = _text(group, "id")
        if not filter_id:
            continue
        category = _text(group, "category")
        for field in group.iter("AbstractField"):
            tag = _text(field, "code")
            if not tag:
                continue
            choices = [
                ScannerFilterChoice(value=value, name=_text(choice, "displayName"))
                for choice in field.iter("ComboValue")
                if (value := _text(choice, "code")) is not None
            ]
            yield ScannerFilterInfo(
                tag=tag,
                name=_text(field, "displayName"),
                filter_id=filter_id,
                category=category,
                value_type=_value_type(field),
                choices=choices[:_MAX_CHOICES],
            )


def parse_scanner_parameters(xml: str, *, fetched_at: datetime | None = None) -> ScannerCatalog:
    """Parse the XML ``reqScannerParameters`` returns into a :class:`ScannerCatalog`.

    Raises:
        IbGatewayMcpError: The document is not XML.
    """
    try:
        # The document comes from the gateway this server is configured to trust.
        root = ET.fromstring(xml)  # noqa: S314
    except ET.ParseError as exc:
        raise IbGatewayMcpError(f"IBKR sent scanner parameters that are not XML: {exc}") from exc

    instruments: list[ScannerInstrumentInfo] = []
    instrument_filters: dict[str, frozenset[str]] = {}
    for instrument_list in root.iter("InstrumentList"):
        for instrument in instrument_list.findall("Instrument"):
            kind = _text(instrument, "type")
            if not kind or kind.upper() in instrument_filters:
                continue
            filter_ids = frozenset(_codes(instrument.findtext("filters")))
            instruments.append(
                ScannerInstrumentInfo(
                    type=kind, name=_text(instrument, "name"), filter_count=len(filter_ids)
                )
            )
            instrument_filters[kind.upper()] = filter_ids

    locations: list[ScannerLocationInfo] = []
    # The top-level tree holds the nested ones; fall back to the first one found anywhere.
    trees = root.findall("LocationTree") or [*root.iter("LocationTree")][:1]
    for tree in trees:
        locations.extend(_locations(tree, None))

    scan_codes: dict[str, ScanCodeInfo] = {}
    for scan_type in root.iter("ScanType"):
        code = _text(scan_type, "scanCode")
        if code and code not in scan_codes:
            scan_codes[code] = ScanCodeInfo(
                code=code,
                name=_text(scan_type, "displayName"),
                instruments=_codes(scan_type.findtext("instruments")),
            )

    # Keyed by (group, tag): a list nested in another, or repeated, must not list twice.
    filters: dict[tuple[str, str], ScannerFilterInfo] = {}
    for filter_list in root.iter("FilterList"):
        for item in _filters(filter_list):
            filters.setdefault((item.filter_id, item.tag), item)

    return ScannerCatalog(
        scan_codes=tuple(scan_codes.values()),
        instruments=tuple(instruments),
        locations=tuple(locations),
        filters=tuple(filters.values()),
        instrument_filters=instrument_filters,
        fetched_at=fetched_at or utc_now(),
    )


def _matches(query: str | None, *values: str | None) -> bool:
    if not query:
        return True
    needle = query.casefold()
    return any(needle in value.casefold() for value in values if value)


def _has_instrument(instruments: Sequence[str], instrument: str | None) -> bool:
    if instrument is None:
        return True
    return instrument.upper() in (code.upper() for code in instruments)


# --- scans ---------------------------------------------------------------------------------


def _format_value(value: str | int | float) -> str:
    """A filter value as IBKR expects it: plain decimal, no exponent."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.10f}".rstrip("0").rstrip(".")
    return value.strip()


def _basic_filters(spec: ScannerSpec) -> dict[str, float | int]:
    """The spec's basic filters that are set, keyed by ``ScannerSubscription`` field."""
    fields: dict[str, float | int | None] = {
        "abovePrice": spec.above_price,
        "belowPrice": spec.below_price,
        "aboveVolume": spec.above_volume,
        "marketCapAbove": spec.market_cap_above,
        "marketCapBelow": spec.market_cap_below,
    }
    return {name: value for name, value in fields.items() if value is not None}


def _subscription(spec: ScannerSpec) -> ScannerSubscription:
    subscription = ScannerSubscription(
        numberOfRows=spec.rows,
        instrument=spec.instrument,
        locationCode=spec.location_code,
        scanCode=spec.scan_code,
    )
    for name, value in _basic_filters(spec).items():
        setattr(subscription, name, value)
    return subscription


def _filter_options(spec: ScannerSpec) -> list[TagValue]:
    options: list[TagValue] = []
    for tag, value in spec.filters.items():
        name = tag.strip()
        if not name:
            raise InvalidRequestError(
                "filters has an empty tag; use the tags get_scanner_parameters lists."
            )
        options.append(TagValue(name, _format_value(value)))
    return options


def _scan_key(spec: ScannerSpec) -> str:
    """Identifies a scan for the subscription registry (same scan, same stream)."""
    parts = [spec.scan_code, spec.instrument, spec.location_code, f"rows={spec.rows}"]
    parts += [f"{name}={_format_value(value)}" for name, value in _basic_filters(spec).items()]
    parts += [
        f"{tag.strip()}={_format_value(value)}" for tag, value in sorted(spec.filters.items())
    ]
    return " ".join(parts)


def _describe_scan(spec: ScannerSpec) -> str:
    return f"{spec.scan_code} ({spec.instrument} in {spec.location_code})"


def _row(data: ScanData, contract: Contract) -> ScannerRow:
    details = data.contractDetails
    return ScannerRow(
        rank=data.rank + 1,
        contract=contract_to_out(contract, description=details.longName or None),
        market_name=clean_str(details.marketName),
        distance=clean_str(data.distance),
        benchmark=clean_str(data.benchmark),
        projection=clean_str(data.projection),
        legs=clean_str(data.legsStr),
    )


def _rows(data: Sequence[ScanData]) -> list[ScannerRow]:
    rows = [
        _row(item, contract)
        for item in data
        if (contract := item.contractDetails.contract) is not None
    ]
    return sorted(rows, key=lambda row: row.rank)


class _ScanStream:
    """One scanner subscription at IBKR: its rows, its errors, and how to stop it."""

    def __init__(self, ib: IB, spec: ScannerSpec, session: Callable[[], int]) -> None:
        self._ib = ib
        self._session = session
        self._opened_in: int | None = None
        self.spec = spec
        self._subscription = _subscription(spec)
        self._options = _filter_options(spec)
        self.data: ScanDataList | None = None
        self.updated_at: datetime | None = None
        self.updates = 0
        self.no_matches = False
        self.error: IbApiError | None = None
        self._first: asyncio.Future[IbApiError | None] | None = None
        self._listening = False

    def open(self) -> None:
        """Send the scanner subscription (sync; raises ``ConnectionError`` when down)."""
        if not self._listening:
            self._ib.errorEvent += self._on_error
            self._listening = True
        self.error = None
        data = self._ib.reqScannerSubscription(self._subscription, [], self._options)
        data.updateEvent += self._on_update
        self.data = data
        self._opened_in = self._session()

    async def open_and_wait(self) -> None:
        """Send the subscription and wait for its first result set.

        Raises:
            IbApiError: IBKR rejected the scan.
        """
        self._first = asyncio.get_running_loop().create_future()
        try:
            self.open()
            error = await self._first
        finally:
            self._first = None
        if error is not None:
            raise error.with_hint(_SCAN_HINT)

    def close(self) -> None:
        """Stop listening and cancel the subscription at IBKR (best effort)."""
        if self._listening:
            self._ib.errorEvent -= self._on_error
            self._listening = False
        data, self.data = self.data, None
        if data is None:
            return
        data.updateEvent -= self._on_update
        if self._opened_in != self._session():
            # Sent in an earlier session: the request died with it, and ib_async reuses
            # request ids, so cancelling this id could stop another request now.
            return
        try:
            self._ib.cancelScannerSubscription(data)
        except (ConnectionError, OSError):
            logger.debug("Scanner subscription %s not cancelled: disconnected", data.reqId)

    def reopen(self) -> None:
        """Re-request the scan after IBKR lost it (error 1101) or after a reconnect."""
        self.close()
        self.open()

    def _on_update(self, data: ScanDataList) -> None:
        if data is not self.data:
            return
        self.updates += 1
        self.updated_at = utc_now()
        self.no_matches = not data
        self._resolve(None)

    def _on_error(self, req_id: int, code: int, message: str, _contract: object) -> None:
        if self.data is None or req_id != self.data.reqId:
            return
        if code == _NO_MATCHES:
            self.no_matches = True
            self._resolve(None)
            return
        if code in _NON_FATAL_CODES or is_informational(code):
            return
        self.error = IbApiError(code, message, req_id)
        self._resolve(self.error)

    def _resolve(self, error: IbApiError | None) -> None:
        if self._first is not None and not self._first.done():
            self._first.set_result(error)

    def rows(self) -> list[ScannerRow]:
        return _rows(self.data or [])

    def snapshot(self) -> ScannerSnapshot:
        return ScannerSnapshot(
            scan_code=self.spec.scan_code,
            instrument=self.spec.instrument,
            location_code=self.spec.location_code,
            rows=self.rows(),
            updated_at=self.updated_at,
            updates=self.updates,
            no_matches=self.no_matches,
            error=str(self.error) if self.error else None,
        )


class ScannersService(BaseService):
    """Market scanner parameters, one-off scans and scanner subscriptions."""

    def __init__(self, gateway: Gateway) -> None:
        super().__init__(gateway)
        self._catalog: ScannerCatalog | None = None
        self._catalog_session: datetime | None = None
        self._catalog_lock = asyncio.Lock()
        self._running_scans = 0
        """One-shot scans in flight; each holds one of IBKR's scanner slots meanwhile."""

    # --- parameters --------------------------------------------------------------------

    async def catalog(self) -> ScannerCatalog:
        """Return the parsed scanner parameters, fetching them once per gateway session."""
        ib = self.ib
        session = self.connection.connected_since
        if self._catalog is not None and self._catalog_session == session:
            return self._catalog
        async with self._catalog_lock:
            if self._catalog is not None and self._catalog_session == session:
                return self._catalog
            xml = await self._call(
                ib.reqScannerParametersAsync,
                what="the scanner parameters",
                exclusive="scannerParams",
            )
            if not isinstance(xml, str) or not xml.strip():
                raise NotFoundError("IBKR returned no scanner parameters; try again later.")
            catalog = await asyncio.to_thread(parse_scanner_parameters, xml, fetched_at=utc_now())
            self._catalog, self._catalog_session = catalog, session
            return catalog

    async def parameters(
        self,
        section: ScannerParameterSection,
        *,
        query: str | None = None,
        instrument: str | None = None,
        limit: int | None = None,
    ) -> ScannerParameters:
        """Browse one section of the scanner catalogue.

        Args:
            section: scan_codes, instruments, locations or filters.
            query: Case-insensitive substring of a code or display name (for filters
                also the filter group and category).
            instrument: Keep only entries for this instrument type (e.g. STK).
            limit: Entries to return (default 50, at most 500).

        Raises:
            NotFoundError: The instrument type is unknown, or nothing matches.
        """
        catalog = await self.catalog()
        query = clean_str(query)
        instrument = clean_str(instrument)
        if instrument is not None and instrument.upper() not in catalog.instrument_filters:
            known = ", ".join(item.type for item in catalog.instruments[:30])
            raise NotFoundError(
                f"Unknown scanner instrument type {instrument!r}. Known types include: {known} "
                "(section 'instruments' lists them all)."
            )
        cap = clamp_limit(limit, default=DEFAULT_PARAMETER_LIMIT, maximum=MAX_PARAMETER_LIMIT)
        result = ScannerParameters(
            section=section,
            query=query,
            instrument=instrument,
            total=0,
            fetched_at=catalog.fetched_at,
        )
        if section == "scan_codes":
            codes = [
                item
                for item in catalog.scan_codes
                if _has_instrument(item.instruments, instrument)
                and _matches(query, item.code, item.name)
            ]
            result.scan_codes, result.truncated = truncate(codes, cap)
            result.total = len(codes)
        elif section == "instruments":
            instruments = [
                item
                for item in catalog.instruments
                if _has_instrument([item.type], instrument)
                and _matches(query, item.type, item.name)
            ]
            result.instruments, result.truncated = truncate(instruments, cap)
            result.total = len(instruments)
        elif section == "locations":
            locations = [
                item
                for item in catalog.locations
                if _has_instrument(item.instruments, instrument)
                and _matches(query, item.code, item.name)
            ]
            result.locations, result.truncated = truncate(locations, cap)
            result.total = len(locations)
        else:
            allowed = catalog.instrument_filters.get(instrument.upper()) if instrument else None
            filters = [
                item
                for item in catalog.filters
                if (allowed is None or item.filter_id in allowed)
                and _matches(query, item.tag, item.name, item.filter_id, item.category)
            ]
            result.filters, result.truncated = truncate(filters, cap)
            result.total = len(filters)
        if not result.total:
            wanted = section.replace("_", " ")
            conditions = [f"matching {query!r}"] if query else []
            if instrument:
                conditions.append(f"for instrument {instrument}")
            raise NotFoundError(
                f"No scanner {wanted} {' '.join(conditions)}. Try a shorter query or "
                "drop the instrument filter."
            )
        return result

    # --- scans -------------------------------------------------------------------------

    def _scanner_subscriptions(self) -> int:
        return sum(1 for info in self.subs.list() if info.kind == "scanner")

    def _ensure_scanner_slot(self) -> None:
        subscribed = self._scanner_subscriptions()
        if subscribed + self._running_scans >= MAX_SCANNER_SUBSCRIPTIONS:
            running = self._running_scans
            busy = f", {running} one-shot running" if running else ""
            raise SubscriptionLimitError(
                f"IBKR allows {MAX_SCANNER_SUBSCRIPTIONS} scanners at a time and all are in "
                f"use ({subscribed} subscribed{busy}). Unsubscribe from a scanner "
                "(list_subscriptions shows them) or retry in a few seconds."
            )

    async def run_scanner(self, spec: ScannerSpec) -> ScanResult:
        """Run a scan once and return its rows, best first.

        Opens a scanner subscription, waits for its first result set, and always
        cancels it again.

        Raises:
            NotFoundError: Nothing matches the scan right now (IBKR message 165).
            IbApiError: IBKR rejected the scan (unknown code, bad filter...).
            SubscriptionLimitError: IBKR's 10 scanner slots are taken (open scanner
                subscriptions plus one-shot scans still running).
            RequestTimeoutError, NotConnectedError: As for ``_call``.
        """
        ib = self.ib
        self._ensure_scanner_slot()
        stream = _ScanStream(ib, spec, self._session)
        self._running_scans += 1
        try:
            await self._call(stream.open_and_wait(), what=f"the {_describe_scan(spec)} scan")
            rows = stream.rows()
        finally:
            self._running_scans -= 1
            stream.close()
        if not rows:
            raise NotFoundError(
                f"Nothing matches the {_describe_scan(spec)} scan right now. Loosen the "
                "filters, try another location, or check that the market is open."
            )
        kept, truncated = truncate(rows, spec.rows)
        return ScanResult(
            scan_code=spec.scan_code,
            instrument=spec.instrument,
            location_code=spec.location_code,
            rows=kept,
            truncated=truncated,
            as_of=stream.updated_at or utc_now(),
        )

    async def subscribe_scanner(self, spec: ScannerSpec) -> SubscriptionOut:
        """Keep a scan running; its rows update as the market moves.

        The first result set must arrive (or the scan be rejected) before the handle
        is returned. The same scan asked for twice shares one subscription.

        Raises:
            IbApiError: IBKR rejected the scan.
            SubscriptionLimitError: No subscription slot is free, or 10 scanner
                subscriptions are already open.
        """
        ib = self.ib
        what = f"the {_describe_scan(spec)} scan"

        async def opener() -> Stream:
            self._ensure_scanner_slot()
            stream = _ScanStream(ib, spec, self._session)
            try:
                await self._call(stream.open_and_wait(), what=what)
            except BaseException:
                stream.close()
                raise
            return Stream(cancel=stream.close, snapshot=stream.snapshot, resubscribe=stream.reopen)

        return await self._subscribe(
            "scanner", _scan_key(spec), opener=opener, meta={"scan": spec.model_dump(mode="json")}
        )
