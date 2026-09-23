"""The fundamentals toolset through an in-memory MCP client, with structured output validated."""

import json
from unittest.mock import MagicMock

import pytest
from ib_async import WshEventData
from ib_async.wrapper import RequestError

from ib_gateway_mcp.models.fundamentals import FundamentalReport, WshEventList, WshMetadata
from tests.conftest import McpClientFactory
from tests.fakes import contract_details, raises, returns

FUNDAMENTALS_TOOLS = {"get_fundamental_data", "get_wsh_metadata", "get_wsh_events"}

METADATA = json.dumps(
    {
        "event_types": [
            {"tag": "wshe_ed", "name": "Earnings Date"},
            {"tag": "wshe_bod", "name": "Board of Directors Meeting"},
        ]
    }
)
EVENTS = json.dumps(
    [
        {"event_type": "wshe_ed", "index_date": "20261029", "conid": "265598"},
        {"event_type": "wshe_ed", "index_date": "20270128", "conid": "265598"},
    ]
)
AAPL = {"symbol": "AAPL"}


def error_text(result: object) -> str:
    return result.content[0].text  # type: ignore[attr-defined, no-any-return]


def wsh_ready(fake_ib: MagicMock, events: str = EVENTS) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    fake_ib.getWshEventDataAsync.side_effect = returns(events)


async def test_fundamentals_tools_are_listed_with_schemas(mcp_client: McpClientFactory) -> None:
    async with mcp_client() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= FUNDAMENTALS_TOOLS
    for name in FUNDAMENTALS_TOOLS:
        tool = tools[name]
        assert tool.description
        assert tool.output_schema is not None
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    assert "Deprecated by IBKR" in (tools["get_fundamental_data"].description or "")
    report_schema = tools["get_fundamental_data"].input_schema
    assert set(report_schema["required"]) == {"contract", "report_type"}
    assert report_schema["properties"]["max_chars"]["default"] == 50_000
    events_schema = tools["get_wsh_events"].input_schema
    assert "required" not in events_schema or not events_schema["required"]
    assert {"contract", "filter_json", "event_types", "start_date", "limit"} <= set(
        events_schema["properties"]
    )


async def test_get_fundamental_data(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details()])
    fake_ib.reqFundamentalDataAsync.side_effect = returns("<Report>\n  <A>1</A>\n</Report>")
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_fundamental_data",
            {"contract": AAPL, "report_type": "ReportsFinSummary", "max_chars": 1000},
        )
    assert not result.is_error
    report = FundamentalReport.model_validate(result.structured_content)
    assert report.xml == "<Report><A>1</A></Report>"
    assert report.contract.con_id == 265598
    assert report.truncated is False


async def test_get_fundamental_data_report_not_available(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details()])
    fake_ib.reqFundamentalDataAsync.side_effect = raises(
        RequestError(9, 430, "Fundamentals data is not available")
    )
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_fundamental_data", {"contract": AAPL, "report_type": "RESC"}
        )
    assert result.is_error
    assert "not_found: IBKR has no RESC report for AAPL" in error_text(result)


async def test_get_fundamental_data_without_subscription(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details()])
    fake_ib.reqFundamentalDataAsync.side_effect = raises(
        RequestError(9, 10358, "Fundamentals data is not allowed")
    )
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_fundamental_data", {"contract": AAPL, "report_type": "ReportSnapshot"}
        )
    assert result.is_error
    assert "ib_api_error: IB error 10358" in error_text(result)
    assert "fundamentals subscription" in error_text(result)


async def test_get_fundamental_data_refuses_non_stocks(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_fundamental_data",
            {"contract": {"symbol": "ES", "sec_type": "FUT"}, "report_type": "ReportSnapshot"},
        )
    assert result.is_error
    assert "invalid_request:" in error_text(result)
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_get_fundamental_data_validates_report_type(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_fundamental_data", {"contract": AAPL, "report_type": "ReportRatios"}
        )
    assert result.is_error
    assert "report_type" in error_text(result)
    fake_ib.reqFundamentalDataAsync.assert_not_called()


async def test_get_wsh_metadata(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = returns(METADATA)
    async with mcp_client() as client:
        first = await client.call_tool("get_wsh_metadata", {})
        filtered = await client.call_tool("get_wsh_metadata", {"query": "board"})
    assert not first.is_error
    metadata = WshMetadata.model_validate(first.structured_content)
    assert metadata.event_types == ["wshe_bod", "wshe_ed"]
    assert metadata.cached is False
    narrowed = WshMetadata.model_validate(filtered.structured_content)
    assert narrowed.cached is True
    assert narrowed.event_types == ["wshe_bod"]
    assert json.loads(narrowed.metadata_json) == {
        "event_types": [{"tag": "wshe_bod", "name": "Board of Directors Meeting"}]
    }
    assert fake_ib.getWshMetaDataAsync.await_count == 1


async def test_get_wsh_metadata_without_subscription(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.getWshMetaDataAsync.side_effect = raises(
        RequestError(4, 10279, "Failed request WSH metadata")
    )
    async with mcp_client() as client:
        result = await client.call_tool("get_wsh_metadata", {})
    assert result.is_error
    assert "ib_api_error: IB error 10279" in error_text(result)
    assert "Wall Street Horizon corporate event data subscription" in error_text(result)


async def test_get_wsh_events(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details()])
    wsh_ready(fake_ib)
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_wsh_events",
            {
                "contract": AAPL,
                "event_types": ["wshe_ed"],
                "start_date": "2026-10-01",
                "end_date": "2027-03-31",
                "limit": 1,
            },
        )
    assert not result.is_error
    events = WshEventList.model_validate(result.structured_content)
    assert events.events == [json.loads(EVENTS)[0]]
    assert (events.total, events.truncated) == (2, True)
    assert events.contract is not None
    assert events.contract.con_id == 265598
    assert events.request.start_date is not None
    assert events.request.start_date.isoformat() == "2026-10-01"
    sent = fake_ib.getWshEventDataAsync.await_args.args[0]
    assert isinstance(sent, WshEventData)
    assert (sent.startDate, sent.endDate, sent.totalLimit) == ("20261001", "20270331", 2)
    assert json.loads(sent.filter) == {"watchlist": ["265598"], "wshe_ed": "true"}


@pytest.mark.parametrize(
    "raw_filter",
    [
        '{"watchlist": ["8314"], "wshe_ed": "true"}',  # as text (the SDK parses it first)
        {"watchlist": ["8314"], "wshe_ed": "true"},  # as an object
    ],
)
async def test_get_wsh_events_with_raw_filter(
    mcp_client: McpClientFactory, fake_ib: MagicMock, raw_filter: object
) -> None:
    wsh_ready(fake_ib)
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_wsh_events", {"filter_json": raw_filter, "contract": AAPL}
        )
    assert not result.is_error
    events = WshEventList.model_validate(result.structured_content)
    assert events.request.filter is not None
    assert json.loads(events.request.filter) == {"watchlist": ["8314"], "wshe_ed": "true"}
    assert events.contract is None
    assert events.notes == [
        "filter_json was given, so contract was ignored: the filter alone selects the events."
    ]
    fake_ib.reqContractDetailsAsync.assert_not_called()


async def test_get_wsh_events_invalid_request(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    async with mcp_client() as client:
        empty = await client.call_tool("get_wsh_events", {})
        not_json = await client.call_tool("get_wsh_events", {"filter_json": "{wshe_ed: true"})
        not_object = await client.call_tool("get_wsh_events", {"filter_json": "[1]"})
        backwards = await client.call_tool(
            "get_wsh_events",
            {"contract": AAPL, "start_date": "2026-12-01", "end_date": "2026-11-01"},
        )
    assert empty.is_error
    assert "invalid_request: Say which events" in error_text(empty)
    assert not_json.is_error
    assert "invalid_request: filter_json is not valid JSON" in error_text(not_json)
    assert not_object.is_error  # parsed to a list by the SDK, refused by the schema
    assert "filter_json" in error_text(not_object)
    assert backwards.is_error
    assert "invalid_request: start_date 2026-12-01 is after end_date" in error_text(backwards)
    fake_ib.getWshEventDataAsync.assert_not_called()


async def test_get_wsh_events_none_found(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    wsh_ready(fake_ib, "[]")
    async with mcp_client() as client:
        result = await client.call_tool("get_wsh_events", {"fill_portfolio": True})
    assert result.is_error
    assert "not_found: Wall Street Horizon returned no events" in error_text(result)


async def test_get_wsh_events_fill_portfolio_needs_every_account_allowed(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.managedAccounts.return_value = ["DU1234567", "DU7654321"]
    wsh_ready(fake_ib)
    async with mcp_client(ib_account="DU1234567") as client:
        refused = await client.call_tool("get_wsh_events", {"fill_portfolio": True})
    assert refused.is_error
    assert "account_not_allowed: fill_portfolio" in error_text(refused)
    assert "DU7654321" not in error_text(refused)
    fake_ib.getWshEventDataAsync.assert_not_called()

    async with mcp_client(
        ib_account="DU1234567", accounts_allowlist=["DU1234567", "DU7654321"]
    ) as client:
        allowed = await client.call_tool("get_wsh_events", {"fill_portfolio": True})
    assert not allowed.is_error
    assert WshEventList.model_validate(allowed.structured_content).request.fill_portfolio
