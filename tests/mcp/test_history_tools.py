"""The history toolset through an in-memory MCP client, with structured output validated."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from ib_async import HistogramData, HistoricalSchedule, HistoricalSession, HistoricalTickLast
from ib_async.objects import TickAttribLast
from ib_async.wrapper import RequestError

from ib_gateway_mcp.models.history import (
    BarList,
    HeadTimestamp,
    Histogram,
    HistoricalTickList,
    TradingSchedule,
)
from tests.conftest import McpClientFactory
from tests.fakes import FIXED_TIME, bar, contract_details, raises, returns, stock

HISTORY_TOOLS = {
    "get_historical_bars": {
        "contract",
        "bar_size",
        "duration",
        "end",
        "what_to_show",
        "use_rth",
        "limit",
    },
    "get_historical_ticks": {
        "contract",
        "start",
        "end",
        "count",
        "what_to_show",
        "use_rth",
        "ignore_size",
    },
    "get_head_timestamp": {"contract", "what_to_show", "use_rth"},
    "get_histogram": {"contract", "period", "use_rth", "limit"},
    "get_trading_schedule": {"contract", "num_days", "end", "use_rth"},
}
AAPL = {"symbol": "AAPL"}


@pytest.fixture(autouse=True)
def _qualifies(fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])


def error_text(result: Any) -> str:
    assert result.is_error
    text: str = result.content[0].text
    return text


async def test_history_tools_are_listed_with_schemas(mcp_client: McpClientFactory) -> None:
    async with mcp_client() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= set(HISTORY_TOOLS)
    for name, params in HISTORY_TOOLS.items():
        tool = tools[name]
        assert tool.description
        assert tool.output_schema is not None
        properties = tool.input_schema["properties"]
        assert set(properties) >= params
        assert tool.input_schema["required"] == ["contract"]
        assert all(properties[p].get("description") for p in params)
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True


async def test_get_historical_bars(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns(
        [bar(date(2026, 1, 1), 99.0), bar(date(2026, 1, 2), 100.0)]
    )
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_historical_bars",
            {
                "contract": AAPL,
                "bar_size": "1 day",
                "duration": "1 M",
                "end": "2026-01-02T21:00:00Z",
                "limit": 1,
            },
        )
    assert not result.is_error
    bars = BarList.model_validate(result.structured_content)
    assert (bars.total, bars.truncated, bars.duration) == (2, True, "1 M")
    assert [b.close for b in bars.bars] == [100.0]
    assert bars.bars[0].time == date(2026, 1, 2)
    assert bars.end == datetime(2026, 1, 2, 21, tzinfo=UTC)
    sent_end = fake_ib.reqHistoricalDataAsync.call_args.args[1]
    assert sent_end == datetime(2026, 1, 2, 21, tzinfo=UTC)


async def test_get_historical_bars_intraday_times(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns([bar(FIXED_TIME, volume=-1.0)])
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_historical_bars", {"contract": AAPL, "what_to_show": "MIDPOINT"}
        )
    assert result.structured_content is not None
    raw_bar = result.structured_content["bars"][0]
    assert raw_bar["time"] == "2026-01-02T15:30:00Z"
    assert raw_bar["volume"] is None
    assert BarList.model_validate(result.structured_content).bars[0].time == FIXED_TIME


@pytest.mark.parametrize(
    ("arguments", "prefix", "needle"),
    [
        ({"duration": "a while"}, "invalid_request:", "duration"),
        ({"bar_size": "1 secs", "duration": "1 D"}, "invalid_request:", "1 secs"),
        ({"bar_size": "2 secs"}, "", "bar_size"),  # rejected by the input schema
    ],
)
async def test_get_historical_bars_invalid(
    mcp_client: McpClientFactory,
    fake_ib: MagicMock,
    arguments: dict[str, Any],
    prefix: str,
    needle: str,
) -> None:
    async with mcp_client() as client:
        result = await client.call_tool("get_historical_bars", {"contract": AAPL, **arguments})
    text = error_text(result)
    assert prefix in text
    assert needle in text
    fake_ib.reqHistoricalDataAsync.assert_not_called()


async def test_get_historical_bars_no_data(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = raises(
        RequestError(
            5, 162, "Historical Market Data Service error message:HMDS query returned no data"
        )
    )
    async with mcp_client() as client:
        result = await client.call_tool("get_historical_bars", {"contract": AAPL})
    assert "not_found: IBKR has no data" in error_text(result)


async def test_get_historical_bars_pacing(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = raises(
        RequestError(5, 162, "Historical data request pacing violation")
    )
    async with mcp_client() as client:
        result = await client.call_tool("get_historical_bars", {"contract": AAPL})
    text = error_text(result)
    assert "ib_api_error: IB error 162: Historical data request pacing violation" in text
    assert "Wait a minute" in text


async def test_get_historical_bars_rate_limited(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqHistoricalDataAsync.side_effect = returns([bar()])
    async with mcp_client() as client:
        for i in range(5):
            args = {"contract": AAPL, "bar_size": "1 secs", "duration": f"{60 + i} S"}
            assert not (await client.call_tool("get_historical_bars", args)).is_error
        result = await client.call_tool(
            "get_historical_bars", {"contract": AAPL, "bar_size": "1 secs", "duration": "70 S"}
        )
    assert "rate_limit:" in error_text(result)


async def test_get_historical_ticks(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    ticks = [
        HistoricalTickLast(
            FIXED_TIME + timedelta(seconds=i), TickAttribLast(), 100.0 + i, 5.0, "ARCA", ""
        )
        for i in range(2)
    ]
    fake_ib.reqHistoricalTicksAsync.side_effect = returns(ticks)
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_historical_ticks",
            {"contract": AAPL, "start": "2026-01-02T15:30:00Z", "count": 2},
        )
    assert not result.is_error
    out = HistoricalTickList.model_validate(result.structured_content)
    assert [t.price for t in out.ticks] == [100.0, 101.0]
    assert (out.truncated, out.what_to_show, out.start) == (True, "TRADES", FIXED_TIME)
    assert out.ticks[0].exchange == "ARCA"


@pytest.mark.parametrize(
    ("arguments", "needle"),
    [
        ({}, "invalid_request: give exactly one of start"),
        ({"start": "2026-01-02T15:30:00Z", "count": 1001}, "count"),
    ],
)
async def test_get_historical_ticks_invalid(
    mcp_client: McpClientFactory, fake_ib: MagicMock, arguments: dict[str, Any], needle: str
) -> None:
    async with mcp_client() as client:
        result = await client.call_tool("get_historical_ticks", {"contract": AAPL, **arguments})
    assert needle in error_text(result)
    fake_ib.reqHistoricalTicksAsync.assert_not_called()


async def test_get_head_timestamp(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    earliest = datetime(1980, 12, 12, 14, 30, tzinfo=UTC)
    fake_ib.reqHeadTimeStampAsync.side_effect = returns(earliest)
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_head_timestamp", {"contract": AAPL, "what_to_show": "BID", "use_rth": False}
        )
    head = HeadTimestamp.model_validate(result.structured_content)
    assert (head.earliest, head.what_to_show, head.use_rth) == (earliest, "BID", False)
    assert head.contract.symbol == "AAPL"


async def test_get_head_timestamp_unknown_contract(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = raises(RequestError(3, 200, "No security"))
    async with mcp_client() as client:
        result = await client.call_tool("get_head_timestamp", {"contract": {"symbol": "NOPE"}})
    assert "not_found: No contract matches NOPE" in error_text(result)


async def test_get_histogram(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqHistogramDataAsync.side_effect = returns(
        [HistogramData(101.0, 5), HistogramData(100.0, 7), HistogramData(102.0, 1)]
    )
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_histogram", {"contract": AAPL, "period": "2 weeks", "limit": 2}
        )
    histogram = Histogram.model_validate(result.structured_content)
    assert [(e.price, e.count) for e in histogram.entries] == [(100.0, 7), (101.0, 5)]
    assert (histogram.period, histogram.total, histogram.truncated) == ("2 weeks", 3, True)
    assert fake_ib.reqHistogramDataAsync.call_args.args[2] == "2 weeks"


async def test_get_histogram_empty(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqHistogramDataAsync.side_effect = returns([])
    async with mcp_client() as client:
        result = await client.call_tool("get_histogram", {"contract": AAPL})
    assert "not_found:" in error_text(result)


async def test_get_trading_schedule(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqHistoricalScheduleAsync.side_effect = returns(
        HistoricalSchedule(
            startDateTime="20260102-04:00:00",
            endDateTime="20260102-20:00:00",
            timeZone="US/Eastern",
            sessions=[
                HistoricalSession("20260102-04:00:00", "20260102-20:00:00", "20260102"),
            ],
        )
    )
    async with mcp_client() as client:
        result = await client.call_tool(
            "get_trading_schedule", {"contract": AAPL, "num_days": 1, "use_rth": False}
        )
    assert result.structured_content is not None
    assert result.structured_content["sessions"][0]["start"] == "2026-01-02T04:00:00-05:00"
    schedule = TradingSchedule.model_validate(result.structured_content)
    assert (schedule.time_zone, schedule.use_rth) == ("US/Eastern", False)
    assert schedule.sessions[0].ref_date == date(2026, 1, 2)
    assert fake_ib.reqHistoricalScheduleAsync.call_args.args[1:] == (1, "", False)


async def test_get_trading_schedule_rejects_too_many_days(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    async with mcp_client() as client:
        result = await client.call_tool("get_trading_schedule", {"contract": AAPL, "num_days": 31})
    assert "num_days" in error_text(result)
    fake_ib.reqHistoricalScheduleAsync.assert_not_called()
