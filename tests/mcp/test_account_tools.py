"""The account toolset through an in-memory MCP client, with structured output validated."""

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

from ib_async import AccountValue, CommissionReport, Fill, Order, OrderState, PnL, PnLSingle
from mcp_types import CallToolResult

from ib_gateway_mcp.models.account import (
    AccountPnl,
    AccountSummary,
    AccountValueList,
    CompletedOrderList,
    ExecutionList,
    OpenOrderList,
    Portfolio,
    PositionList,
    PositionPnl,
)
from tests.conftest import McpClientFactory
from tests.fakes import (
    FIXED_TIME,
    PAPER_ACCOUNT,
    account_value,
    contract_details,
    emit_error,
    execution,
    go_offline,
    portfolio_item,
    position,
    returns,
    stock,
    trade,
)

ACCOUNT_TOOLS = {
    "get_account_summary",
    "get_account_values",
    "get_positions",
    "get_portfolio",
    "get_pnl",
    "get_position_pnl",
    "get_executions",
    "get_open_orders",
    "get_completed_orders",
}


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    return result.content[0].text  # type: ignore[union-attr]


def soon(action: Any) -> None:
    asyncio.get_running_loop().call_soon(action)


async def test_account_tools_are_listed_with_schemas(mcp_client: McpClientFactory) -> None:
    async with mcp_client() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= ACCOUNT_TOOLS
    for name in ACCOUNT_TOOLS:
        tool = tools[name]
        assert tool.description
        assert tool.output_schema is not None
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert "account" in tool.input_schema["properties"]
    executions = tools["get_executions"].input_schema["properties"]
    assert set(executions) == {"account", "symbol", "sec_type", "side", "since", "limit"}
    assert tools["get_position_pnl"].input_schema["required"] == ["contract"]


async def test_get_account_summary(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.accountSummaryAsync.side_effect = returns(
        [
            AccountValue(PAPER_ACCOUNT, "NetLiquidation", "100000", "USD", ""),
            AccountValue(PAPER_ACCOUNT, "DayTradesRemaining", "3", "", ""),
        ]
    )
    async with mcp_client() as client:
        result = await client.call_tool("get_account_summary", {})
        bad_tag = await client.call_tool("get_account_summary", {"tags": ["Nope"]})
    summary = AccountSummary.model_validate(result.structured_content)
    assert (summary.net_liquidation, summary.day_trades_remaining) == (100000, 3)
    assert "invalid_request: Unknown tag(s) for this account: Nope" in error_text(bad_tag)


async def test_get_account_values(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.accountValues.return_value = [
        account_value("CashBalance", "1", currency="USD"),
        account_value("CashBalance", "2", currency="EUR"),
        account_value("NetLiquidation", "3"),
    ]
    async with mcp_client() as client:
        result = await client.call_tool("get_account_values", {"tags": ["CashBalance"], "limit": 1})
    values = AccountValueList.model_validate(result.structured_content)
    assert [(v.tag, v.currency, v.amount) for v in values.values] == [("CashBalance", "EUR", 2)]
    assert (values.total, values.truncated) == (2, True)


async def test_get_positions(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.positions.return_value = [position(qty=10, avg_cost=95)]
    async with mcp_client() as client:
        result = await client.call_tool("get_positions", {})
    positions = PositionList.model_validate(result.structured_content)
    assert [(p.account, p.contract.con_id, p.position) for p in positions.positions] == [
        (PAPER_ACCOUNT, 265598, 10)
    ]


async def test_get_portfolio(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.portfolio.return_value = [portfolio_item(qty=2)]
    async with mcp_client() as client:
        result = await client.call_tool("get_portfolio", {})
    portfolio = Portfolio.model_validate(result.structured_content)
    assert [(i.position, i.market_value, i.unrealized_pnl) for i in portfolio.items] == [
        (2, 200.0, 10.0)
    ]


async def test_get_pnl(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.pnl.return_value = []

    def req_pnl(account: str, modelCode: str = "") -> PnL:
        entry = PnL(account, modelCode)

        def update() -> None:
            entry.dailyPnL, entry.unrealizedPnL, entry.realizedPnL = 10.0, float("nan"), 2.0
            fake_ib.pnlEvent.emit(entry)

        soon(update)
        return entry

    fake_ib.reqPnL.side_effect = req_pnl
    async with mcp_client() as client:
        result = await client.call_tool("get_pnl", {"model_code": "MODEL1"})
    pnl = AccountPnl.model_validate(result.structured_content)
    assert (pnl.daily_pnl, pnl.unrealized_pnl, pnl.realized_pnl) == (10.0, None, 2.0)
    assert pnl.model_code == "MODEL1"
    fake_ib.cancelPnL.assert_called_once_with(PAPER_ACCOUNT, "MODEL1")


async def test_get_pnl_times_out(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.pnl.return_value = []
    fake_ib.reqPnL.return_value = PnL(PAPER_ACCOUNT, "")
    async with mcp_client() as client:
        assert mcp_client.gateway is not None
        mcp_client.gateway.account.pnl_wait = 0.05
        result = await client.call_tool("get_pnl", {})
    assert "request_timeout: Timed out after 0.05s" in error_text(result)


async def test_get_position_pnl(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.positions.return_value = [position()]
    fake_ib.pnlSingle.return_value = [
        PnLSingle(PAPER_ACCOUNT, "", 265598, 1.0, 2.0, 3.0, 10, 1000.0)
    ]
    async with mcp_client() as client:
        result = await client.call_tool("get_position_pnl", {"contract": {"con_id": 265598}})
    pnl = PositionPnl.model_validate(result.structured_content)
    assert (pnl.contract.symbol, pnl.position, pnl.market_value) == ("AAPL", 10, 1000.0)


async def test_get_position_pnl_not_found(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.positions.return_value = []
    fake_ib.pnlSingle.return_value = []

    def req_pnl_single(account: str, modelCode: str, conId: int) -> PnLSingle:
        entry = PnLSingle(account, modelCode, conId)  # no position, every value unset
        soon(lambda: fake_ib.pnlSingleEvent.emit(entry))
        return entry

    fake_ib.reqPnLSingle.side_effect = req_pnl_single
    async with mcp_client() as client:
        result = await client.call_tool("get_position_pnl", {"contract": {"symbol": "AAPL"}})
    assert "not_found: Account DU1234567 has no position" in error_text(result)
    fake_ib.cancelPnLSingle.assert_called_once_with(PAPER_ACCOUNT, "", 265598)


async def test_get_position_pnl_without_a_position_and_no_update_is_not_found(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.positions.return_value = []
    fake_ib.pnlSingle.return_value = []
    fake_ib.reqPnLSingle.return_value = PnLSingle(PAPER_ACCOUNT, "", 265598)  # IBKR stays silent
    async with mcp_client() as client:
        assert mcp_client.gateway is not None
        mcp_client.gateway.account.pnl_wait = 0.05
        result = await client.call_tool("get_position_pnl", {"contract": {"con_id": 265598}})
    text = error_text(result)
    assert "not_found: Account DU1234567 has no position and no P&L today" in text
    assert "request_timeout" not in text
    fake_ib.cancelPnLSingle.assert_called_once_with(PAPER_ACCOUNT, "", 265598)


async def test_get_executions(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    def fill(exec_id: str, minutes: int, side: str) -> Fill:
        the_execution = execution(
            execId=exec_id, side=side, time=FIXED_TIME + timedelta(minutes=minutes)
        )
        report = CommissionReport(execId=exec_id, commission=1.0, currency="USD")
        return Fill(stock(), the_execution, report, the_execution.time)

    fake_ib.reqExecutionsAsync.side_effect = returns(
        [fill("e1", 0, "BOT"), fill("e2", 5, "SLD"), fill("e3", 10, "BOT")]
    )
    fake_ib.fills.return_value = []
    since = (FIXED_TIME + timedelta(minutes=1)).isoformat()
    async with mcp_client() as client:
        result = await client.call_tool("get_executions", {"side": "BUY", "since": since})
    executions = ExecutionList.model_validate(result.structured_content)
    assert [(e.exec_id, e.side, e.commission) for e in executions.executions] == [
        ("e3", "BUY", 1.0)
    ]


async def test_get_open_orders(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    own = trade()
    own.order.clientId = 80
    other = trade(
        order=Order(
            orderId=3,
            clientId=5,
            permId=9,
            action="SELL",
            totalQuantity=1,
            orderType="MKT",
            account=PAPER_ACCOUNT,
        )
    )
    fake_ib.reqAllOpenOrdersAsync.side_effect = returns([own, other])
    fake_ib.reqOpenOrdersAsync.side_effect = returns([own])
    async with mcp_client() as client:
        everyone = await client.call_tool("get_open_orders", {})
        mine = await client.call_tool("get_open_orders", {"include_other_clients": False})
    orders = OpenOrderList.model_validate(everyone.structured_content)
    assert [(o.order_id, o.modifiable) for o in orders.orders] == [(7, True), (3, False)]
    mine_list = OpenOrderList.model_validate(mine.structured_content)
    assert [o.order_id for o in mine_list.orders] == [7]
    assert orders.note is None
    assert mine_list.note is None


async def test_get_completed_orders(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    done = trade(status="Filled")
    done.order.permId = 77
    state = OrderState(status="Filled", completedTime="20260102 10:00:00 America/New_York")

    async def req_completed(api_only: bool) -> list[Any]:
        fake_ib.wrapper.completedOrder(done.contract, done.order, state)
        return [done]

    fake_ib.reqCompletedOrdersAsync.side_effect = req_completed
    async with mcp_client() as client:
        result = await client.call_tool("get_completed_orders", {"api_only": True})
    orders = CompletedOrderList.model_validate(result.structured_content)
    assert [(o.perm_id, o.status) for o in orders.orders] == [(77, "Filled")]
    assert orders.orders[0].completed_at == FIXED_TIME.replace(hour=15, minute=0)


READ_ONLY_TEXT = (
    "Error validating request.-'bN' : cause - The API interface is currently in Read-Only mode."
)


async def test_order_reads_on_a_read_only_api(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    own = trade()
    own.order.clientId = 80
    other = trade(
        order=Order(
            orderId=3,
            clientId=5,
            permId=9,
            action="SELL",
            totalQuantity=1,
            orderType="MKT",
            account=PAPER_ACCOUNT,
        )
    )
    fake_ib.reqAllOpenOrdersAsync.side_effect = returns([own, other])
    async with mcp_client() as client:
        emit_error(fake_ib, 321, READ_ONLY_TEXT)  # an earlier request was refused
        fake_ib.reqCompletedOrdersAsync.reset_mock()
        mine = await client.call_tool("get_open_orders", {"include_other_clients": False})
        completed = await client.call_tool("get_completed_orders", {})
    orders = OpenOrderList.model_validate(mine.structured_content)
    assert [o.order_id for o in orders.orders] == [7]
    assert orders.note is not None
    assert "read-only" in orders.note
    text = error_text(completed)
    assert "ib_api_error: IB error 321: The gateway's API is in read-only mode" in text
    assert "READ_ONLY_API=no" in text
    assert "get_executions" in text
    fake_ib.reqCompletedOrdersAsync.assert_not_called()


async def test_account_tools_refuse_accounts_outside_the_allowlist(
    mcp_client: McpClientFactory,
) -> None:
    async with mcp_client() as client:
        result = await client.call_tool("get_positions", {"account": "U1234567"})
    assert "account_not_allowed: Account U1234567 is not allowed" in error_text(result)


async def test_account_tools_when_the_gateway_is_down(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    go_offline(fake_ib)
    async with mcp_client() as client:
        result = await client.call_tool("get_account_summary", {})
    assert "not_connected:" in error_text(result)
