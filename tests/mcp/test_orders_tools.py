"""The orders toolset through an in-memory MCP client, with structured output validated.

Live-account submits are driven through elicitation on both protocol eras. No
``from __future__ import annotations``: nothing here needs it, and tool-adjacent code
keeps real annotations.
"""

import logging
from typing import Any
from unittest.mock import MagicMock

import mcp_types as types
import pytest
from ib_async.util import UNSET_DOUBLE

from ib_gateway_mcp.mcp.registry import REGISTRY, Tier
from ib_gateway_mcp.models.orders import OrderPreview, OrderResult, OrderStatusOut
from ib_gateway_mcp.safety import AUDIT_LOGGER_NAME
from tests.conftest import McpClientFactory
from tests.fakes import LIVE_ACCOUNT, PAPER_ACCOUNT, option
from tests.mcp.test_confirm import Elicitor
from tests.unit.test_orders_service import OrderBook, audit_events, details_for

ORDER_TOOLS = {
    "preview_order",
    "preview_bracket_order",
    "preview_oca_group",
    "preview_combo_order",
    "preview_modify_order",
    "preview_exercise_options",
    "preview_cancel_all_orders",
    "submit_order",
    "cancel_order",
    "get_order_status",
}
WRITE_TOOLS = {"submit_order", "cancel_order"}
MODES = ["auto", "legacy"]
LMT_ORDER = {
    "contract": {"symbol": "AAPL"},
    "action": "BUY",
    "quantity": 10,
    "order_type": "LMT",
    "limit_price": 150.0,
}


@pytest.fixture
def book(fake_ib: MagicMock) -> OrderBook:
    return OrderBook(fake_ib)


def text_of(result: types.CallToolResult) -> str:
    return result.content[0].text  # type: ignore[union-attr]


def ok(result: types.CallToolResult) -> dict[str, Any]:
    assert not result.is_error, text_of(result)
    content: dict[str, Any] | None = result.structured_content
    assert content is not None
    return content


# --- registration ----------------------------------------------------------------------


def test_the_orders_toolset_is_exactly_ten_tools() -> None:
    specs = {spec.name: spec for spec in REGISTRY.specs_for({"orders"})}
    assert set(specs) == ORDER_TOOLS
    assert {name for name, spec in specs.items() if spec.tier is Tier.WRITE} == WRITE_TOOLS
    assert "list_open_orders" not in REGISTRY


async def test_order_tools_are_listed_with_schemas(mcp_client: McpClientFactory) -> None:
    async with mcp_client(profile="trading") as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= ORDER_TOOLS
    for name in ORDER_TOOLS:
        tool = tools[name]
        assert tool.description
        assert tool.output_schema is not None
        assert tool.annotations is not None
        if name in WRITE_TOOLS:
            assert tool.annotations.read_only_hint is False
            assert tool.annotations.destructive_hint is True
        else:
            assert tool.annotations.read_only_hint is True
    assert set(tools["submit_order"].input_schema["properties"]) == {"token"}
    assert tools["cancel_order"].annotations.idempotent_hint is True  # type: ignore[union-attr]
    bracket = tools["preview_bracket_order"].input_schema
    assert {"contract", "action", "quantity", "take_profit_price", "stop_loss_price"} <= set(
        bracket["required"]
    )


async def test_order_tools_are_absent_in_the_readonly_profile(
    mcp_client: McpClientFactory,
) -> None:
    async with mcp_client() as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert not names & ORDER_TOOLS


# --- preview and submit -------------------------------------------------------------------


async def test_preview_then_submit(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook
) -> None:
    async with mcp_client(profile="trading") as client:
        preview = OrderPreview.model_validate(
            ok(await client.call_tool("preview_order", {"order": LMT_ORDER}))
        )
        result = OrderResult.model_validate(
            ok(await client.call_tool("submit_order", {"token": preview.token}))
        )
        again = await client.call_tool("submit_order", {"token": preview.token})
    assert preview.summary == "BUY 10 AAPL STK LMT 150.00 DAY"
    assert preview.is_paper is True
    assert preview.what_if is not None
    assert result.accepted is True
    assert result.status == "Submitted"
    assert result.order_ids == [101]
    assert fake_ib.placeOrder.call_args.args[1].account == PAPER_ACCOUNT
    assert again.is_error
    assert "token_not_found:" in text_of(again)


async def test_preview_order_argument_errors_reach_the_model(
    mcp_client: McpClientFactory, book: OrderBook
) -> None:
    missing_price = {**LMT_ORDER, "limit_price": None}
    async with mcp_client(profile="trading") as client:
        result = await client.call_tool("preview_order", {"order": missing_price})
    assert result.is_error
    assert "LMT orders need limit_price" in text_of(result)


async def test_preview_order_limit_errors(mcp_client: McpClientFactory, book: OrderBook) -> None:
    async with mcp_client(profile="trading", max_quantity=1) as client:
        result = await client.call_tool("preview_order", {"order": LMT_ORDER})
    assert result.is_error
    assert "order_limit:" in text_of(result)


async def test_submit_unknown_token(mcp_client: McpClientFactory, book: OrderBook) -> None:
    async with mcp_client(profile="trading") as client:
        result = await client.call_tool("submit_order", {"token": "no-such-token"})
    assert result.is_error
    assert "token_not_found:" in text_of(result)


# --- live confirmation --------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_paper_submit_never_asks(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook, mode: str
) -> None:
    elicitor = Elicitor("decline")
    async with mcp_client(profile="trading", mode=mode, elicitation_callback=elicitor) as client:
        token = ok(await client.call_tool("preview_order", {"order": LMT_ORDER}))["token"]
        result = ok(await client.call_tool("submit_order", {"token": token}))
    assert result["accepted"] is True
    assert elicitor.asked == []


@pytest.mark.parametrize("mode", MODES)
async def test_live_submit_asks_the_human(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook, mode: str
) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    elicitor = Elicitor("accept", {"confirm": True})
    async with mcp_client(
        profile="trading", allow_live=True, mode=mode, elicitation_callback=elicitor
    ) as client:
        preview = ok(await client.call_tool("preview_order", {"order": LMT_ORDER}))
        result = ok(await client.call_tool("submit_order", {"token": preview["token"]}))
    assert preview["is_paper"] is False
    assert result["accepted"] is True
    assert fake_ib.placeOrder.call_args.args[1].account == LIVE_ACCOUNT
    [question] = elicitor.asked
    assert isinstance(question, types.ElicitRequestFormParams)
    assert f"LIVE ACCOUNT {LIVE_ACCOUNT} (real money)" in question.message
    assert "BUY 10 AAPL STK LMT 150.00 DAY" in question.message
    assert "Initial margin change: 1,000.00" in question.message


@pytest.mark.parametrize("mode", MODES)
async def test_live_submit_declined(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook, mode: str
) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    elicitor = Elicitor("decline")
    async with mcp_client(
        profile="trading", allow_live=True, mode=mode, elicitation_callback=elicitor
    ) as client:
        token = ok(await client.call_tool("preview_order", {"order": LMT_ORDER}))["token"]
        declined = await client.call_tool("submit_order", {"token": token})
    assert declined.is_error
    assert "confirmation_declined:" in text_of(declined)
    fake_ib.placeOrder.assert_not_called()


@pytest.mark.parametrize("mode", MODES)
async def test_live_submit_without_elicitation_fails_closed(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook, mode: str
) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    async with mcp_client(profile="trading", allow_live=True, mode=mode) as client:
        token = ok(await client.call_tool("preview_order", {"order": LMT_ORDER}))["token"]
        refused = await client.call_tool("submit_order", {"token": token})
        retried = await client.call_tool("submit_order", {"token": token})
    assert refused.is_error
    assert "confirmation_unavailable:" in text_of(refused)
    # The refused token was burned, and the retry says nothing was sent.
    assert "token_not_found:" in text_of(retried)
    assert "discarded without sending anything" in text_of(retried)
    assert "already used" not in text_of(retried)
    fake_ib.placeOrder.assert_not_called()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "answer", [("decline", None), ("cancel", None), ("accept", {"confirm": False})]
)
async def test_live_submit_refusals_burn_and_audit_the_token(
    mcp_client: McpClientFactory,
    fake_ib: MagicMock,
    book: OrderBook,
    caplog: pytest.LogCaptureFixture,
    *,
    mode: str,
    answer: tuple[str, dict[str, Any] | None],
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    elicitor = Elicitor(*answer)
    async with mcp_client(
        profile="trading", allow_live=True, mode=mode, elicitation_callback=elicitor
    ) as client:
        token = ok(await client.call_tool("preview_order", {"order": LMT_ORDER}))["token"]
        refused = await client.call_tool("submit_order", {"token": token})
        retried = await client.call_tool("submit_order", {"token": token})
    assert refused.is_error
    assert "confirmation_declined:" in text_of(refused)
    assert "discarded without sending anything" in text_of(retried)
    assert len(elicitor.asked) == 1  # the retry fails before anyone is asked again
    fake_ib.placeOrder.assert_not_called()
    [event] = [e for e in audit_events(caplog) if e["event"] == "rejected"]
    assert (event["stage"], event["kind"], event["account"]) == (
        "confirmation",
        "order",
        LIVE_ACCOUNT,
    )


@pytest.mark.parametrize("mode", MODES)
async def test_nobody_approves_a_submit_the_breaker_would_refuse(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook, mode: str
) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    elicitor = Elicitor("accept", {"confirm": True})
    async with mcp_client(
        profile="trading", allow_live=True, mode=mode, elicitation_callback=elicitor
    ) as client:
        token = ok(await client.call_tool("preview_order", {"order": LMT_ORDER}))["token"]
        gateway = mcp_client.gateway
        assert gateway is not None
        for _ in range(gateway.settings.breaker_rejects):
            gateway.safety.breaker.record_rejection("test")
        result = await client.call_tool("submit_order", {"token": token})
    assert "circuit_open:" in text_of(result)
    assert elicitor.asked == []
    fake_ib.placeOrder.assert_not_called()


@pytest.mark.parametrize("mode", MODES)
async def test_nobody_approves_a_rate_limited_submit(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook, mode: str
) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    elicitor = Elicitor("accept", {"confirm": True})
    async with mcp_client(
        profile="trading",
        allow_live=True,
        max_orders_per_minute=1,
        mode=mode,
        elicitation_callback=elicitor,
    ) as client:
        first = ok(await client.call_tool("preview_order", {"order": LMT_ORDER}))["token"]
        second = ok(await client.call_tool("preview_order", {"order": LMT_ORDER}))["token"]
        ok(await client.call_tool("submit_order", {"token": first}))
        limited = await client.call_tool("submit_order", {"token": second})
    assert "rate_limit:" in text_of(limited)
    assert len(elicitor.asked) == 1  # only the first submit was put to the human
    assert fake_ib.placeOrder.call_count == 1


@pytest.mark.parametrize("mode", MODES)
async def test_a_global_cancel_on_a_mixed_login_names_the_live_account(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook, mode: str
) -> None:
    fake_ib.managedAccounts.return_value = [PAPER_ACCOUNT, LIVE_ACCOUNT]
    book.add(order_id=84)
    book.add(order_id=85, account=LIVE_ACCOUNT)
    elicitor = Elicitor("accept", {"confirm": True})
    async with mcp_client(
        profile="trading",
        ib_account=PAPER_ACCOUNT,
        accounts_allowlist=[PAPER_ACCOUNT, LIVE_ACCOUNT],
        allow_live=True,
        allow_global_cancel=True,
        mode=mode,
        elicitation_callback=elicitor,
    ) as client:
        preview = ok(
            await client.call_tool(
                "preview_cancel_all_orders", {"account": PAPER_ACCOUNT, "scope": "global"}
            )
        )
        ok(await client.call_tool("submit_order", {"token": preview["token"]}))
    assert preview["is_paper"] is False
    [question] = elicitor.asked
    assert question.message.startswith(f"LIVE ACCOUNT {LIVE_ACCOUNT} (real money)")
    assert "CANCEL ALL 2 working orders" in question.message
    fake_ib.reqGlobalCancel.assert_called_once_with()


# --- the other previews -------------------------------------------------------------------


async def test_preview_bracket_order(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook
) -> None:
    arguments = {
        "contract": {"symbol": "AAPL"},
        "action": "BUY",
        "quantity": 10,
        "entry_price": 150.0,
        "take_profit_price": 160.0,
        "stop_loss_price": 140.0,
    }
    async with mcp_client(profile="trading") as client:
        preview = ok(await client.call_tool("preview_bracket_order", arguments))
        result = ok(await client.call_tool("submit_order", {"token": preview["token"]}))
        wrong_side = await client.call_tool(
            "preview_bracket_order", {**arguments, "stop_loss_price": 170.0}
        )
    assert preview["kind"] == "bracket"
    assert [line["role"] for line in preview["orders"]] == ["entry", "take_profit", "stop_loss"]
    assert result["order_ids"] == [101, 102, 103]
    assert [call.args[1].transmit for call in fake_ib.placeOrder.call_args_list] == [
        False,
        False,
        True,
    ]
    assert wrong_side.is_error
    assert "invalid_request: Invalid BracketSpec" in text_of(wrong_side)


async def test_order_attributes_through_the_tools(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook
) -> None:
    order = {
        **LMT_ORDER,
        "order_type": "PEG MID",
        "aux_price": 0.01,
        "all_or_none": True,
        "algo": None,
        "good_after_time": "2026-12-18T09:45:00-05:00",
        "soft_dollar_tier": {"name": "Research", "value": "R1"},
    }
    bracket = {
        "contract": {"symbol": "AAPL"},
        "action": "BUY",
        "quantity": 10,
        "entry_price": 150.0,
        "take_profit_price": 160.0,
        "stop_loss_price": 140.0,
        "model_code": "Growth",
    }
    async with mcp_client(profile="trading") as client:
        preview = OrderPreview.model_validate(
            ok(await client.call_tool("preview_order", {"order": order}))
        )
        brackets = OrderPreview.model_validate(
            ok(await client.call_tool("preview_bracket_order", bracket))
        )
    [line] = preview.orders
    assert (line.order_type, line.aux_price, line.all_or_none) == ("PEG MID", 0.01, True)
    assert line.good_after_time == "20261218-14:45:00"
    assert line.soft_dollar_tier == "Research"
    assert {item.model_code for item in brackets.orders} == {"Growth"}


async def test_preview_oca_group(mcp_client: McpClientFactory, book: OrderBook) -> None:
    orders = [LMT_ORDER, {**LMT_ORDER, "limit_price": 140.0}]
    async with mcp_client(profile="trading") as client:
        preview = ok(await client.call_tool("preview_oca_group", {"orders": orders, "oca_type": 1}))
        too_few = await client.call_tool("preview_oca_group", {"orders": orders[:1]})
    assert preview["kind"] == "oca"
    groups = {line["oca_group"] for line in preview["orders"]}
    assert len(groups) == 1
    assert too_few.is_error


async def test_preview_combo_order(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = details_for(
        option(strike=200.0, con_id=700001), option(strike=210.0, con_id=700002)
    )

    def leg(strike: float, action: str) -> dict[str, Any]:
        return {
            "contract": {
                "symbol": "AAPL",
                "sec_type": "OPT",
                "last_trade_date_or_contract_month": "20261218",
                "strike": strike,
                "right": "C",
            },
            "action": action,
        }

    async with mcp_client(profile="trading") as client:
        preview = ok(
            await client.call_tool(
                "preview_combo_order",
                {
                    "legs": [leg(200.0, "BUY"), leg(210.0, "SELL")],
                    "action": "BUY",
                    "quantity": 1,
                    "limit_price": 2.5,
                },
            )
        )
        missing_price = await client.call_tool(
            "preview_combo_order",
            {"legs": [leg(200.0, "BUY"), leg(210.0, "SELL")], "action": "BUY", "quantity": 1},
        )
    assert preview["kind"] == "combo"
    assert preview["orders"][0]["contract"]["sec_type"] == "BAG"
    assert missing_price.is_error
    assert "invalid_request:" in text_of(missing_price)


async def test_preview_modify_order(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook
) -> None:
    trade = book.add(order_id=55)
    async with mcp_client(profile="trading") as client:
        preview = ok(
            await client.call_tool(
                "preview_modify_order", {"order_id": 55, "changes": {"limit_price": 151.0}}
            )
        )
        result = ok(await client.call_tool("submit_order", {"token": preview["token"]}))
        unknown = await client.call_tool(
            "preview_modify_order", {"order_id": 99, "changes": {"limit_price": 1.0}}
        )
    assert preview["kind"] == "modify"
    assert result["kind"] == "modify"
    assert trade.order.lmtPrice == 151.0
    assert "not_found:" in text_of(unknown)


async def test_preview_exercise_options(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = details_for(option())
    arguments = {
        "contract": {
            "symbol": "AAPL",
            "sec_type": "OPT",
            "last_trade_date_or_contract_month": "20261218",
            "strike": 200,
            "right": "C",
        },
        "action": "exercise",
        "quantity": 1,
    }
    async with mcp_client(profile="trading") as client:
        preview = ok(await client.call_tool("preview_exercise_options", arguments))
        not_option = await client.call_tool(
            "preview_exercise_options", {**arguments, "contract": {"symbol": "AAPL"}}
        )
    assert preview["kind"] == "exercise"
    assert preview["summary"].startswith("EXERCISE 1 AAPL OPT")
    assert "invalid_request: Only options" in text_of(not_option)


async def test_preview_exercise_options_by_con_id(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook
) -> None:
    fake_ib.reqContractDetailsAsync.side_effect = details_for(option())
    arguments = {"contract": {"con_id": 700001}, "action": "exercise", "quantity": 1}
    async with mcp_client(profile="trading") as client:
        preview = ok(await client.call_tool("preview_exercise_options", arguments))
    assert preview["summary"] == "EXERCISE 1 AAPL OPT 20261218 200 C (x100)"


async def test_preview_cancel_all_orders(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook
) -> None:
    book.add(order_id=70)
    async with mcp_client(profile="trading") as client:
        preview = ok(await client.call_tool("preview_cancel_all_orders", {}))
        result = ok(await client.call_tool("submit_order", {"token": preview["token"]}))
        nothing = await client.call_tool("preview_cancel_all_orders", {"scope": "this_client"})
    assert preview["kind"] == "cancel_all"
    assert [line["order_id"] for line in preview["orders"]] == [70]
    assert result["status"] == "Cancelled"
    assert "not_found:" in text_of(nothing)


# --- cancel and status --------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_cancel_order(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook, mode: str
) -> None:
    trade = book.add(order_id=90)
    book.add(order_id=91, client_id=7)
    elicitor = Elicitor("decline")
    async with mcp_client(profile="trading", mode=mode, elicitation_callback=elicitor) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        result = OrderResult.model_validate(
            ok(await client.call_tool("cancel_order", {"order_id": 90}))
        )
        foreign = await client.call_tool("cancel_order", {"order_id": 91})
    assert set(tools["cancel_order"].input_schema["properties"]) == {"order_id"}
    assert result.status == "Cancelled"
    fake_ib.cancelOrder.assert_called_once_with(trade.order)
    assert "invalid_request:" in text_of(foreign)
    assert elicitor.asked == []  # paper: nobody is asked


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("action", "content"), [("accept", {"confirm": True}), ("decline", None)])
async def test_live_cancel_order_asks_the_human(
    mcp_client: McpClientFactory,
    fake_ib: MagicMock,
    book: OrderBook,
    *,
    mode: str,
    action: str,
    content: dict[str, Any] | None,
) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    trade = book.add(
        order_id=50,
        account=LIVE_ACCOUNT,
        action="SELL",
        orderType="STP",
        lmtPrice=UNSET_DOUBLE,
        auxPrice=140.0,
        parentId=49,
    )
    elicitor = Elicitor(action, content)
    async with mcp_client(
        profile="trading", allow_live=True, mode=mode, elicitation_callback=elicitor
    ) as client:
        result = await client.call_tool("cancel_order", {"order_id": 50})
    [question] = elicitor.asked
    assert "CANCEL order 50: SELL 10 AAPL STK STP stop 140.00 DAY" in question.message
    assert "attached to order 49" in question.message
    if action == "accept":
        assert ok(result)["status"] == "Cancelled"
        fake_ib.cancelOrder.assert_called_once_with(trade.order)
    else:
        assert "confirmation_declined:" in text_of(result)
        fake_ib.cancelOrder.assert_not_called()


async def test_live_cancel_without_elicitation_fails_closed(
    mcp_client: McpClientFactory, fake_ib: MagicMock, book: OrderBook
) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    book.add(order_id=51, account=LIVE_ACCOUNT)
    async with mcp_client(profile="trading", allow_live=True) as client:
        result = await client.call_tool("cancel_order", {"order_id": 51})
    assert "confirmation_unavailable:" in text_of(result)
    fake_ib.cancelOrder.assert_not_called()


async def test_get_order_status(mcp_client: McpClientFactory, book: OrderBook) -> None:
    book.add(order_id=100, perm_id=4242)
    async with mcp_client(profile="trading") as client:
        by_id = OrderStatusOut.model_validate(
            ok(await client.call_tool("get_order_status", {"order_id": 100}))
        )
        by_perm = OrderStatusOut.model_validate(
            ok(await client.call_tool("get_order_status", {"perm_id": 4242}))
        )
        neither = await client.call_tool("get_order_status", {})
        unknown = await client.call_tool("get_order_status", {"perm_id": 1})
    assert by_id.order_id == 100
    assert by_id.status == "Submitted"
    assert by_perm.order_id == 100
    assert "invalid_request:" in text_of(neither)
    assert "not_found:" in text_of(unknown)
