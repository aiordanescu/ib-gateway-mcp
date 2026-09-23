"""ScannersService: the parsed scanner catalogue, one-shot scans and scanner subscriptions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from ib_async import ScanData, ScanDataList, ScannerSubscription, TagValue

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    IbApiError,
    IbGatewayMcpError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RequestTimeoutError,
    SubscriptionLimitError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.scanners import ScannerSnapshot, ScannerSpec
from ib_gateway_mcp.services.scanners import (
    MAX_SCANNER_SUBSCRIPTIONS,
    ScannersService,
    parse_scanner_parameters,
)
from tests.fakes import emit_error, pending, returns, scan_data, stock

SCANNER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ScanParameterResponse>
  <InstrumentList varName="instrumentList">
    <Instrument>
      <name>US Stocks</name>
      <type>STK</type>
      <filters>PRICE,VOLUME,STKTYPE</filters>
    </Instrument>
    <Instrument>
      <name>US Futures</name>
      <type>FUT.US</type>
      <filters>PRICE</filters>
    </Instrument>
  </InstrumentList>
  <LocationTree varName="locationTree">
    <Location>
      <displayName>US Stocks</displayName>
      <locationCode>STK.US</locationCode>
      <instruments>STK</instruments>
      <LocationTree varName="locationTree">
        <Location>
          <displayName>Listed/NASDAQ</displayName>
          <locationCode>STK.US.MAJOR</locationCode>
          <instruments>STK</instruments>
          <LocationTree varName="locationTree">
            <Location>
              <displayName>NASDAQ</displayName>
              <locationCode>STK.NASDAQ</locationCode>
              <instruments>STK</instruments>
            </Location>
          </LocationTree>
        </Location>
      </LocationTree>
    </Location>
    <Location>
      <displayName>US Futures</displayName>
      <locationCode>FUT.US</locationCode>
      <instruments>FUT.US</instruments>
    </Location>
  </LocationTree>
  <ScanTypeList varName="scanTypeList">
    <ScanType>
      <displayName>Top % Gainers</displayName>
      <scanCode>TOP_PERC_GAIN</scanCode>
      <instruments>STK,FUT.US</instruments>
      <Columns varName="columns">
        <Column><colId>1</colId><name>Change %</name></Column>
      </Columns>
    </ScanType>
    <ScanType>
      <displayName>Top % Losers</displayName>
      <scanCode>TOP_PERC_LOSE</scanCode>
      <instruments>STK</instruments>
    </ScanType>
    <ScanType>
      <displayName>Most Active</displayName>
      <scanCode>MOST_ACTIVE</scanCode>
      <instruments>FUT.US</instruments>
    </ScanType>
  </ScanTypeList>
  <FilterList varName="filterList">
    <RangeFilter>
      <id>PRICE</id>
      <category>Price</category>
      <AbstractField type="scanner.filter.DoubleField">
        <code>priceAbove</code>
        <displayName>Price Above</displayName>
      </AbstractField>
      <AbstractField type="scanner.filter.DoubleField">
        <code>priceBelow</code>
        <displayName>Price Below</displayName>
      </AbstractField>
    </RangeFilter>
    <RangeFilter>
      <id>VOLUME</id>
      <category>Volume</category>
      <AbstractField type="scanner.filter.IntField">
        <code>volumeAbove</code>
        <displayName>Volume Above</displayName>
      </AbstractField>
    </RangeFilter>
    <SimpleFilter>
      <id>STKTYPE</id>
      <category>Fundamentals</category>
      <AbstractField type="scanner.filter.ComboField">
        <code>stkTypes</code>
        <displayName>Stock Type</displayName>
        <ComboValues>
          <ComboValue><code>inc:CORP</code><displayName>Corporation</displayName></ComboValue>
          <ComboValue><code>inc:ADR</code><displayName>ADR</displayName></ComboValue>
        </ComboValues>
      </AbstractField>
    </SimpleFilter>
    <SimpleFilter>
      <category>No id, skipped</category>
    </SimpleFilter>
  </FilterList>
</ScanParameterResponse>
"""


@pytest.fixture
def service(gateway: Gateway) -> ScannersService:
    return gateway.scanners


@pytest.fixture
async def fast_gateway(
    settings_factory: Callable[..., Settings], fake_ib: MagicMock
) -> AsyncIterator[Gateway]:
    """A gateway whose requests time out after 0.1 s."""
    gw = Gateway(settings_factory(request_timeout=0.1), ib_factory=lambda: fake_ib)
    await gw.start()
    try:
        yield gw
    finally:
        await gw.stop()


class FakeScanner:
    """Answers ``reqScannerSubscription`` the way IBKR and ib_async do, one loop turn later.

    ``mode`` is ``rows`` (send ``rows``), ``no_matches`` (message 165: ib_async clears the
    list and emits its update), ``error`` (an error for the request id) or ``silent``.
    """

    def __init__(self, fake_ib: MagicMock, rows: Sequence[ScanData] = ()) -> None:
        self.fake_ib = fake_ib
        self.rows = list(rows)
        self.mode = "rows"
        self.error = (162, "Historical Market Data Service error message:Invalid scan code")
        self.lists: list[ScanDataList] = []
        self.next_id = 500
        fake_ib.reqScannerSubscription.side_effect = self

    def __call__(
        self,
        subscription: ScannerSubscription,
        options: list[TagValue],
        filter_options: list[TagValue],
    ) -> ScanDataList:
        data = ScanDataList()
        data.reqId = self.next_id
        data.subscription = subscription
        data.scannerSubscriptionOptions = options
        data.scannerSubscriptionFilterOptions = filter_options
        self.next_id += 1
        self.lists.append(data)
        asyncio.get_running_loop().call_soon(self.answer, data, self.mode)
        return data

    def answer(self, data: ScanDataList, mode: str) -> None:
        if mode == "rows":
            push(data, self.rows)
        elif mode == "no_matches":
            data.clear()
            data.updateEvent.emit(data)
            emit_error(self.fake_ib, 165, "no items retrieved", req_id=data.reqId)
        elif mode == "error":
            emit_error(self.fake_ib, *self.error, req_id=data.reqId)

    @property
    def last(self) -> ScanDataList:
        return self.lists[-1]

    @property
    def subscription(self) -> ScannerSubscription:
        return self.last.subscription


def push(data: ScanDataList, rows: Sequence[ScanData]) -> None:
    """Deliver a result set the way ib_async's wrapper does."""
    data.clear()
    data.extend(rows)
    data.updateEvent.emit(data)


def rows_for(*symbols: str) -> list[ScanData]:
    return [
        scan_data(rank, stock(symbol, con_id=1000 + rank)) for rank, symbol in enumerate(symbols)
    ]


# --- parsing -------------------------------------------------------------------------------


def test_parse_scanner_parameters() -> None:
    fetched = datetime(2026, 1, 2, tzinfo=UTC)
    catalog = parse_scanner_parameters(SCANNER_XML, fetched_at=fetched)

    assert [item.type for item in catalog.instruments] == ["STK", "FUT.US"]
    assert catalog.instruments[0].name == "US Stocks"
    assert catalog.instruments[0].filter_count == 3
    assert catalog.instrument_filters["STK"] == {"PRICE", "VOLUME", "STKTYPE"}

    locations = {item.code: item for item in catalog.locations}
    assert list(locations) == ["STK.US", "STK.US.MAJOR", "STK.NASDAQ", "FUT.US"]
    assert locations["STK.US"].parent is None
    assert locations["STK.US.MAJOR"].parent == "STK.US"
    assert locations["STK.NASDAQ"].parent == "STK.US.MAJOR"
    assert locations["STK.NASDAQ"].instruments == ["STK"]

    gain = catalog.scan_codes[0]
    assert (gain.code, gain.name, gain.instruments) == (
        "TOP_PERC_GAIN",
        "Top % Gainers",
        ["STK", "FUT.US"],
    )

    filters = {item.tag: item for item in catalog.filters}
    assert list(filters) == ["priceAbove", "priceBelow", "volumeAbove", "stkTypes"]
    assert filters["priceAbove"].value_type == "number"
    assert filters["priceAbove"].filter_id == "PRICE"
    assert filters["priceAbove"].category == "Price"
    assert filters["volumeAbove"].value_type == "integer"
    assert filters["stkTypes"].value_type == "choice"
    assert [(c.value, c.name) for c in filters["stkTypes"].choices] == [
        ("inc:CORP", "Corporation"),
        ("inc:ADR", "ADR"),
    ]
    assert catalog.fetched_at == fetched


def test_parse_rejects_garbage() -> None:
    with pytest.raises(IbGatewayMcpError, match="not XML"):
        parse_scanner_parameters("<ScanParameterResponse>")


def test_parse_caps_choice_lists() -> None:
    values = "".join(f"<ComboValue><code>v{i}</code></ComboValue>" for i in range(80))
    xml = (
        "<r><FilterList><SimpleFilter><id>X</id><AbstractField type='scanner.filter."
        f"ComboField'><code>x</code><ComboValues>{values}</ComboValues></AbstractField>"
        "</SimpleFilter></FilterList></r>"
    )
    catalog = parse_scanner_parameters(xml)
    assert len(catalog.filters[0].choices) == 50


def test_parse_tolerates_other_layouts() -> None:
    xml = (
        "<r><Wrapper><LocationTree><Location><locationCode>A</locationCode>"
        "<LocationTree><Location><locationCode>B</locationCode></Location></LocationTree>"
        "</Location></LocationTree></Wrapper>"
        "<ScanTypeList><ScanType><scanCode>X</scanCode></ScanType></ScanTypeList>"
        "<Other><ScanType><scanCode>X</scanCode><displayName>dup</displayName></ScanType>"
        "<ScanType><displayName>no code</displayName></ScanType></Other></r>"
    )
    catalog = parse_scanner_parameters(xml)
    assert [(item.code, item.parent) for item in catalog.locations] == [("A", None), ("B", "A")]
    assert [(item.code, item.name) for item in catalog.scan_codes] == [("X", None)]
    assert catalog.instruments == ()


def test_parse_lists_repeated_entries_once() -> None:
    group = (
        "<RangeFilter><id>PRICE</id><AbstractField type='scanner.filter.DoubleField'>"
        "<code>priceAbove</code></AbstractField></RangeFilter>"
    )
    instrument = "<Instrument><type>STK</type><filters>PRICE</filters></Instrument>"
    xml = (
        f"<r><InstrumentList>{instrument}</InstrumentList>"
        f"<Other><InstrumentList>{instrument}</InstrumentList></Other>"
        f"<FilterList>{group}</FilterList><Other><FilterList>{group}</FilterList></Other></r>"
    )
    catalog = parse_scanner_parameters(xml)
    assert [item.tag for item in catalog.filters] == ["priceAbove"]
    assert [item.type for item in catalog.instruments] == ["STK"]


# --- parameters ----------------------------------------------------------------------------


async def test_parameters_are_fetched_once_per_session(
    service: ScannersService, gateway: Gateway, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ib.reqScannerParametersAsync.side_effect = returns(SCANNER_XML)
    first, second = await asyncio.gather(
        service.parameters("scan_codes"), service.parameters("locations")
    )
    assert first.total == 3
    assert second.total == 4
    fake_ib.reqScannerParametersAsync.assert_called_once_with()

    # A new gateway session fetches the catalogue again.
    monkeypatch.setattr(gateway.connection, "_connected_since", datetime(2030, 1, 1, tzinfo=UTC))
    await service.parameters("instruments")
    assert fake_ib.reqScannerParametersAsync.call_count == 2


async def test_catalog_is_parsed_once_and_refuses_an_empty_answer(
    service: ScannersService, fake_ib: MagicMock
) -> None:
    fake_ib.reqScannerParametersAsync.side_effect = returns("  ")
    with pytest.raises(NotFoundError, match="no scanner parameters"):
        await service.catalog()

    fake_ib.reqScannerParametersAsync.side_effect = returns(SCANNER_XML)
    catalog = await service.catalog()
    assert [item.code for item in catalog.scan_codes][:1] == ["TOP_PERC_GAIN"]
    assert catalog.fetched_at is not None
    assert await service.catalog() is catalog
    assert fake_ib.reqScannerParametersAsync.call_count == 2  # the empty answer, then one


async def test_scan_codes_filtered_by_query_and_instrument(
    service: ScannersService, fake_ib: MagicMock
) -> None:
    fake_ib.reqScannerParametersAsync.side_effect = returns(SCANNER_XML)
    result = await service.parameters("scan_codes", query="top %")
    assert [item.code for item in result.scan_codes] == ["TOP_PERC_GAIN", "TOP_PERC_LOSE"]
    assert result.query == "top %"

    futures = await service.parameters("scan_codes", instrument="fut.us")
    assert [item.code for item in futures.scan_codes] == ["TOP_PERC_GAIN", "MOST_ACTIVE"]
    assert futures.filters == []


async def test_filters_follow_the_instrument(service: ScannersService, fake_ib: MagicMock) -> None:
    fake_ib.reqScannerParametersAsync.side_effect = returns(SCANNER_XML)
    everything = await service.parameters("filters")
    assert everything.total == 4
    futures = await service.parameters("filters", instrument="FUT.US")
    assert [item.tag for item in futures.filters] == ["priceAbove", "priceBelow"]
    by_category = await service.parameters("filters", query="fundamentals")
    assert [item.tag for item in by_category.filters] == ["stkTypes"]


async def test_locations_and_instruments(service: ScannersService, fake_ib: MagicMock) -> None:
    fake_ib.reqScannerParametersAsync.side_effect = returns(SCANNER_XML)
    locations = await service.parameters("locations", instrument="STK", query="nasdaq")
    assert [item.code for item in locations.locations] == ["STK.US.MAJOR", "STK.NASDAQ"]
    instruments = await service.parameters("instruments", query="futures")
    assert [item.type for item in instruments.instruments] == ["FUT.US"]


async def test_parameters_limit(service: ScannersService, fake_ib: MagicMock) -> None:
    fake_ib.reqScannerParametersAsync.side_effect = returns(SCANNER_XML)
    result = await service.parameters("locations", limit=2)
    assert len(result.locations) == 2
    assert result.truncated is True
    assert result.total == 4
    full = await service.parameters("locations", limit=10)
    assert full.truncated is False


async def test_unknown_instrument(service: ScannersService, fake_ib: MagicMock) -> None:
    fake_ib.reqScannerParametersAsync.side_effect = returns(SCANNER_XML)
    with pytest.raises(
        NotFoundError, match=r"Unknown scanner instrument type 'BOND'.*STK, FUT\.US"
    ):
        await service.parameters("scan_codes", instrument="BOND")


async def test_nothing_matches(service: ScannersService, fake_ib: MagicMock) -> None:
    fake_ib.reqScannerParametersAsync.side_effect = returns(SCANNER_XML)
    with pytest.raises(NotFoundError, match="No scanner scan codes matching 'zzz'"):
        await service.parameters("scan_codes", query="zzz")


async def test_empty_catalogue(service: ScannersService, fake_ib: MagicMock) -> None:
    fake_ib.reqScannerParametersAsync.side_effect = returns("")
    with pytest.raises(NotFoundError, match="no scanner parameters"):
        await service.parameters("scan_codes")


async def test_parameters_timeout(fast_gateway: Gateway, fake_ib: MagicMock) -> None:
    fake_ib.reqScannerParametersAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match="scanner parameters"):
        await fast_gateway.scanners.parameters("scan_codes")


# --- run_scanner ---------------------------------------------------------------------------


async def test_run_scanner(service: ScannersService, fake_ib: MagicMock) -> None:
    scanner = FakeScanner(fake_ib, rows_for("AAA", "BBB", "CCC"))
    spec = ScannerSpec(
        scan_code="TOP_PERC_GAIN",
        above_price=5,
        above_volume=100_000,
        market_cap_below=2.5e9,
        filters={"avgVolumeAbove": 100000, "stkTypes": " inc:CORP ", "changePercAbove": 1.25},
        rows=10,
    )
    result = await service.run_scanner(spec)

    assert [row.rank for row in result.rows] == [1, 2, 3]
    assert [row.contract.symbol for row in result.rows] == ["AAA", "BBB", "CCC"]
    assert result.rows[0].contract.con_id == 1000
    assert result.rows[0].contract.description == "APPLE INC"
    assert result.rows[0].market_name == "NMS"
    assert result.rows[0].distance is None
    assert (result.scan_code, result.instrument, result.location_code) == (
        "TOP_PERC_GAIN",
        "STK",
        "STK.US.MAJOR",
    )
    assert result.truncated is False

    sent = scanner.subscription
    assert (sent.scanCode, sent.instrument, sent.locationCode, sent.numberOfRows) == (
        "TOP_PERC_GAIN",
        "STK",
        "STK.US.MAJOR",
        10,
    )
    assert (sent.abovePrice, sent.aboveVolume, sent.marketCapBelow) == (5, 100_000, 2.5e9)
    assert sent.belowPrice > 1e300  # unset stays IB's UNSET_DOUBLE
    assert scanner.last.scannerSubscriptionFilterOptions == [
        TagValue("avgVolumeAbove", "100000"),
        TagValue("stkTypes", "inc:CORP"),
        TagValue("changePercAbove", "1.25"),
    ]
    fake_ib.cancelScannerSubscription.assert_called_once_with(scanner.last)


async def test_run_scanner_keeps_the_requested_rows(
    service: ScannersService, fake_ib: MagicMock
) -> None:
    FakeScanner(fake_ib, rows_for("C", "A", "B", "D"))
    result = await service.run_scanner(ScannerSpec(scan_code="MOST_ACTIVE", rows=2))
    assert [row.contract.symbol for row in result.rows] == ["C", "A"]
    assert result.truncated is True


async def test_run_scanner_skips_rows_without_a_contract(
    service: ScannersService, fake_ib: MagicMock
) -> None:
    rows = rows_for("AAA")
    rows[0].contractDetails.contract = None
    FakeScanner(fake_ib, [*rows, *rows_for("BBB")])
    result = await service.run_scanner(ScannerSpec(scan_code="MOST_ACTIVE"))
    assert [row.contract.symbol for row in result.rows] == ["BBB"]


async def test_run_scanner_without_matches(service: ScannersService, fake_ib: MagicMock) -> None:
    scanner = FakeScanner(fake_ib)
    scanner.mode = "no_matches"
    with pytest.raises(NotFoundError, match=r"Nothing matches the TOP_PERC_GAIN \(STK in"):
        await service.run_scanner(ScannerSpec(scan_code="TOP_PERC_GAIN"))
    fake_ib.cancelScannerSubscription.assert_called_once_with(scanner.last)


async def test_run_scanner_rejected(service: ScannersService, fake_ib: MagicMock) -> None:
    scanner = FakeScanner(fake_ib)
    scanner.mode = "error"
    listeners = len(fake_ib.errorEvent)
    with pytest.raises(IbApiError) as info:
        await service.run_scanner(ScannerSpec(scan_code="NOPE"))
    assert info.value.error_code == 162
    assert info.value.req_id == scanner.last.reqId
    assert "Invalid scan code" in str(info.value)
    assert "get_scanner_parameters" in str(info.value)
    fake_ib.cancelScannerSubscription.assert_called_once_with(scanner.last)
    assert len(fake_ib.errorEvent) == listeners  # the error listener is gone


async def test_run_scanner_ignores_notices_and_other_requests(
    service: ScannersService, fake_ib: MagicMock
) -> None:
    scanner = FakeScanner(fake_ib, rows_for("AAA"))
    scanner.mode = "silent"

    async def answer_later() -> None:
        await asyncio.sleep(0)
        data = scanner.last
        emit_error(fake_ib, 10167, "Displaying delayed market data", req_id=data.reqId)
        emit_error(fake_ib, 2106, "HMDS data farm connection is OK", req_id=data.reqId)
        emit_error(fake_ib, 162, "someone else's scan", req_id=data.reqId + 99)
        push(ScanDataList(), rows_for("ZZZ"))  # another list: ignored
        push(data, rows_for("AAA"))

    task = asyncio.create_task(answer_later())
    result = await service.run_scanner(ScannerSpec(scan_code="HOT_BY_VOLUME"))
    await task
    assert [row.contract.symbol for row in result.rows] == ["AAA"]


async def test_run_scanner_timeout_cancels(fast_gateway: Gateway, fake_ib: MagicMock) -> None:
    scanner = FakeScanner(fake_ib)
    scanner.mode = "silent"
    with pytest.raises(RequestTimeoutError, match="TOP_PERC_GAIN"):
        await fast_gateway.scanners.run_scanner(ScannerSpec(scan_code="TOP_PERC_GAIN"))
    fake_ib.cancelScannerSubscription.assert_called_once_with(scanner.last)


async def test_run_scanner_not_connected(service: ScannersService, fake_ib: MagicMock) -> None:
    fake_ib.reqScannerSubscription.side_effect = ConnectionError("Not connected")
    with pytest.raises(NotConnectedError):
        await service.run_scanner(ScannerSpec(scan_code="TOP_PERC_GAIN"))
    fake_ib.cancelScannerSubscription.assert_not_called()


async def test_cancel_errors_are_swallowed(service: ScannersService, fake_ib: MagicMock) -> None:
    FakeScanner(fake_ib, rows_for("AAA"))
    fake_ib.cancelScannerSubscription.side_effect = ConnectionError("Not connected")
    result = await service.run_scanner(ScannerSpec(scan_code="TOP_PERC_GAIN"))
    assert len(result.rows) == 1


async def test_run_scanner_rejects_a_blank_filter_tag(
    service: ScannersService, fake_ib: MagicMock
) -> None:
    FakeScanner(fake_ib)
    with pytest.raises(InvalidRequestError, match="empty tag"):
        await service.run_scanner(ScannerSpec(scan_code="TOP_PERC_GAIN", filters={" ": "1"}))
    fake_ib.reqScannerSubscription.assert_not_called()


async def test_filter_values_are_plain_decimals(
    service: ScannersService, fake_ib: MagicMock
) -> None:
    scanner = FakeScanner(fake_ib, rows_for("AAA"))
    spec = ScannerSpec(scan_code="X", filters={"a": 1e10, "b": 0.000001, "c": True, "d": 3})
    await service.run_scanner(spec)
    assert [tv.value for tv in scanner.last.scannerSubscriptionFilterOptions] == [
        "10000000000",
        "0.000001",
        "1",
        "3",
    ]


# --- subscribe_scanner ---------------------------------------------------------------------


def snapshot(gateway: Gateway, subscription_id: str) -> ScannerSnapshot:
    data = gateway.scanners._subscription_data(subscription_id).data
    return ScannerSnapshot.model_validate(data)


async def test_subscribe_scanner(
    service: ScannersService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    scanner = FakeScanner(fake_ib, rows_for("AAA", "BBB"))
    listeners = len(fake_ib.errorEvent)
    spec = ScannerSpec(scan_code="TOP_PERC_GAIN", rows=5, filters={"b": "2", "a": "1"})
    handle = await service.subscribe_scanner(spec)

    assert handle.kind == "scanner"
    assert handle.key == "TOP_PERC_GAIN STK STK.US.MAJOR rows=5 a=1 b=2"
    assert handle.deduplicated is False
    first = snapshot(gateway, handle.subscription_id)
    assert [row.contract.symbol for row in first.rows] == ["AAA", "BBB"]
    assert first.updates == 1
    assert first.updated_at is not None
    assert first.no_matches is False

    push(scanner.last, rows_for("CCC"))
    later = snapshot(gateway, handle.subscription_id)
    assert [row.contract.symbol for row in later.rows] == ["CCC"]
    assert later.updates == 2

    emit_error(fake_ib, 165, "no items retrieved", req_id=scanner.last.reqId)
    scanner.last.clear()
    assert snapshot(gateway, handle.subscription_id).no_matches is True

    emit_error(fake_ib, 162, "API scanner subscription cancelled", req_id=scanner.last.reqId)
    assert "162" in (snapshot(gateway, handle.subscription_id).error or "")

    await gateway.subscriptions.remove(handle.subscription_id)
    fake_ib.cancelScannerSubscription.assert_called_once_with(scanner.lists[0])
    assert len(fake_ib.errorEvent) == listeners


async def test_subscribe_scanner_deduplicates(service: ScannersService, fake_ib: MagicMock) -> None:
    FakeScanner(fake_ib, rows_for("AAA"))
    first = await service.subscribe_scanner(ScannerSpec(scan_code="TOP_PERC_GAIN"))
    again = await service.subscribe_scanner(ScannerSpec(scan_code="TOP_PERC_GAIN"))
    assert again.subscription_id == first.subscription_id
    assert again.deduplicated is True
    assert fake_ib.reqScannerSubscription.call_count == 1


async def test_subscribe_scanner_rejected(
    service: ScannersService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    scanner = FakeScanner(fake_ib)
    scanner.mode = "error"
    with pytest.raises(IbApiError, match="Invalid scan code"):
        await service.subscribe_scanner(ScannerSpec(scan_code="NOPE"))
    fake_ib.cancelScannerSubscription.assert_called_once_with(scanner.last)
    assert gateway.subscriptions.list() == []


async def test_subscribe_scanner_resubscribes(
    service: ScannersService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    scanner = FakeScanner(fake_ib, rows_for("AAA"))
    handle = await service.subscribe_scanner(ScannerSpec(scan_code="TOP_PERC_GAIN"))
    old = scanner.last
    scanner.rows = rows_for("NEW")
    assert await gateway.subscriptions.resubscribe_all() == 1
    await asyncio.sleep(0)
    assert scanner.last is not old
    fake_ib.cancelScannerSubscription.assert_called_once_with(old)
    push(old, rows_for("OLD"))  # the old list no longer counts
    rows = snapshot(gateway, handle.subscription_id).rows
    assert [row.contract.symbol for row in rows] == ["NEW"]


async def test_scanner_resubscribed_after_a_reconnect_cancels_no_stale_request_id(
    service: ScannersService, gateway: Gateway, fake_ib: MagicMock
) -> None:
    """cancelScannerSubscription would also end whatever now owns the old request id."""
    scanner = FakeScanner(fake_ib, rows_for("AAA"))
    await service.subscribe_scanner(ScannerSpec(scan_code="TOP_PERC_GAIN"))
    old = scanner.last
    gateway.connection._session += 1  # what a reconnect does
    assert await gateway.subscriptions.resubscribe_all() == 1
    await asyncio.sleep(0)
    assert scanner.last is not old
    fake_ib.cancelScannerSubscription.assert_not_called()


async def test_scanner_subscription_cap(service: ScannersService, fake_ib: MagicMock) -> None:
    FakeScanner(fake_ib, rows_for("AAA"))
    for index in range(MAX_SCANNER_SUBSCRIPTIONS):
        await service.subscribe_scanner(ScannerSpec(scan_code=f"SCAN_{index}"))
    with pytest.raises(SubscriptionLimitError, match="IBKR allows 10"):
        await service.subscribe_scanner(ScannerSpec(scan_code="ONE_MORE"))
    with pytest.raises(SubscriptionLimitError):
        await service.run_scanner(ScannerSpec(scan_code="ONE_MORE"))
    assert fake_ib.reqScannerSubscription.call_count == MAX_SCANNER_SUBSCRIPTIONS
    # An open scan is still returned.
    again = await service.subscribe_scanner(ScannerSpec(scan_code="SCAN_0"))
    assert again.deduplicated is True


async def test_running_scans_hold_a_scanner_slot(
    service: ScannersService, fake_ib: MagicMock
) -> None:
    scanner = FakeScanner(fake_ib, rows_for("AAA"))
    for index in range(MAX_SCANNER_SUBSCRIPTIONS - 1):
        await service.subscribe_scanner(ScannerSpec(scan_code=f"SCAN_{index}"))
    scanner.mode = "silent"
    slow = asyncio.create_task(service.run_scanner(ScannerSpec(scan_code="SLOW")))
    await asyncio.sleep(0)  # the slow scan is waiting at IBKR and holds the last slot
    slow_list = scanner.last

    with pytest.raises(SubscriptionLimitError, match=r"9 subscribed, 1 one-shot running"):
        await service.run_scanner(ScannerSpec(scan_code="ANOTHER"))
    with pytest.raises(SubscriptionLimitError):
        await service.subscribe_scanner(ScannerSpec(scan_code="ANOTHER"))
    assert fake_ib.reqScannerSubscription.call_count == MAX_SCANNER_SUBSCRIPTIONS

    push(slow_list, rows_for("BBB"))
    assert [row.contract.symbol for row in (await slow).rows] == ["BBB"]
    scanner.mode = "rows"
    result = await service.run_scanner(ScannerSpec(scan_code="NOW_FREE"))  # slot released
    assert [row.contract.symbol for row in result.rows] == ["AAA"]


async def test_failed_scans_release_their_slot(
    service: ScannersService, fake_ib: MagicMock
) -> None:
    scanner = FakeScanner(fake_ib, rows_for("AAA"))
    for index in range(MAX_SCANNER_SUBSCRIPTIONS - 1):
        await service.subscribe_scanner(ScannerSpec(scan_code=f"SCAN_{index}"))
    scanner.mode = "error"
    with pytest.raises(IbApiError):
        await service.run_scanner(ScannerSpec(scan_code="NOPE"))
    scanner.mode = "rows"
    handle = await service.subscribe_scanner(ScannerSpec(scan_code="LAST_SLOT"))
    assert handle.deduplicated is False


async def test_subscribe_scanner_not_connected(
    service: ScannersService, fake_ib: MagicMock
) -> None:
    fake_ib.reqScannerSubscription.side_effect = ConnectionError("Not connected")
    with pytest.raises(NotConnectedError):
        await service.subscribe_scanner(ScannerSpec(scan_code="TOP_PERC_GAIN"))
