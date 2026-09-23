"""Order round trips against a paper gateway. Never run in CI, never against a live login.

Run with ``IB_HOST=... IB_PORT=... IB_ACCOUNT=DU... uv run pytest -m paper``. The guard
fixture stops the whole session unless every account the login manages is a paper
account (ids starting with ``D``). Orders are BUY limits far below the market
(``IB_TEST_LIMIT_PRICE``, default 1.00, on ``IB_TEST_SYMBOL``, default SPY), for one
share, and are cancelled right away; the fixture cancels anything this client left
working. If the gateway's API precautions reject far-from-market prices, set
``IB_TEST_LIMIT_PRICE`` closer to (but still well below) the market.

Beyond single orders the suite covers OCA groups, a two-leg option vertical (BAG) at a
far-from-market debit (``IB_TEST_COMBO_PRICE``, default 0.01), algo, trailing, stop and
GTD orders, the account reads over orders it placed (open orders with ``modifiable``,
completed orders, executions), and probes of the advisor and admin toolsets.

``test_global_cancel`` runs only with ``IB_TEST_GLOBAL_CANCEL=1``: ``reqGlobalCancel``
cancels **every** working order on the login, including other API clients' and manual
ones, so use it only on a dedicated paper login.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mcp import Client

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    IbGatewayMcpError,
    NotFoundError,
    RequestTimeoutError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.mcp.server import build_server
from ib_gateway_mcp.models.common import ContractSpec
from ib_gateway_mcp.models.orders import (
    AdaptiveAlgo,
    BracketSpec,
    ComboOrderLegSpec,
    ComboSpec,
    ModifySpec,
    OcaSpec,
    OrderResult,
    OrderSpec,
)

pytestmark = [
    pytest.mark.paper,
    pytest.mark.skipif(
        not (
            os.environ.get("IB_HOST") and os.environ.get("IB_PORT") and os.environ.get("IB_ACCOUNT")
        ),
        reason="set IB_HOST, IB_PORT and IB_ACCOUNT (a paper account) to run paper order tests",
    ),
]

CONNECT_WAIT = 30.0
SETTLE_WAIT = 10.0
SYMBOL = os.environ.get("IB_TEST_SYMBOL", "SPY")
LIMIT_PRICE = float(os.environ.get("IB_TEST_LIMIT_PRICE", "1.00"))
COMBO_PRICE = float(os.environ.get("IB_TEST_COMBO_PRICE", "0.01"))
GLOBAL_CANCEL = os.environ.get("IB_TEST_GLOBAL_CANCEL") == "1"
WORKING = {"PreSubmitted", "Submitted"}
CANCELLED = {"Cancelled", "ApiCancelled", "PendingCancel"}


def paper_settings() -> Settings:
    """Settings from the environment on the trading profile, live trading off, one share max."""
    return Settings(profile="trading", toolsets=None, allow_live=False, max_quantity=1)


@pytest.fixture
async def paper_gateway() -> AsyncIterator[Gateway]:
    """A connected trading gateway; aborts the session on anything but a paper login."""
    async with Gateway(paper_settings()) as gateway:
        await gateway.wait_connected(CONNECT_WAIT)
        managed = gateway.accounts.managed
        if not managed or not all(account.upper().startswith("D") for account in managed):
            pytest.exit(
                "Refusing to run order tests: the gateway login manages a non-paper account.",
                returncode=3,
            )
        assert gateway.ops.health().trading_enabled
        gateway.orders.status_wait = SETTLE_WAIT
        try:
            yield gateway
        finally:
            own = gateway.settings.ib_client_id
            for trade in gateway.ib.openTrades():
                if trade.order.clientId == own and trade.order.orderId > 0:
                    with contextlib.suppress(IbGatewayMcpError):
                        await gateway.orders.cancel(trade.order.orderId)


def far_limit(quantity: float = 1, price: float = LIMIT_PRICE, **fields: Any) -> OrderSpec:
    return OrderSpec(
        contract=ContractSpec(symbol=SYMBOL),
        action="BUY",
        quantity=quantity,
        order_type="LMT",
        limit_price=price,
        **fields,
    )


async def cancel_quietly(gateway: Gateway, *order_ids: int) -> None:
    """Cancel orders this test placed; ones already gone are fine."""
    for order_id in order_ids:
        with contextlib.suppress(IbGatewayMcpError):
            await gateway.orders.cancel(order_id)


async def test_what_if_only(paper_gateway: Gateway) -> None:
    """IBKR's what-if answers with real numbers (it fails fast with 321 on a read-only API)."""
    orders = paper_gateway.orders
    preview = await orders.preview_order(far_limit())
    assert preview.is_paper is True
    what_if = preview.what_if
    assert what_if is not None
    assert what_if.init_margin_change is not None
    assert what_if.commission is not None or what_if.min_commission is not None
    assert preview.orders[0].contract.con_id
    orders.discard(preview.token, reason="what-if only")


async def wait_for(orders_status: Any, order_id: int, done: Callable[[Any], bool]) -> Any:
    """Poll get_order_status until ``done`` holds or SETTLE_WAIT passes."""
    status = None
    for _ in range(int(SETTLE_WAIT * 2)):
        status = await orders_status(order_id=order_id)
        if done(status):
            break
        await asyncio.sleep(0.5)
    return status


async def test_far_limit_preview_submit_cancel(paper_gateway: Gateway) -> None:
    orders = paper_gateway.orders
    preview = await orders.preview_order(far_limit())
    result = await orders.submit(preview.token)
    assert result.accepted, result.messages
    assert result.status in WORKING
    assert result.order_id is not None

    status = await orders.order_status(order_id=result.order_id)
    assert status.placed_by_this_server
    assert status.filled == 0

    cancelled = await orders.cancel(result.order_id)
    assert cancelled.accepted, cancelled.messages
    assert cancelled.status in CANCELLED


async def test_bracket_submit_then_cancel(paper_gateway: Gateway) -> None:
    orders = paper_gateway.orders
    preview = await orders.preview_bracket(
        BracketSpec(
            contract=ContractSpec(symbol=SYMBOL),
            action="BUY",
            quantity=1,
            entry_price=LIMIT_PRICE,
            take_profit_price=LIMIT_PRICE * 2,
            stop_loss_price=round(LIMIT_PRICE / 2, 2),
        )
    )
    assert preview.what_if is not None
    result = await orders.submit(preview.token)
    assert result.accepted, result.messages
    assert len(result.order_ids) == 3
    parent = result.order_ids[0]
    assert all(order.parent_id == parent for order in result.orders[1:])

    await orders.cancel(parent)  # IBKR cancels the children with their parent
    for _ in range(int(SETTLE_WAIT * 2)):
        statuses = [(await orders.order_status(order_id=i)).status for i in result.order_ids]
        if all(status in CANCELLED for status in statuses):
            break
        await asyncio.sleep(0.5)
    assert all(status in CANCELLED for status in statuses)


async def test_modify_a_far_limit(paper_gateway: Gateway) -> None:
    orders = paper_gateway.orders
    placed = await orders.submit((await orders.preview_order(far_limit())).token)
    assert placed.accepted, placed.messages
    assert placed.order_id is not None
    new_price = round(LIMIT_PRICE + 0.01, 2)  # still far below the market
    preview = await orders.preview_modify(placed.order_id, ModifySpec(limit_price=new_price))
    modified = await orders.submit(preview.token)
    assert modified.accepted, modified.messages
    status = await wait_for(
        orders.order_status, placed.order_id, lambda s: s.limit_price == new_price
    )
    assert status.limit_price == new_price
    assert status.status in WORKING
    cancelled = await orders.cancel(placed.order_id)
    assert cancelled.accepted, cancelled.messages


async def test_cancel_all_of_this_client(paper_gateway: Gateway) -> None:
    orders = paper_gateway.orders
    ids = []
    for _ in range(2):
        result = await orders.submit((await orders.preview_order(far_limit())).token)
        assert result.accepted, result.messages
        assert result.order_id is not None
        ids.append(result.order_id)
    preview = await orders.preview_cancel_all()
    assert set(ids) <= {line.order_id for line in preview.orders}
    result = await orders.submit(preview.token)
    assert result.accepted, result.messages
    for order_id in ids:
        status = await wait_for(orders.order_status, order_id, lambda s: s.status in CANCELLED)
        assert status.status in CANCELLED


async def test_mcp_round_trip(paper_gateway: Gateway) -> None:
    """preview_order -> submit_order -> cancel_order through the real server (paper: no
    human confirmation)."""
    server = build_server(paper_gateway.settings, gateway=paper_gateway)
    async with Client(server) as client:
        preview = await client.call_tool(
            "preview_order",
            {
                "order": {
                    "contract": {"symbol": SYMBOL},
                    "action": "BUY",
                    "quantity": 1,
                    "order_type": "LMT",
                    "limit_price": LIMIT_PRICE,
                }
            },
        )
        assert not preview.is_error, preview.content
        assert preview.structured_content is not None
        submitted = await client.call_tool(
            "submit_order", {"token": preview.structured_content["token"]}
        )
        assert not submitted.is_error, submitted.content
        result = OrderResult.model_validate(submitted.structured_content)
        assert result.accepted, result.messages
        assert result.order_id is not None
        cancelled = await client.call_tool("cancel_order", {"order_id": result.order_id})
        assert not cancelled.is_error, cancelled.content


# --- OCA groups, combos, algos and other order types ---------------------------------------


async def test_oca_pair_shares_one_group(paper_gateway: Gateway) -> None:
    orders = paper_gateway.orders
    preview = await orders.preview_oca(
        OcaSpec(orders=[far_limit(), far_limit(price=round(LIMIT_PRICE + 0.01, 2))], oca_type=1)
    )
    assert len(preview.orders) == 2
    assert all(line.what_if is not None for line in preview.orders)
    result = await orders.submit(preview.token)
    try:
        assert result.accepted, result.messages
        assert len(result.order_ids) == 2
        groups = {order.oca_group for order in result.orders}
        assert len(groups) == 1
        assert None not in groups

        working = await paper_gateway.account.open_orders(include_other_clients=False)
        mine = {row.order_id: row for row in working.orders if row.order_id in result.order_ids}
        assert set(mine) == set(result.order_ids)
        assert all(row.modifiable for row in mine.values())
        assert {row.oca_group for row in mine.values()} == groups
    finally:
        await cancel_quietly(paper_gateway, *result.order_ids)


async def option_vertical(gateway: Gateway) -> list[ComboOrderLegSpec]:
    """Two adjacent call strikes about a month out, near the middle of the chain."""
    underlying = ContractSpec(symbol=SYMBOL)
    chains = await gateway.contracts.option_chain(underlying)
    chain = next((c for c in chains.chains if c.trading_class == SYMBOL), chains.chains[0])
    month = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y%m%d")
    expiration = next((e for e in chain.expirations if e >= month), chain.expirations[-1])
    calls = ContractSpec(
        symbol=SYMBOL,
        sec_type="OPT",
        last_trade_date_or_contract_month=expiration,
        right="C",
        trading_class=chain.trading_class,
    )
    details = await gateway.contracts.contract_details(calls, limit=200)
    strikes = sorted({row.contract.strike for row in details.contracts if row.contract.strike})
    assert len(strikes) >= 2, strikes
    middle = len(strikes) // 2
    low, high = strikes[middle - 1], strikes[middle]
    return [
        ComboOrderLegSpec(contract=calls.model_copy(update={"strike": low}), action="BUY"),
        ComboOrderLegSpec(contract=calls.model_copy(update={"strike": high}), action="SELL"),
    ]


async def test_option_vertical_combo(paper_gateway: Gateway) -> None:
    """A BUY call vertical at a far-from-market debit: what-if, submit, cancel."""
    orders = paper_gateway.orders
    legs = await option_vertical(paper_gateway)
    preview = await orders.preview_combo(
        ComboSpec(legs=legs, action="BUY", quantity=1, limit_price=COMBO_PRICE)
    )
    assert preview.what_if is not None
    [line] = preview.orders
    assert line.contract.sec_type == "BAG"
    assert len(line.contract.combo_legs) == 2
    result = await orders.submit(preview.token)
    try:
        assert result.accepted, result.messages
        assert result.status in WORKING
    finally:
        if result.order_id is not None:
            await cancel_quietly(paper_gateway, result.order_id)


async def test_adaptive_limit_what_if(paper_gateway: Gateway) -> None:
    orders = paper_gateway.orders
    preview = await orders.preview_order(
        far_limit(algo=AdaptiveAlgo(strategy="Adaptive", priority="Patient"))
    )
    assert preview.what_if is not None
    assert preview.orders[0].algo_strategy == "Adaptive"
    orders.discard(preview.token, reason="what-if only")


@pytest.mark.parametrize(
    "fields",
    [
        {"order_type": "STP", "aux_price": LIMIT_PRICE},
        {"order_type": "TRAIL", "trailing_percent": 50.0},
        {
            "order_type": "TRAIL LIMIT",
            "trailing_percent": 50.0,
            "trail_stop_price": LIMIT_PRICE,
            "limit_price_offset": 0.5,
        },
    ],
    ids=["stop", "trail", "trail-limit"],
)
async def test_stop_and_trailing_what_ifs(paper_gateway: Gateway, fields: dict[str, Any]) -> None:
    """SELL stops far below the market: IBKR prices the what-if, nothing is placed."""
    orders = paper_gateway.orders
    spec = OrderSpec.model_validate(
        {"contract": {"symbol": SYMBOL}, "action": "SELL", "quantity": 1, **fields}
    )
    preview = await orders.preview_order(spec)
    assert preview.what_if is not None
    orders.discard(preview.token, reason="what-if only")


async def test_gtd_far_limit(paper_gateway: Gateway) -> None:
    orders = paper_gateway.orders
    until = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0)
    preview = await orders.preview_order(far_limit(tif="GTD", good_till_date=until))
    assert preview.orders[0].good_till_date
    result = await orders.submit(preview.token)
    assert result.accepted, result.messages
    assert result.order_id is not None
    try:
        status = await orders.order_status(order_id=result.order_id)
        assert status.tif == "GTD"
        assert status.status in WORKING
    finally:
        await cancel_quietly(paper_gateway, result.order_id)


# --- account reads over the orders placed here --------------------------------------------


async def test_account_reads_see_the_orders_placed(paper_gateway: Gateway) -> None:
    orders, account = paper_gateway.orders, paper_gateway.account
    placed = await orders.submit((await orders.preview_order(far_limit())).token)
    assert placed.accepted, placed.messages
    assert placed.order_id is not None
    assert placed.perm_id, "IBKR assigns the perm id once the order is accepted"
    try:
        own = await account.open_orders(include_other_clients=False)
        row = next(row for row in own.orders if row.order_id == placed.order_id)
        assert row.modifiable
        assert row.limit_price == LIMIT_PRICE
        everyone = await account.open_orders()
        assert placed.perm_id in {row.perm_id for row in everyone.orders}
    finally:
        await cancel_quietly(paper_gateway, placed.order_id)

    await wait_for(orders.order_status, placed.order_id, lambda s: s.status in CANCELLED)
    completed = None
    for _ in range(int(SETTLE_WAIT * 2)):
        completed = await account.completed_orders(api_only=True)
        if placed.perm_id in {row.perm_id for row in completed.orders}:
            break
        await asyncio.sleep(0.5)
    assert completed is not None
    done = next(row for row in completed.orders if row.perm_id == placed.perm_id)
    assert done.status in CANCELLED

    executions = await account.executions()
    assert all(row.account == executions.account for row in executions.executions)
    assert placed.perm_id not in {row.perm_id for row in executions.executions}  # never filled


@pytest.mark.skipif(not GLOBAL_CANCEL, reason="set IB_TEST_GLOBAL_CANCEL=1 (dedicated paper login)")
async def test_global_cancel(paper_gateway: Gateway) -> None:
    orders = paper_gateway.orders
    if set(paper_gateway.accounts.managed) != set(paper_gateway.accounts.allowed):
        pytest.skip("global cancel needs IBKR_MCP_ACCOUNTS to cover every managed account")
    placed = await orders.submit((await orders.preview_order(far_limit())).token)
    assert placed.accepted, placed.messages
    assert placed.order_id is not None
    preview = await orders.preview_cancel_all(scope="global")
    assert placed.order_id in {line.order_id for line in preview.orders}
    result = await orders.submit(preview.token)
    assert result.accepted, result.messages
    status = await wait_for(orders.order_status, placed.order_id, lambda s: s.status in CANCELLED)
    assert status.status in CANCELLED


# --- advisor and admin toolsets (probes) --------------------------------------------------


async def test_admin_probes(paper_gateway: Gateway) -> None:
    """set_server_log_level has no acknowledgement; display groups are a TWS GUI feature."""
    level = paper_gateway.admin.set_server_log_level("error")  # TWS's default level
    assert (level.level, level.code) == ("error", 2)
    try:
        groups = await paper_gateway.admin.list_display_groups()
    except (RequestTimeoutError, NotFoundError) as exc:
        pytest.xfail(f"IB Gateway does not list display groups: {exc}")
    assert all(isinstance(group, int) for group in groups.groups)


async def test_advisor_probes_on_a_non_advisor_login(paper_gateway: Gateway) -> None:
    """A plain paper login has no FA setup: each call answers or fails with a clear code."""
    advisor = paper_gateway.advisor
    expected = {"not_found", "invalid_request", "request_timeout"}
    codes: dict[str, str] = {}
    for call in (advisor.soft_dollar_tiers, advisor.family_codes, advisor.fa_config):
        try:
            await call()
        except IbGatewayMcpError as exc:
            codes[call.__name__] = exc.code
    assert set(codes.values()) <= expected, codes
