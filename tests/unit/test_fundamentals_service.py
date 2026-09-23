"""FundamentalsService: fundamental reports and Wall Street Horizon metadata and events."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from ib_async import Contract, WshEventData
from ib_async.util import UNSET_INTEGER
from ib_async.wrapper import RequestError

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.connection import ConnectionManager
from ib_gateway_mcp.errors import (
    AccountNotAllowedError,
    AmbiguousContractError,
    IbApiError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RequestTimeoutError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.common import ContractSpec
from ib_gateway_mcp.services.fundamentals import (
    DEFAULT_WSH_EVENTS,
    MAX_REPORT_CHARS,
    MAX_WSH_EVENTS,
    FundamentalsService,
)
from tests.fakes import FIXED_TIME, contract_details, go_offline, option, pending, raises, returns
from tests.fakes import stock as make_stock

XML = """<?xml version="1.0" encoding="UTF-8"?>
<ReportSnapshot Major="1" Minor="0">
    <CoIDs>
        <CoID Type="CompanyName">Apple Inc.</CoID>
    </CoIDs>
</ReportSnapshot>
"""
COMPACT_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><ReportSnapshot Major="1" Minor="0"><CoIDs>'
    '<CoID Type="CompanyName">Apple Inc.</CoID></CoIDs></ReportSnapshot>'
)

METADATA = json.dumps(
    {
        "meta_data": {
            "event_types": [
                {"tag": "wshe_ed", "name": "Earnings Date", "weight": float("nan")},
                {"tag": "wshe_bod", "name": "Board of Directors Meeting"},
            ],
            "filters": [{"tag": "country", "name": "Country"}],
        }
    },
    indent=2,
)
EVENT = {
    "event_type": "wshe_ed",
    "index_date": "20261029",
    "conid": "265598",
    "data": {"earnings_date": "20261029", "time_of_day": "AMC"},
}
EVENTS = json.dumps([EVENT, {**EVENT, "index_date": "20270128"}])


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    """Short request timeout, so timeout tests finish quickly."""
    return settings_factory(request_timeout=0.2)


@pytest.fixture
def service(gateway: Gateway) -> FundamentalsService:
    return gateway.fundamentals


def qualifies(fake_ib: MagicMock, contract: Contract | None = None) -> None:
    """Make every contract lookup resolve to ``contract`` (default: AAPL stock)."""
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(contract)])


def wsh_ready(fake_ib: MagicMock, events: str = EVENTS) -> None:
    """Metadata and events both answer."""
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    fake_ib.getWshEventDataAsync.side_effect = returns(events)


def sent_event_data(fake_ib: MagicMock, call: int = -1) -> WshEventData:
    data = fake_ib.getWshEventDataAsync.await_args_list[call].args[0]
    assert isinstance(data, WshEventData)
    return data


AAPL = ContractSpec(symbol="AAPL")


# --- fundamental reports ---------------------------------------------------------------------


async def test_fundamental_data_returns_compact_xml(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    fake_ib.reqFundamentalDataAsync.side_effect = returns(XML)
    report = await service.fundamental_data(AAPL, "ReportSnapshot")
    assert report.xml == COMPACT_XML
    assert (report.total_chars, report.truncated) == (len(COMPACT_XML), False)
    assert report.report_type == "ReportSnapshot"
    assert (report.contract.con_id, report.contract.symbol) == (265598, "AAPL")
    contract, report_type = fake_ib.reqFundamentalDataAsync.call_args.args
    assert (contract.conId, contract.secType, report_type) == (265598, "STK", "ReportSnapshot")


async def test_fundamental_data_truncates_at_max_chars(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    fake_ib.reqFundamentalDataAsync.side_effect = returns(XML)
    report = await service.fundamental_data(AAPL, "ReportsFinSummary", max_chars=20)
    assert report.xml == COMPACT_XML[:20]
    assert report.truncated is True
    assert report.total_chars == len(COMPACT_XML)


async def test_fundamental_data_caps_max_chars(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    big = "<a>" + "x" * (MAX_REPORT_CHARS + 10) + "</a>"
    fake_ib.reqFundamentalDataAsync.side_effect = returns(big)
    report = await service.fundamental_data(AAPL, "ReportsFinStatements", max_chars=10**9)
    assert len(report.xml) == MAX_REPORT_CHARS
    assert report.truncated is True


async def test_fundamental_data_rejects_unknown_report_type(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match="ReportSnapshot"):
        await service.fundamental_data(AAPL, "ReportRatios")
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_fundamental_data_rejects_non_stock_spec(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match="stocks only"):
        await service.fundamental_data(ContractSpec(symbol="ES", sec_type="FUT"), "RESC")
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_fundamental_data_rejects_con_id_of_an_option(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib, option())
    with pytest.raises(InvalidRequestError, match="OPT contract"):
        await service.fundamental_data(ContractSpec(con_id=700001), "ReportSnapshot")
    fake_ib.reqFundamentalDataAsync.assert_not_called()


async def test_fundamental_data_unknown_contract(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(RequestError(3, 200, "No security"))
    with pytest.raises(NotFoundError, match="No contract matches NOPE"):
        await service.fundamental_data(ContractSpec(symbol="NOPE"), "ReportSnapshot")


async def test_fundamental_data_ambiguous_contract(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns(
        [contract_details(make_stock(con_id=1)), contract_details(make_stock(con_id=2))]
    )
    with pytest.raises(AmbiguousContractError):
        await service.fundamental_data(AAPL, "ReportSnapshot")


async def test_fundamental_data_error_430_is_not_found(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    fake_ib.reqFundamentalDataAsync.side_effect = raises(
        RequestError(9, 430, "We are sorry, but fundamentals data is not available.")
    )
    with pytest.raises(NotFoundError, match=r"no CalendarReport report for AAPL \(IB error 430"):
        await service.fundamental_data(AAPL, "CalendarReport")


async def test_fundamental_data_without_subscription(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    fake_ib.reqFundamentalDataAsync.side_effect = raises(
        RequestError(9, 10358, "Fundamentals data is not allowed.")
    )
    with pytest.raises(IbApiError, match="fundamentals subscription") as info:
        await service.fundamental_data(AAPL, "ReportSnapshot")
    assert (info.value.error_code, info.value.req_id) == (10358, 9)


async def test_fundamental_data_other_errors_mention_the_deprecation(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    fake_ib.reqFundamentalDataAsync.side_effect = raises(RequestError(9, 10197, "No data"))
    with pytest.raises(IbApiError, match=r"removed in TWS API 10\.50") as info:
        await service.fundamental_data(AAPL, "ReportSnapshot")
    assert info.value.error_code == 10197


@pytest.mark.parametrize("answer", ["", "  \n", []])
async def test_fundamental_data_empty_answer_is_not_found(
    service: FundamentalsService, fake_ib: MagicMock, answer: object
) -> None:
    qualifies(fake_ib)
    fake_ib.reqFundamentalDataAsync.side_effect = returns(answer)
    with pytest.raises(NotFoundError, match="returned no ReportsOwnership report for AAPL"):
        await service.fundamental_data(AAPL, "ReportsOwnership")


async def test_fundamental_data_timeout_cancels_the_request(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    fake_ib.wrapper._futures = {"openOrders": object(), 41: future}

    def request(*_args: Any, **_kwargs: Any) -> asyncio.Future[str]:
        return future

    fake_ib.reqFundamentalDataAsync.side_effect = request
    with pytest.raises(
        RequestTimeoutError, match=r"ReportSnapshot report for AAPL.*removed in TWS API 10\.50"
    ):
        await service.fundamental_data(AAPL, "ReportSnapshot")
    fake_ib.client.cancelFundamentalData.assert_called_once_with(41)


async def test_fundamental_data_timeout_without_a_known_request_id(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    fake_ib.reqFundamentalDataAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError):
        await service.fundamental_data(AAPL, "ReportSnapshot")
    fake_ib.client.cancelFundamentalData.assert_not_called()


async def test_fundamental_data_not_connected(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    go_offline(fake_ib)
    with pytest.raises(NotConnectedError):
        await service.fundamental_data(AAPL, "ReportSnapshot")


# --- WSH metadata ------------------------------------------------------------------------------


async def test_wsh_metadata_is_compacted_and_cached(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    first = await service.wsh_metadata()
    second = await service.wsh_metadata()
    assert first.cached is False
    assert second.cached is True
    assert second.fetched_at == first.fetched_at
    assert fake_ib.getWshMetaDataAsync.await_count == 1
    assert first.event_types == ["wshe_bod", "wshe_ed"]
    assert "\n" not in first.metadata_json
    assert first.total_chars == len(first.metadata_json)
    assert first.truncated is False
    parsed = json.loads(first.metadata_json)
    assert parsed["meta_data"]["event_types"][0]["weight"] is None  # NaN became null


async def test_wsh_metadata_query_keeps_matching_entries(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    result = await service.wsh_metadata(query="  EARNINGS ")
    assert result.query == "EARNINGS"
    assert json.loads(result.metadata_json) == {
        "meta_data": {"event_types": [{"tag": "wshe_ed", "name": "Earnings Date", "weight": None}]}
    }
    assert result.event_types == ["wshe_ed"]


async def test_wsh_metadata_query_matching_a_key_keeps_the_subtree(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    result = await service.wsh_metadata(query="filters")
    assert json.loads(result.metadata_json) == {
        "meta_data": {"filters": [{"tag": "country", "name": "Country"}]}
    }


async def test_wsh_metadata_query_without_match(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    with pytest.raises(NotFoundError, match=r"mentions 'zzz'\. Event types: wshe_bod, wshe_ed"):
        await service.wsh_metadata(query="zzz")


async def test_wsh_metadata_truncates(service: FundamentalsService, fake_ib: MagicMock) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    result = await service.wsh_metadata(max_chars=30)
    assert len(result.metadata_json) == 30
    assert result.truncated is True
    assert result.total_chars > 30


async def test_wsh_metadata_passes_non_json_through(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = returns("  not json: wshe_ed  ")
    result = await service.wsh_metadata(query="WSHE")
    assert result.metadata_json == "not json: wshe_ed"
    with pytest.raises(NotFoundError):
        await service.wsh_metadata(query="dividend")


@pytest.mark.parametrize("answer", ["", "   "])
async def test_wsh_metadata_empty_answer(
    service: FundamentalsService, fake_ib: MagicMock, answer: str
) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = returns(answer)
    with pytest.raises(NotFoundError, match="no Wall Street Horizon metadata"):
        await service.wsh_metadata()


async def test_wsh_metadata_error_mentions_the_subscription(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = raises(
        RequestError(4, 10279, "Failed request WSH metadata.")
    )
    with pytest.raises(IbApiError, match="Wall Street Horizon corporate event data") as info:
        await service.wsh_metadata()
    assert info.value.error_code == 10279
    assert "IB error 10279: Failed request WSH metadata. WSH requests" in str(info.value)


async def test_wsh_metadata_duplicate_request_error(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = raises(RequestError(4, 10278, "Duplicate"))
    with pytest.raises(IbApiError, match="still active"):
        await service.wsh_metadata()


async def test_wsh_metadata_timeout_cancels_the_request(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match="Wall Street Horizon metadata"):
        await service.wsh_metadata()
    fake_ib.cancelWshMetaData.assert_called_once_with()


async def test_wsh_metadata_not_connected(service: FundamentalsService, fake_ib: MagicMock) -> None:
    go_offline(fake_ib)
    with pytest.raises(NotConnectedError):
        await service.wsh_metadata()


# --- WSH events --------------------------------------------------------------------------------


async def test_wsh_events_for_a_contract(service: FundamentalsService, fake_ib: MagicMock) -> None:
    qualifies(fake_ib)
    wsh_ready(fake_ib)
    result = await service.wsh_events(contract=AAPL)
    assert fake_ib.getWshMetaDataAsync.await_count == 1  # metadata first, as IBKR requires
    data = sent_event_data(fake_ib)
    assert (data.conId, data.filter) == (265598, "")
    assert (data.startDate, data.endDate) == ("", "")
    assert data.totalLimit == DEFAULT_WSH_EVENTS + 1
    assert result.events == [EVENT, {**EVENT, "index_date": "20270128"}]
    assert (result.total, result.truncated) == (2, False)
    assert result.contract is not None
    assert result.contract.con_id == 265598
    assert result.request.con_id == 265598
    assert result.request.filter is None
    assert result.notes == []


async def test_wsh_events_with_event_types_builds_a_filter(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    wsh_ready(fake_ib)
    result = await service.wsh_events(
        contract=AAPL,
        event_types=[" WSHE_ED", "wshe_bod", "wshe_ed", ""],
        start_date=date(2026, 10, 1),
        end_date=date(2026, 12, 31),
        fill_competitors=True,
    )
    data = sent_event_data(fake_ib)
    assert data.conId == UNSET_INTEGER
    assert json.loads(data.filter) == {
        "watchlist": ["265598"],
        "wshe_ed": "true",
        "wshe_bod": "true",
    }
    assert (data.startDate, data.endDate) == ("20261001", "20261231")
    assert (data.fillWatchlist, data.fillPortfolio, data.fillCompetitors) == (False, False, True)
    assert result.request.filter == data.filter
    assert result.request.start_date == date(2026, 10, 1)


async def test_wsh_events_event_types_without_contract(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    wsh_ready(fake_ib)
    result = await service.wsh_events(event_types=["wshe_ed"], fill_portfolio=True)
    data = sent_event_data(fake_ib)
    assert json.loads(data.filter) == {"wshe_ed": "true"}
    assert data.fillPortfolio is True
    assert result.contract is None
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_wsh_events_fill_flag_alone(service: FundamentalsService, fake_ib: MagicMock) -> None:
    wsh_ready(fake_ib)
    await service.wsh_events(fill_watchlist=True)
    data = sent_event_data(fake_ib)
    assert (data.conId, data.filter, data.fillWatchlist) == (UNSET_INTEGER, "", True)


async def test_wsh_events_filter_json_overrides_contract_and_types(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    wsh_ready(fake_ib)
    raw = '{"watchlist": ["8314"], "wshe_bod": "true"}'
    result = await service.wsh_events(
        contract=AAPL, event_types=["wshe_ed"], filter_json=f"  {raw} ", limit=5
    )
    data = sent_event_data(fake_ib)
    assert (data.filter, data.conId, data.totalLimit) == (raw, UNSET_INTEGER, 6)
    fake_ib.reqContractDetailsAsync.assert_not_called()
    assert result.contract is None
    assert result.notes == [
        "filter_json was given, so contract and event_types were ignored: the filter alone "
        "selects the events."
    ]


@pytest.mark.parametrize(
    ("filter_json", "message"),
    [("{not json", "not valid JSON"), ('["wshe_ed"]', "must be a JSON object")],
)
async def test_wsh_events_rejects_bad_filter_json(
    service: FundamentalsService, fake_ib: MagicMock, filter_json: str, message: str
) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        await service.wsh_events(filter_json=filter_json)
    fake_ib.getWshEventDataAsync.assert_not_called()


async def test_wsh_events_filter_as_a_mapping(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    wsh_ready(fake_ib)
    result = await service.wsh_events(filter_json={"watchlist": ["8314"], "wshe_ed": "true"})
    assert sent_event_data(fake_ib).filter == '{"watchlist":["8314"],"wshe_ed":"true"}'
    assert result.notes == []


async def test_wsh_events_filter_mapping_must_be_json(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match="cannot be sent as JSON"):
        await service.wsh_events(filter_json={"when": date(2026, 1, 1)})
    fake_ib.getWshMetaDataAsync.assert_not_called()


@pytest.mark.parametrize("empty", ["  ", {}])
async def test_wsh_events_empty_filter_counts_as_none(
    service: FundamentalsService, fake_ib: MagicMock, empty: object
) -> None:
    with pytest.raises(InvalidRequestError, match="Say which events"):
        await service.wsh_events(filter_json=empty)  # type: ignore[arg-type]


async def test_wsh_events_needs_something_to_ask_for(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match="Say which events"):
        await service.wsh_events(event_types=["  "])
    fake_ib.getWshMetaDataAsync.assert_not_called()


async def test_wsh_events_fill_competitors_alone_is_not_enough(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match="fill_competitors only adds"):
        await service.wsh_events(fill_competitors=True)
    fake_ib.getWshMetaDataAsync.assert_not_called()


async def test_wsh_events_fill_portfolio_refused_when_accounts_are_hidden(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    # The login manages a second account this server may not read: its holdings would
    # leak into the events through WSH fillPortfolio.
    service.accounts.refresh(["DU1234567", "DU7654321"])
    wsh_ready(fake_ib)
    with pytest.raises(AccountNotAllowedError, match="fill_portfolio") as info:
        await service.wsh_events(event_types=["wshe_ed"], fill_portfolio=True)
    assert "DU7654321" not in str(info.value)
    fake_ib.getWshMetaDataAsync.assert_not_called()
    fake_ib.getWshEventDataAsync.assert_not_called()
    # The same login without fill_portfolio is fine.
    result = await service.wsh_events(event_types=["wshe_ed"], fill_watchlist=True)
    assert result.total == 2


async def test_wsh_events_rejects_dates_in_the_wrong_order(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    with pytest.raises(InvalidRequestError, match="after end_date"):
        await service.wsh_events(
            contract=AAPL, start_date=date(2026, 12, 1), end_date=date(2026, 11, 1)
        )


async def test_wsh_events_rejects_event_types_missing_from_metadata(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    wsh_ready(fake_ib)
    with pytest.raises(InvalidRequestError, match=r"wshe_nope\. Known types: wshe_bod, wshe_ed"):
        await service.wsh_events(event_types=["wshe_ed", "wshe_nope"])
    fake_ib.getWshEventDataAsync.assert_not_called()


async def test_wsh_events_limit_truncates(service: FundamentalsService, fake_ib: MagicMock) -> None:
    qualifies(fake_ib)
    wsh_ready(fake_ib, json.dumps([{"n": n} for n in range(5)]))
    result = await service.wsh_events(contract=AAPL, limit=2)
    assert sent_event_data(fake_ib).totalLimit == 3
    assert result.events == [{"n": 0}, {"n": 1}]
    assert (result.total, result.truncated) == (5, True)


async def test_wsh_events_limit_is_capped(service: FundamentalsService, fake_ib: MagicMock) -> None:
    qualifies(fake_ib)
    wsh_ready(fake_ib)
    result = await service.wsh_events(contract=AAPL, limit=10_000)
    assert sent_event_data(fake_ib).totalLimit == MAX_WSH_EVENTS
    assert result.request.total_limit == MAX_WSH_EVENTS
    assert result.truncated is False


@pytest.mark.parametrize(("returned", "truncated"), [(MAX_WSH_EVENTS, True), (99, False)])
async def test_wsh_events_at_the_cap_a_full_answer_counts_as_truncated(
    service: FundamentalsService, fake_ib: MagicMock, returned: int, truncated: bool
) -> None:
    # At the cap no extra event can be asked for, so an answer that fills the request
    # may be incomplete.
    qualifies(fake_ib)
    wsh_ready(fake_ib, json.dumps([{"n": n} for n in range(returned)]))
    result = await service.wsh_events(contract=AAPL, limit=MAX_WSH_EVENTS)
    assert (len(result.events), result.total, result.truncated) == (returned, returned, truncated)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (json.dumps({"events": [{"a": 1}, {"a": 2}]}), [{"a": 1}, {"a": 2}]),
        (
            json.dumps({"event_type": "wshe_ed", "data": {"x": 1}}),
            [{"event_type": "wshe_ed", "data": {"x": 1}}],
        ),
        ('{"a": 1}\n{"a": 2}\n', [{"a": 1}, {"a": 2}]),
        ('[1, "two", {"a": NaN}]', [{"value": 1}, {"value": "two"}, {"a": None}]),
        ("[] ", None),
        ("<html>oops</html>", [{"raw": "<html>oops</html>"}]),
        ('{"a": 1} trailing', [{"a": 1}, {"raw": "trailing"}]),
    ],
)
async def test_wsh_events_parses_event_shapes(
    service: FundamentalsService,
    fake_ib: MagicMock,
    raw: str,
    expected: list[dict[str, Any]] | None,
) -> None:
    qualifies(fake_ib)
    wsh_ready(fake_ib, raw)
    if expected is None:
        with pytest.raises(NotFoundError, match="no events for AAPL"):
            await service.wsh_events(contract=AAPL)
        return
    result = await service.wsh_events(contract=AAPL)
    assert result.events == expected


async def test_wsh_events_empty_answer_is_not_found(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    wsh_ready(fake_ib, "")
    with pytest.raises(NotFoundError, match="no events for this filter"):
        await service.wsh_events(event_types=["wshe_ed"])


async def test_wsh_events_reuse_metadata_within_a_session(
    service: FundamentalsService, fake_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    qualifies(fake_ib)
    wsh_ready(fake_ib)
    await service.wsh_events(contract=AAPL)
    await service.wsh_events(contract=AAPL)
    assert fake_ib.getWshMetaDataAsync.await_count == 1
    # A new session (reconnect) must request the metadata again before events...
    later = FIXED_TIME + timedelta(days=1)
    monkeypatch.setattr(ConnectionManager, "connected_since", property(lambda _self: later))
    await service.wsh_events(contract=AAPL)
    assert fake_ib.getWshMetaDataAsync.await_count == 2
    # ...while get_wsh_metadata keeps serving the cache.
    assert (await service.wsh_metadata()).cached is True
    assert fake_ib.getWshMetaDataAsync.await_count == 2


async def test_wsh_events_rerequest_metadata_when_ibkr_says_so(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    fake_ib.getWshEventDataAsync.side_effect = [
        RequestError(8, 10282, "WSH metadata not requested"),
        EVENTS,
    ]
    result = await service.wsh_events(contract=AAPL)
    assert result.total == 2
    assert fake_ib.getWshMetaDataAsync.await_count == 2
    assert fake_ib.getWshEventDataAsync.await_count == 2


async def test_wsh_events_metadata_error_after_retry_is_mapped(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    fake_ib.getWshEventDataAsync.side_effect = raises(
        RequestError(8, 10282, "WSH metadata not requested")
    )
    with pytest.raises(IbApiError, match="WSH requests need") as info:
        await service.wsh_events(contract=AAPL)
    assert info.value.error_code == 10282
    assert fake_ib.getWshEventDataAsync.await_count == 2


async def test_wsh_events_error_mentions_the_subscription(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    fake_ib.getWshEventDataAsync.side_effect = raises(
        RequestError(8, 10283, "Fail request WSH event data")
    )
    with pytest.raises(IbApiError, match="Wall Street Horizon corporate event data") as info:
        await service.wsh_events(contract=AAPL)
    assert (info.value.error_code, info.value.req_id) == (10283, 8)
    assert fake_ib.getWshEventDataAsync.await_count == 1


async def test_wsh_events_metadata_failure_stops_the_request(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = raises(RequestError(4, 10279, "Failed"))
    with pytest.raises(IbApiError, match="WSH requests need"):
        await service.wsh_events(fill_portfolio=True)
    fake_ib.getWshEventDataAsync.assert_not_called()


async def test_wsh_events_timeout_cancels_the_request(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    fake_ib.getWshEventDataAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match="Wall Street Horizon events"):
        await service.wsh_events(fill_portfolio=True)
    fake_ib.cancelWshEventData.assert_called_once_with()


async def test_wsh_requests_run_one_at_a_time(
    service: FundamentalsService, fake_ib: MagicMock
) -> None:
    qualifies(fake_ib)
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    active = peak = 0

    async def answer(_data: WshEventData) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return EVENTS

    fake_ib.getWshEventDataAsync.side_effect = answer
    results = await asyncio.gather(*(service.wsh_events(contract=AAPL) for _ in range(3)))
    assert [r.total for r in results] == [2, 2, 2]
    assert peak == 1
    assert fake_ib.getWshMetaDataAsync.await_count == 1
