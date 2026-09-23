"""Read-only probes against a real gateway. Never run in CI.

Run with ``IB_HOST=... IB_PORT=... uv run pytest -m live_readonly``. The tests use the
``readonly`` profile, so no order, advisor or admin tool exists and the trading gate
refuses every write; no code path here places, changes or cancels an order.
(``connectAsync(readonly=True)`` itself only skips the startup order sync; it is not
what keeps orders out.) One gateway session serves the whole module.

Each read toolset gets at least one probe through the library, and a few go through
the MCP server. Questions only a real gateway can answer are answered here, as an
assertion or as an xfail that states the answer:

* ``reqAllOpenOrders`` / ``reqOpenOrders`` on a read-only API (321 or not), and that a
  refusal fails fast with a clear error instead of waiting out the request timeout:
  ``test_open_orders_*``;
* how far back executions and completed orders reach: ``test_account_history_windows``
  (recorded as test properties, visible with ``--junitxml``); a read-only API refuses
  completed orders, which must fail fast and clearly;
* whether P&L needs the gateway's daily-P&L setting: ``test_pnl``;
* whether display groups answer on IB Gateway: ``test_display_groups_on_gateway``;
* bonds with a fractional coupon (the float coupon shim): ``test_bond_coupon_shim``.

Environment (all optional): ``IB_TEST_SYMBOL`` (default SPY, an optionable stock or
ETF), ``IB_TEST_STOCK`` (default AAPL, for fundamentals), ``IB_TEST_MARKET_DATA_TYPE``
(default ``delayed``: IBKR then sends live data where the login has it and delayed
data otherwise), ``IB_TEST_BOND_CUSIP`` (a bond with a fractional coupon) or
``IB_TEST_BOND_ISSUER`` (default IBM) for the bond probe.
"""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from mcp import Client
from mcp_types import CallToolResult

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    IbApiError,
    NotFoundError,
    RequestTimeoutError,
    SubscriptionNotFoundError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.mcp.server import build_server
from ib_gateway_mcp.models.account import AccountSummary
from ib_gateway_mcp.models.common import ContractOut, ContractSpec, SubscriptionOut
from ib_gateway_mcp.models.contracts import OptionChainList
from ib_gateway_mcp.models.history import BarList
from ib_gateway_mcp.models.market_data import QuoteList
from ib_gateway_mcp.models.ops import AccountList, ConnectionState, HealthReport, ServerTime
from ib_gateway_mcp.models.scanners import ScannerSpec

pytestmark = [
    pytest.mark.live_readonly,
    pytest.mark.skipif(
        not (os.environ.get("IB_HOST") and os.environ.get("IB_PORT")),
        reason="set IB_HOST and IB_PORT to run live read-only probes",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

CONNECT_WAIT = 30.0
STREAM_WAIT = 15.0
READ_ONLY_FAST = 10.0
"""Seconds within which a read-only API's refusal must surface (the request timeout is 30)."""
SYMBOL = os.environ.get("IB_TEST_SYMBOL", "SPY")
STOCK = os.environ.get("IB_TEST_STOCK", "AAPL")
MARKET_DATA_TYPE = os.environ.get("IB_TEST_MARKET_DATA_TYPE", "delayed")
UNDERLYING = ContractSpec(symbol=SYMBOL)
RecordProperty = Callable[[str, object], None]
NO_MARKET_DATA = frozenset({354, 10089, 10168})
"""IBKR's refusals of an instrument's market data for want of a subscription. IB Gateway
10.45 can answer 10089 even with delayed data selected, and then sends nothing."""


def readonly_settings() -> Settings:
    """Settings from the environment, forced onto the read-only profile."""
    return Settings(profile="readonly", toolsets=None, allow_live=False)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def gw() -> AsyncIterator[Gateway]:
    """One read-only gateway session for the module, on the test market data type."""
    settings = readonly_settings()
    assert not settings.needs_write_access
    async with Gateway(settings) as gateway:
        await gateway.wait_connected(CONNECT_WAIT)
        gateway.market_data.set_market_data_type(MARKET_DATA_TYPE)  # type: ignore[arg-type]
        yield gateway


def a_price(*values: float | None) -> float | None:
    """The first usable (positive, finite) price."""
    return next((v for v in values if v is not None and math.isfinite(v) and v > 0), None)


def xfail_without_market_data(gw: Gateway, exc: IbApiError) -> None:
    """xfail when IBKR refuses the test symbol's market data to this login."""
    if exc.error_code in NO_MARKET_DATA:
        data_type = gw.ops.health().market_data_type
        pytest.xfail(
            f"IBKR refuses {SYMBOL} market data on this login with {data_type} data "
            f"selected (a market data subscription is missing): {exc}"
        )


async def underlying_price(gw: Gateway) -> float:
    """A current (or last known) price of the test symbol: a quote, else the last bar."""
    try:
        quotes = await gw.market_data.quotes([UNDERLYING])
    except IbApiError as exc:
        if exc.error_code not in NO_MARKET_DATA:
            raise
        quotes = QuoteList(quotes=[])
    if quotes.quotes:
        quote = quotes.quotes[0]
        price = a_price(quote.last, quote.bid and quote.ask and (quote.bid + quote.ask) / 2)
        price = price or a_price(quote.close)
        if price is not None:
            return price
    bars = await gw.history.historical_bars(UNDERLYING, bar_size="1 day", duration="5 D")
    price = a_price(bars.bars[-1].close)
    assert price is not None, "no price for the test symbol"
    return price


# --- ops ------------------------------------------------------------------------------------


async def test_library_read_only_probes(gw: Gateway) -> None:
    health = gw.ops.health()
    assert health.state is ConnectionState.CONNECTED
    assert health.orders_synced is False
    assert health.trading_enabled is False
    assert health.market_data_type == MARKET_DATA_TYPE

    probed = await gw.ops.health_report(probe=True)
    assert probed.probe is not None
    assert probed.probe.ok, probed.probe.error

    server_time = await gw.ops.server_time()
    assert abs(server_time.skew_seconds) < 120

    accounts = gw.ops.list_accounts()
    assert accounts.accounts, "the login should expose at least one allowed account"


# --- contracts -----------------------------------------------------------------------------


async def test_contracts(gw: Gateway) -> None:
    contracts = gw.contracts
    qualified = await contracts.qualify_contract(UNDERLYING)
    assert qualified.con_id
    assert qualified.symbol == SYMBOL

    details = await contracts.contract_details(UNDERLYING, limit=5)
    assert details.contracts[0].contract.con_id == qualified.con_id

    found = await contracts.search_symbols(SYMBOL)
    assert any(match.contract.symbol == SYMBOL for match in found.matches)

    chains = await contracts.option_chain(UNDERLYING)
    chain = next((c for c in chains.chains if c.trading_class == SYMBOL), chains.chains[0])
    assert chain.expirations
    assert chain.strikes


# --- market data -----------------------------------------------------------------------------


async def test_quotes(gw: Gateway) -> None:
    try:
        result = await gw.market_data.quotes([UNDERLYING])
    except IbApiError as exc:
        xfail_without_market_data(gw, exc)
        raise
    assert not result.errors, result.errors
    [quote] = result.quotes
    assert quote.contract.symbol == SYMBOL
    assert a_price(quote.bid, quote.ask, quote.last, quote.close) is not None, quote
    assert quote.market_data_type is not None


async def test_quote_stream_round_trip(gw: Gateway) -> None:
    market_data = gw.market_data
    try:
        sub = await market_data.subscribe_quotes(UNDERLYING, ["mark_price"])
    except IbApiError as exc:
        xfail_without_market_data(gw, exc)
        raise
    try:
        assert sub.kind == "quotes"
        assert sub.contract is not None
        deadline = asyncio.get_running_loop().time() + STREAM_WAIT
        data: dict[str, Any] = {}
        while asyncio.get_running_loop().time() < deadline:
            data = market_data.subscription_data(sub.subscription_id).data
            quote = data["quote"]
            if data["updates"] or a_price(quote["bid"], quote["last"], quote["close"]):
                break
            await asyncio.sleep(0.5)
        assert data["active"] is True, data.get("error")
        assert data["updates"] or a_price(data["quote"]["close"]), data
        listing = market_data.list_subscriptions()
        assert sub.subscription_id in {entry.subscription_id for entry in listing.subscriptions}
    finally:
        await market_data.unsubscribe(sub.subscription_id)
    with pytest.raises(SubscriptionNotFoundError):
        market_data.subscription_data(sub.subscription_id)


# --- history ----------------------------------------------------------------------------------


async def test_history(gw: Gateway) -> None:
    history = gw.history
    bars = await history.historical_bars(UNDERLYING, bar_size="1 hour", duration="2 D")
    assert bars.bars
    assert all(bar.close is not None for bar in bars.bars)

    head = await history.head_timestamp(UNDERLYING)
    assert head.earliest < datetime.now(UTC)

    schedule = await history.trading_schedule(UNDERLYING, num_days=3)
    assert schedule.time_zone
    assert schedule.sessions


# --- scanners ---------------------------------------------------------------------------------


async def test_scanners(gw: Gateway) -> None:
    catalog = await gw.scanners.parameters("scan_codes", query="MOST_ACTIVE", limit=5)
    assert any(item.code == "MOST_ACTIVE" for item in catalog.scan_codes)

    result = await gw.scanners.run_scanner(ScannerSpec(scan_code="MOST_ACTIVE", rows=5))
    assert len(result.rows) <= 5
    assert all(row.contract.con_id for row in result.rows)


# --- news ---------------------------------------------------------------------------------


async def test_news(gw: Gateway) -> None:
    try:
        providers = await gw.news.providers()
    except NotFoundError as exc:
        pytest.xfail(f"no API news providers are enabled on this login: {exc}")
    assert all(provider.code for provider in providers.providers)
    try:
        headlines = await gw.news.historical_news(UNDERLYING, limit=5)
    except NotFoundError:
        return  # no recent headlines for the symbol: nothing more to check
    assert len(headlines.headlines) <= 5
    assert all(item.headline for item in headlines.headlines)


# --- fundamentals -----------------------------------------------------------------------------


async def test_fundamentals(gw: Gateway) -> None:
    """Both need paid data (Refinitiv, Wall Street Horizon); without it the error is clear."""
    stock = ContractSpec(symbol=STOCK)
    outcomes: dict[str, str] = {}
    try:
        report = await gw.fundamentals.fundamental_data(stock, "ReportSnapshot", max_chars=2000)
    except (NotFoundError, IbApiError, RequestTimeoutError) as exc:
        outcomes["fundamental_data"] = f"{exc.code}: {exc}"
    else:
        assert report.xml.lstrip().startswith("<")
    try:
        metadata = await gw.fundamentals.wsh_metadata(max_chars=2000)
    except (NotFoundError, IbApiError, RequestTimeoutError) as exc:
        outcomes["wsh_metadata"] = f"{exc.code}: {exc}"
    else:
        assert metadata.event_types
    if outcomes:
        pytest.xfail(f"no fundamentals or WSH data on this login: {outcomes}")


# --- account ------------------------------------------------------------------------------


async def test_account_reads(gw: Gateway) -> None:
    account = gw.account
    summary = await account.account_summary()
    assert summary.account in gw.accounts.allowed
    assert summary.net_liquidation is not None

    positions = await account.positions()
    assert all(row.account == positions.account for row in positions.positions)

    portfolio = await account.portfolio()
    assert all(item.account == portfolio.account for item in portfolio.items)


async def test_account_history_windows(gw: Gateway, record_property: RecordProperty) -> None:
    """Gateway behavior probe: how far back reqExecutions and reqCompletedOrders reach."""
    now = datetime.now(UTC)
    executions = await gw.account.executions(limit=1000)
    times = [row.time for row in executions.executions if row.time is not None]
    record_property("executions", len(executions.executions))
    if times:
        oldest = min(times)
        record_property("executions_window_days", round((now - oldest).total_seconds() / 86400, 2))
        # IBKR reports the current day, or up to 7 days with the trade-log setting.
        assert oldest > now - timedelta(days=8), oldest
        assert times == sorted(times, reverse=True)  # newest first

    completed = await read_or_read_only_refusal(
        gw, gw.account.completed_orders(limit=1000), "completed orders"
    )
    stamps = [row.completed_at for row in completed.orders if row.completed_at is not None]
    record_property("completed_orders", len(completed.orders))
    if stamps:
        record_property(
            "completed_orders_window_days",
            round((now - min(stamps)).total_seconds() / 86400, 2),
        )
        assert stamps == sorted(stamps, reverse=True)
    assert all(row.account == completed.account for row in completed.orders)


async def test_pnl(gw: Gateway) -> None:
    """Gateway behavior probe: does reqPnL answer without the gateway's daily-P&L setting?"""
    try:
        pnl = await gw.account.pnl()
    except RequestTimeoutError as exc:
        positions = len((await gw.account.positions()).positions)
        pytest.xfail(
            f"IBKR sent no P&L update in time for an account with {positions} positions "
            "(IB Gateway 10.45 can stay silent on reqPnL for a paper account without "
            "positions, even for 20 s), or the gateway's 'prepare daily P&L' API setting "
            f"may be needed ({exc})"
        )
    assert pnl.account in gw.accounts.allowed
    assert any(value is not None for value in (pnl.daily_pnl, pnl.unrealized_pnl, pnl.realized_pnl))


@pytest.mark.parametrize("include_other_clients", [True, False])
async def test_open_orders_on_this_api(
    gw: Gateway, include_other_clients: bool, record_property: RecordProperty
) -> None:
    """Gateway behavior probe: do reqAllOpenOrders / reqOpenOrders work on a read-only API?

    A read-only API refuses reqOpenOrders; this server's orders are then read from
    reqAllOpenOrders, and ``note`` says so. On a writable API there is no note.
    """
    orders = await read_or_read_only_refusal(
        gw, gw.account.open_orders(include_other_clients=include_other_clients), "open orders"
    )
    read_only = gw.ops.health().api_read_only
    record_property("api_read_only", read_only)
    assert all(order.account == orders.account for order in orders.orders)
    if include_other_clients:
        assert orders.note is None
    else:
        assert all(order.modifiable for order in orders.orders)
        if read_only:
            assert orders.note is not None
            assert "read-only" in orders.note
        else:
            assert orders.note is None


async def read_or_read_only_refusal[T](gw: Gateway, read: Awaitable[T], what: str) -> T:
    """Await an order read; a read-only API's refusal must be fast and clear (then xfail)."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        return await read
    except IbApiError as exc:
        if exc.error_code != 321:
            raise
        refusal = str(exc)
    elapsed = loop.time() - started
    assert elapsed < READ_ONLY_FAST, f"the refusal took {elapsed:.1f}s: {refusal}"
    assert gw.ops.health().api_read_only
    assert "READ_ONLY_API=no" in refusal
    assert what in refusal
    pytest.xfail(f"the read-only API refuses {what} (321, {elapsed:.1f}s): {refusal}")


# --- options ---------------------------------------------------------------------------------


async def test_option_calculators(gw: Gateway) -> None:
    price = await underlying_price(gw)
    chains = await gw.contracts.option_chain(UNDERLYING)
    chain = next((c for c in chains.chains if c.trading_class == SYMBOL), chains.chains[0])
    soon = (datetime.now(UTC) + timedelta(days=7)).strftime("%Y%m%d")
    expiration = next((e for e in chain.expirations if e >= soon), chain.expirations[-1])
    strike = min(chain.strikes, key=lambda k: abs(k - price))
    option = ContractSpec(
        symbol=SYMBOL,
        sec_type="OPT",
        last_trade_date_or_contract_month=expiration,
        strike=strike,
        right="C",
        trading_class=chain.trading_class,
    )
    try:
        theo = await gw.options.option_price(option, volatility=0.2, underlying_price=price)
    except NotFoundError:
        # Not every strike of the chain exists for every expiration.
        details = await gw.contracts.contract_details(
            option.model_copy(update={"strike": None}), limit=200
        )
        strikes = sorted(
            {row.contract.strike for row in details.contracts if row.contract.strike},
            key=lambda k: abs(k - price),
        )
        option = option.model_copy(update={"strike": strikes[0]})
        theo = await gw.options.option_price(option, volatility=0.2, underlying_price=price)
    assert theo.option_price > 0
    assert theo.greeks.delta is not None
    assert 0 < theo.greeks.delta < 1

    iv = await gw.options.implied_volatility(
        option, option_price=theo.option_price, underlying_price=price
    )
    assert iv.implied_vol == pytest.approx(0.2, abs=0.02)


# --- gateway behavior probes ----------------------------------------------------------------


async def test_display_groups_on_gateway(gw: Gateway, record_property: RecordProperty) -> None:
    """Gateway behavior probe: display groups are a TWS GUI feature; does IB Gateway answer?"""
    try:
        groups = await gw.admin.list_display_groups()
    except (RequestTimeoutError, NotFoundError) as exc:
        pytest.xfail(f"IB Gateway does not list display groups: {exc}")
    record_property("display_groups", groups.groups)
    assert all(isinstance(group, int) for group in groups.groups)


async def test_bond_coupon_shim(gw: Gateway) -> None:
    """Bonds with a fractional coupon decode (ib_async would drop them without the shim).

    IBKR finds a bond by CUSIP (or ISIN) given as the symbol; ``sec_id_type="CUSIP"``
    finds nothing on a login without CUSIP data (IB Gateway 10.45: error 200).
    """
    cusip = os.environ.get("IB_TEST_BOND_CUSIP")
    issuer = cusip or os.environ.get("IB_TEST_BOND_ISSUER", "IBM")
    spec = ContractSpec(symbol=issuer, sec_type="BOND")
    try:
        details = await gw.contracts.contract_details(spec, limit=200)
    except (NotFoundError, IbApiError) as exc:
        pytest.xfail(f"no bond details for {spec.model_dump(exclude_none=True)}: {exc}")
    bonds = [row.bond for row in details.contracts if row.bond is not None]
    assert bonds, "bond rows came back without bond terms"
    if all(bond.coupon is None and bond.maturity is None for bond in bonds):
        pytest.xfail(
            f"IBKR withholds bond reference data on this login: all {len(bonds)} {issuer} "
            "bonds arrive with no coupon, maturity, ratings or currency and an IBCID "
            "placeholder instead of the CUSIP (the login likely lacks IBKR's bond "
            "reference data / CUSIP permission), so the fractional-coupon decode cannot "
            "be observed; desc_append still names the coupon, e.g. "
            f"{bonds[0].desc_append!r}"
        )
    coupons = [bond.coupon for bond in bonds]
    if not any(coupon is not None and coupon % 1 for coupon in coupons):
        pytest.xfail(f"no fractional coupon among {len(coupons)} bonds; set IB_TEST_BOND_CUSIP")


# --- through the MCP server ------------------------------------------------------------------


async def test_mcp_read_only_probes(gw: Gateway) -> None:
    async with Client(build_server(gw.settings, gateway=gw)) as client:
        health = await client.call_tool("get_health", {"probe": True})
        server_time = await client.call_tool("get_server_time", {})
        accounts = await client.call_tool("list_accounts", {})

    report = HealthReport.model_validate(health.structured_content)
    assert report.state is ConnectionState.CONNECTED
    assert report.probe is not None
    assert report.probe.ok
    assert ServerTime.model_validate(server_time.structured_content).server_time
    assert AccountList.model_validate(accounts.structured_content).accounts


def market_data_refusal(result: CallToolResult) -> str | None:
    """The text of a tool error that is IBKR refusing market data (see NO_MARKET_DATA)."""
    text = " ".join(getattr(block, "text", "") for block in result.content)
    refused = any(f"IB error {code}:" in text for code in NO_MARKET_DATA)
    return text if result.is_error and refused else None


async def test_mcp_market_data_history_and_account(gw: Gateway) -> None:
    contract = {"symbol": SYMBOL}
    async with Client(build_server(gw.settings, gateway=gw)) as client:
        quotes = await client.call_tool("get_quotes", {"contracts": [contract]})
        bars = await client.call_tool(
            "get_historical_bars", {"contract": contract, "bar_size": "1 hour", "duration": "2 D"}
        )
        chain = await client.call_tool("get_option_chain", {"underlying": contract})
        summary = await client.call_tool("get_account_summary", {})
        sub = await client.call_tool("subscribe_quotes", {"contract": contract})
        data = stopped = None
        if not sub.is_error:
            handle = SubscriptionOut.model_validate(sub.structured_content)
            data = await client.call_tool(
                "get_subscription_data", {"subscription_id": handle.subscription_id}
            )
            stopped = await client.call_tool(
                "unsubscribe", {"subscription_id": handle.subscription_id}
            )

    for result in (bars, chain, summary):
        assert not result.is_error, result.content
    assert BarList.model_validate(bars.structured_content).bars
    assert OptionChainList.model_validate(chain.structured_content).chains
    assert AccountSummary.model_validate(summary.structured_content).account
    refusal = market_data_refusal(quotes) or market_data_refusal(sub)
    if refusal is not None:
        data_type = gw.ops.health().market_data_type
        pytest.xfail(
            f"IBKR refuses {SYMBOL} market data on this login with {data_type} data "
            f"selected (a market data subscription is missing): {refusal}"
        )
    assert not quotes.is_error, quotes.content
    assert not sub.is_error, sub.content
    assert data is not None
    assert stopped is not None
    assert not data.is_error, data.content
    assert not stopped.is_error, stopped.content
    assert QuoteList.model_validate(quotes.structured_content).quotes
    assert data.structured_content is not None
    assert data.structured_content["kind"] == "quotes"
    assert ContractOut.model_validate(data.structured_content["data"]["contract"]).con_id


async def test_mcp_errors_are_tool_errors(gw: Gateway) -> None:
    """A bad request comes back as a tool error with a code, never as a crash."""
    async with Client(build_server(gw.settings, gateway=gw)) as client:
        unknown = await client.call_tool(
            "qualify_contract", {"contract": {"symbol": "NO_SUCH_SYMBOL_XYZ"}}
        )
        orders = [tool.name for tool in (await client.list_tools()).tools]
    assert unknown.is_error
    assert "not_found" in unknown.content[0].text  # type: ignore[union-attr]
    assert "preview_order" not in orders  # the read-only profile has no order tools
