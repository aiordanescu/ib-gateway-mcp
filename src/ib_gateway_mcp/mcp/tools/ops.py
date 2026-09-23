"""Operational tools: gateway health, server time, connection details, accounts, user info.

This module is the pattern every toolset follows: each tool gets the gateway from the
context, calls exactly one service method, and returns its pydantic model. The
docstring is what the model reads, so it says what the tool does, when to use it, and
what the fields mean. Tools that take arguments use the shared parameter types in
:mod:`ib_gateway_mcp.mcp.params` (``AccountArg``, ``LimitArg``, ``ContractArg``), so
every toolset describes the same inputs the same way; see the example in
:mod:`ib_gateway_mcp.mcp.registry`.
"""

from typing import Annotated

from pydantic import Field

from ib_gateway_mcp.mcp.context import ToolContext, gateway_from
from ib_gateway_mcp.mcp.registry import Tier, ib_tool
from ib_gateway_mcp.models.ops import (
    AccountList,
    ConnectionInfo,
    HealthReport,
    ServerTime,
    UserInfo,
)


@ib_tool("ops", Tier.READ, "Gateway health")
async def get_health(
    ctx: ToolContext,
    probe: Annotated[
        bool,
        Field(
            description=(
                "Also send one request to the gateway (its clock) to prove the connection "
                "answers right now; the outcome is in `probe`. Takes up to IB_REQUEST_TIMEOUT."
            )
        ),
    ] = False,
) -> HealthReport:
    """Report whether the Interactive Brokers gateway connection is usable, and why not.

    Call this first when another tool fails with not_connected or times out. It never
    fails itself. `state` is one of:
    - connected: everything works.
    - connecting: a connection attempt is in progress.
    - not_accepting: the gateway refused or ignored the connection (it is down, logged out,
      or waiting for the user to approve 2FA). Retries run in the background.
    - connectivity_lost: the gateway is up but cut off from IBKR's servers; usually heals.
    - not_connected: stopped, or the connection dropped and a retry is pending.
    `hint` explains what to do. `trading_enabled` says whether the trading gate is open
    (order tools also need `circuit_open` false: after repeated IBKR rejections the
    circuit breaker halts order submits until a human resets it). `api_read_only` means
    the gateway's own settings reject orders. `is_paper` is true when the login only has
    paper accounts. `market_data_type` is the data type requested for this session
    (set_market_data_type changes it), and `subscriptions_used`/`subscriptions_max` show
    how many streams are open. Pass probe=true to test the connection with a real
    request (the state alone can lag behind a stalled socket).
    """
    return await gateway_from(ctx).ops.health_report(probe=probe)


@ib_tool("ops", Tier.READ, "Gateway server time")
async def get_server_time(ctx: ToolContext) -> ServerTime:
    """Return the gateway's current time and how far this server's clock is from it.

    Useful as a cheap round-trip check that the gateway answers requests, and before
    time-sensitive requests (historical data end times, order good-till times). IBKR
    reports whole seconds, so a skew under a second or two is normal.
    """
    return await gateway_from(ctx).ops.server_time()


@ib_tool("ops", Tier.READ, "Connection details")
async def get_connection_info(ctx: ToolContext) -> ConnectionInfo:
    """Return technical details of the API session: endpoint, client id, API versions.

    Includes the negotiated server version, the API version range this client speaks,
    whether open and completed orders were synced (`orders_synced`), when the session
    started, and traffic counters. Works when disconnected too (the session fields are
    then null).
    """
    return gateway_from(ctx).ops.connection_info()


@ib_tool("ops", Tier.READ, "Accounts in scope")
async def list_accounts(ctx: ToolContext) -> AccountList:
    """List the IBKR accounts this server may use, and which one is the default.

    Account-scoped tools use `default_account` when called without an account. If it is
    null, the login manages several accounts and you must pass one explicitly. Paper
    accounts start with D. Accounts outside the server's allowlist are only counted
    (`other_managed_accounts`), never named, and cannot be used.
    """
    return gateway_from(ctx).ops.list_accounts()


@ib_tool("ops", Tier.READ, "User info")
async def get_user_info(ctx: ToolContext) -> UserInfo:
    """Return details about the logged-in IBKR user: the white-branding id, if any.

    The id identifies an introducing broker's white-labelled platform; it is empty for
    most direct IBKR clients.
    """
    return await gateway_from(ctx).ops.user_info()
