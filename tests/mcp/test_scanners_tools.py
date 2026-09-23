"""The scanners toolset through an in-memory MCP client, with structured output validated."""

from unittest.mock import MagicMock

from ib_gateway_mcp.models.common import SubscriptionOut
from ib_gateway_mcp.models.scanners import ScannerParameters, ScannerSnapshot, ScanResult
from tests.conftest import McpClientFactory
from tests.fakes import returns
from tests.unit.test_scanners_service import SCANNER_XML, FakeScanner, rows_for

SCANNER_TOOLS = {"get_scanner_parameters", "run_scanner", "subscribe_scanner"}


def error_text(result: object) -> str:
    return result.content[0].text  # type: ignore[attr-defined,no-any-return]


async def test_scanner_tools_are_listed(mcp_client: McpClientFactory) -> None:
    async with mcp_client() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= SCANNER_TOOLS
    for name in SCANNER_TOOLS:
        tool = tools[name]
        assert tool.description
        assert tool.output_schema is not None
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    assert tools["get_scanner_parameters"].input_schema["required"] == ["section"]
    run = tools["run_scanner"].input_schema
    assert run["required"] == ["scan_code"]
    assert run["properties"]["rows"]["maximum"] == 50
    assert "get_scanner_parameters" in run["properties"]["filters"]["description"]
    assert run["properties"]["location_code"]["default"] == "STK.US.MAJOR"


async def test_get_scanner_parameters(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqScannerParametersAsync.side_effect = returns(SCANNER_XML)
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_scanner_parameters", {"section": "filters", "instrument": "STK", "limit": 2}
        )
    assert not result.is_error
    params = ScannerParameters.model_validate(result.structured_content)
    assert [item.tag for item in params.filters] == ["priceAbove", "priceBelow"]
    assert params.total == 4
    assert params.truncated is True
    assert params.scan_codes == []


async def test_get_scanner_parameters_not_found(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqScannerParametersAsync.side_effect = returns(SCANNER_XML)
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_scanner_parameters", {"section": "scan_codes", "query": "nothing like this"}
        )
        bad_section = await client.call_tool("get_scanner_parameters", {"section": "codes"})
    assert result.is_error
    assert "not_found: No scanner scan codes matching" in error_text(result)
    assert bad_section.is_error


async def test_run_scanner(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    scanner = FakeScanner(fake_ib, rows_for("AAA", "BBB"))
    async with mcp_client() as client:
        result = await client.call_tool(
            "run_scanner",
            {
                "scan_code": "TOP_PERC_GAIN",
                "above_price": 5,
                "filters": {"avgVolumeAbove": 100000},
                "rows": 5,
            },
        )
    assert not result.is_error
    scan = ScanResult.model_validate(result.structured_content)
    assert [(row.rank, row.contract.symbol) for row in scan.rows] == [(1, "AAA"), (2, "BBB")]
    assert (scan.instrument, scan.location_code) == ("STK", "STK.US.MAJOR")
    assert scanner.subscription.abovePrice == 5
    assert scanner.subscription.numberOfRows == 5
    assert scanner.last.scannerSubscriptionFilterOptions[0].value == "100000"
    fake_ib.cancelScannerSubscription.assert_called_once_with(scanner.last)


async def test_run_scanner_errors(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    scanner = FakeScanner(fake_ib)
    async with mcp_client() as client:
        scanner.mode = "error"
        rejected = await client.call_tool("run_scanner", {"scan_code": "NOPE"})
        scanner.mode = "no_matches"
        empty = await client.call_tool("run_scanner", {"scan_code": "TOP_PERC_GAIN"})
        too_many = await client.call_tool("run_scanner", {"scan_code": "X", "rows": 51})
        blank = await client.call_tool("run_scanner", {"scan_code": "  "})
    assert rejected.is_error
    assert "ib_api_error: IB error 162:" in error_text(rejected)
    assert "get_scanner_parameters" in error_text(rejected)
    assert empty.is_error
    assert "not_found: Nothing matches" in error_text(empty)
    assert too_many.is_error
    assert blank.is_error
    assert "scan_code" in error_text(blank)  # a validation error, not an internal one
    assert fake_ib.reqScannerSubscription.call_count == 2


async def test_subscribe_scanner(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    FakeScanner(fake_ib, rows_for("AAA"))
    async with mcp_client() as client:
        result = await client.call_tool(
            "subscribe_scanner", {"scan_code": "HOT_BY_VOLUME", "location_code": "STK.NASDAQ"}
        )
        again = await client.call_tool(
            "subscribe_scanner", {"scan_code": "HOT_BY_VOLUME", "location_code": "STK.NASDAQ"}
        )
        assert mcp_client.gateway is not None
        handle = SubscriptionOut.model_validate(result.structured_content)
        data = mcp_client.gateway.scanners._subscription_data(handle.subscription_id).data
    assert not result.is_error
    assert handle.kind == "scanner"
    assert handle.key == "HOT_BY_VOLUME STK STK.NASDAQ rows=25"
    assert SubscriptionOut.model_validate(again.structured_content).deduplicated is True
    snapshot = ScannerSnapshot.model_validate(data)
    assert [row.contract.symbol for row in snapshot.rows] == ["AAA"]
