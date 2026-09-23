"""Server assembly: toolset gating, annotations, error mapping, /healthz and bearer auth.

No ``from __future__ import annotations`` here: this module defines tool functions, and
the SDK needs their real annotations.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx2
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ib_gateway_mcp.config import TOOLSETS, Settings
from ib_gateway_mcp.errors import ConfigurationError, NotConnectedError
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.mcp import server as server_module
from ib_gateway_mcp.mcp.auth import (
    CLIENT_ID,
    StaticTokenVerifier,
    require_http_auth,
    transport_security,
)
from ib_gateway_mcp.mcp.context import ServerState, ToolContext, gateway_from
from ib_gateway_mcp.mcp.params import AccountArg, ContractArg, LimitArg
from ib_gateway_mcp.mcp.registry import REGISTRY, Tier, ToolRegistry, ToolSpec, ib_tool
from ib_gateway_mcp.mcp.server import (
    HEALTH_PATH,
    MCP_PATH,
    READY_PATH,
    build_instructions,
    build_server,
    run,
)
from ib_gateway_mcp.models.common import Truncatable
from tests.conftest import McpClientFactory
from tests.fakes import LIVE_ACCOUNT, PAPER_ACCOUNT, go_offline, make_fake_ib

OPS_TOOLS = {
    "get_health",
    "get_server_time",
    "get_connection_info",
    "list_accounts",
    "get_user_info",
}
SESSION_TOOLS = {"set_market_data_type", "unsubscribe"}
"""READ-tier tools that change state every client of the server shares: not read-only."""
TOKEN = "test-bearer-token-0123456789abcdef"  # at least MIN_TOKEN_LENGTH characters
BASE_URL = "http://127.0.0.1:8000"


def dummy_registry() -> ToolRegistry:
    """One tool per interesting toolset and tier."""
    registry = ToolRegistry()

    @registry.tool("ops", Tier.READ, "Dummy ops")
    async def dummy_ops(ctx: ToolContext) -> str:
        """Always on."""
        return gateway_from(ctx).health().state.value

    @registry.tool("contracts", Tier.READ, "Dummy read")
    async def dummy_read(symbol: str) -> str:
        """Echo a symbol."""
        return symbol

    @registry.tool("orders", Tier.WRITE, "Dummy order")
    async def dummy_order(ctx: ToolContext) -> str:
        """Pretend to place an order."""
        return "placed"

    @registry.tool("advisor", Tier.WRITE, "Dummy advisor", idempotent=True)
    async def dummy_advisor(ctx: ToolContext) -> str:
        """Pretend to replace FA config."""
        return "replaced"

    @registry.tool("admin", Tier.ADMIN, "Dummy admin")
    async def dummy_admin(ctx: ToolContext) -> str:
        """Pretend to change a setting."""
        return "changed"

    @registry.tool("contracts", Tier.READ, "Failing read")
    async def failing_read(kind: str) -> str:
        """Fail on purpose."""
        if kind == "ours":
            raise NotConnectedError("The gateway is down for the test.")
        raise RuntimeError("internal detail that must not leak")

    return registry


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, {"dummy_ops", "dummy_read", "failing_read"}),
        ({"profile": "trading"}, {"dummy_ops", "dummy_read", "failing_read", "dummy_order"}),
        (
            {"profile": "full"},
            {
                "dummy_ops",
                "dummy_read",
                "failing_read",
                "dummy_order",
                "dummy_advisor",
                "dummy_admin",
            },
        ),
        ({"toolsets": "orders"}, {"dummy_ops", "dummy_order"}),
        ({"profile": "full", "toolsets": "contracts"}, {"dummy_ops", "dummy_read", "failing_read"}),
    ],
)
async def test_tools_follow_profiles_and_toolsets(
    mcp_client: McpClientFactory, overrides: dict[str, Any], expected: set[str]
) -> None:
    async with mcp_client(registry=dummy_registry(), **overrides) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == expected


async def test_annotations_follow_the_tier(mcp_client: McpClientFactory) -> None:
    async with mcp_client(registry=dummy_registry(), profile="full") as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    ops = tools["dummy_ops"]
    assert ops.title == "Dummy ops"
    assert ops.annotations is not None
    assert ops.annotations.read_only_hint is True
    assert ops.annotations.open_world_hint is True
    assert ops.annotations.idempotent_hint is None  # meaningless for read-only tools
    assert ops.annotations.destructive_hint is None
    assert tools["dummy_advisor"].annotations.idempotent_hint is True  # type: ignore[union-attr]

    for name in ("dummy_order", "dummy_advisor", "dummy_admin"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is True


async def test_library_errors_reach_the_model_and_crashes_do_not(
    mcp_client: McpClientFactory,
) -> None:
    async with mcp_client(registry=dummy_registry()) as client:
        ours = await client.call_tool("failing_read", {"kind": "ours"})
        crash = await client.call_tool("failing_read", {"kind": "crash"})
        ok = await client.call_tool("dummy_ops", {})

    assert ours.is_error
    assert "not_connected: The gateway is down for the test." in ours.content[0].text  # type: ignore[union-attr]
    assert crash.is_error
    assert "must not leak" not in crash.content[0].text  # type: ignore[union-attr]
    assert not ok.is_error
    assert ok.structured_content == {"result": "connected"}


async def test_the_real_registry_is_consistent(mcp_client: McpClientFactory) -> None:
    for spec in REGISTRY.specs():
        assert spec.toolset in TOOLSETS
        assert spec.fn.__doc__, f"{spec.name} needs an LLM-facing docstring"
        if spec.name in SESSION_TOOLS:
            # Changes this server's session, not the account: no gate, not read-only.
            assert spec.tier is Tier.READ
            assert spec.annotations.read_only_hint is False
            assert spec.annotations.destructive_hint is False
            continue
        read_only = spec.tier is Tier.READ
        assert spec.annotations.read_only_hint is read_only
        assert spec.annotations.destructive_hint is (None if read_only else True)
        if read_only:
            assert spec.annotations.idempotent_hint is None
    async with mcp_client() as client:
        tools = (await client.list_tools()).tools
    names = {tool.name for tool in tools}
    assert names >= OPS_TOOLS
    # The default (readonly) profile must never expose a tool that can change state at
    # IBKR; only session settings are not read-only.
    assert all(
        tool.annotations and (tool.annotations.read_only_hint or tool.name in SESSION_TOOLS)
        for tool in tools
    )


def test_decorator_rejects_bad_tools() -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="unknown toolset"):
        registry.tool("nope", Tier.READ, "x")

    def sync_tool() -> str:
        """Sync."""
        return ""

    with pytest.raises(TypeError, match="async"):
        registry.tool("ops", Tier.READ, "x")(sync_tool)  # type: ignore[type-var]

    async def undocumented() -> str:
        return ""

    with pytest.raises(TypeError, match="docstring"):
        registry.tool("ops", Tier.READ, "x")(undocumented)

    async def stringly(symbol: "str") -> str:
        """Uses a string annotation."""
        return symbol

    with pytest.raises(TypeError, match="string annotations"):
        registry.tool("ops", Tier.READ, "x")(stringly)

    async def first() -> str:
        """One."""
        return ""

    registry.tool("ops", Tier.READ, "x")(first)
    registry.tool("ops", Tier.READ, "x")(first)  # re-registering the same function is fine

    def make_clash() -> Any:
        async def first() -> str:
            """A different function with the same name."""
            return ""

        return first

    with pytest.raises(ValueError, match="already registered"):
        registry.tool("ops", Tier.READ, "x")(make_clash())
    assert "first" in registry
    assert len(registry) == 1

    with pytest.raises(ValueError, match="idempotent"):
        registry.tool("ops", Tier.READ, "x", idempotent=True)

    async def no_context() -> str:
        """A write tool that cannot reach the trading gate."""
        return ""

    with pytest.raises(TypeError, match="ctx: ToolContext"):
        registry.tool("orders", Tier.WRITE, "x")(no_context)


def module_level_tool(module: str, doc: str) -> Any:
    """A function that looks as if ``module`` defined it at top level."""

    async def get_order_status() -> str:
        return ""

    get_order_status.__doc__ = doc
    get_order_status.__module__ = module
    get_order_status.__qualname__ = "get_order_status"
    return get_order_status


def test_same_name_in_two_modules_is_refused() -> None:
    """The last import must not silently replace another module's tool."""
    registry = ToolRegistry()
    registry.tool("account", Tier.READ, "Order status")(
        module_level_tool("tools.account", "Read an order's status.")
    )
    with pytest.raises(ValueError, match=r"already registered by tools\.account\.get_order_status"):
        registry.tool("orders", Tier.READ, "Order status")(
            module_level_tool("tools.orders", "Read an order's status too.")
        )
    assert [(s.name, s.toolset) for s in registry.specs()] == [("get_order_status", "account")]


def gated_specs() -> list[ToolSpec]:
    """Every WRITE/ADMIN tool: the real registry's plus the dummy ones."""
    return [s for s in (*REGISTRY.specs(), *dummy_registry().specs()) if s.tier is not Tier.READ]


async def test_every_write_and_admin_tool_is_behind_the_trading_gate(gateway: Gateway) -> None:
    """The gate runs before the body: no arguments are needed to prove the refusal."""
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=ServerState(gateway)))
    specs = gated_specs()
    assert specs
    for spec in specs:
        assert spec.context_param is not None, spec.name
        with pytest.raises(ToolError, match="configuration_error: Order tools are disabled"):
            await spec.fn(**{spec.context_param: ctx})


async def test_write_tools_refuse_live_accounts_without_allow_live(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    async with mcp_client(registry=dummy_registry(), profile="full") as client:
        refused = await client.call_tool("dummy_order", {})
        read = await client.call_tool("dummy_read", {"symbol": "AAPL"})
    assert refused.is_error
    assert "live_trading_disabled:" in refused.content[0].text  # type: ignore[union-attr]
    assert not read.is_error
    async with mcp_client(registry=dummy_registry(), profile="full", allow_live=True) as client:
        placed = await client.call_tool("dummy_order", {})
    assert placed.structured_content == {"result": "placed"}


class Positions(Truncatable):
    symbols: list[str]


async def test_shared_parameter_types_describe_themselves(mcp_client: McpClientFactory) -> None:
    registry = ToolRegistry()

    @registry.tool("account", Tier.READ, "Positions")
    async def list_positions(
        ctx: ToolContext,
        contract: ContractArg,
        account: AccountArg = None,
        limit: LimitArg = None,
    ) -> Positions:
        """List positions."""
        return Positions(symbols=[contract.symbol or ""], truncated=limit == 1)

    async with mcp_client(registry=registry) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "list_positions")
        result = await client.call_tool(
            "list_positions", {"contract": {"symbol": "AAPL"}, "limit": 1}
        )
    properties = tool.input_schema["properties"]
    assert "default account" in properties["account"]["description"]
    assert "truncated" in properties["limit"]["description"]
    assert "con_id" in properties["contract"]["description"]
    assert tool.input_schema["required"] == ["contract"]
    assert result.structured_content == {"truncated": True, "symbols": ["AAPL"]}


def test_ib_tool_writes_to_the_default_registry() -> None:
    assert "get_health" in REGISTRY
    assert callable(ib_tool)


def test_instructions_describe_the_safety_model(settings_factory: Callable[..., Settings]) -> None:
    readonly = build_instructions(settings_factory(), frozenset({"ops", "contracts"}))
    assert "Enabled toolsets: contracts, ops." in readonly
    assert "Order tools are not enabled" in readonly
    assert "get_subscription_data" not in readonly
    streaming = build_instructions(
        settings_factory(subscription_idle_ttl=600), frozenset({"ops", "market_data"})
    )
    assert "read it with get_subscription_data" in streaming
    assert "600 seconds" in streaming
    trading = build_instructions(settings_factory(token_ttl=90), frozenset({"ops", "orders"}))
    assert "submit_order(token)" in trading
    assert "90 seconds" in trading
    assert "human confirmation" in trading


async def test_server_owns_a_gateway_when_none_is_injected(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = make_fake_ib()
    monkeypatch.setattr(server_module, "Gateway", lambda s: Gateway(s, ib_factory=lambda: fake))
    async with Client(build_server(settings)) as client:
        result = await client.call_tool("get_health", {})
        assert result.structured_content is not None
        assert result.structured_content["state"] == "connected"
    fake.disconnect.assert_called()
    assert not fake.isConnected()


# --- HTTP: /healthz and bearer auth -----------------------------------------------------


@asynccontextmanager
async def http_client(
    server_settings: Settings, gateway: Gateway
) -> AsyncIterator[httpx2.AsyncClient]:
    server = build_server(server_settings, gateway=gateway)
    app = server.streamable_http_app(streamable_http_path=MCP_PATH, host=server_settings.http_host)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            yield http


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def test_bearer_token_guards_mcp(
    settings_factory: Callable[..., Settings], gateway: Gateway
) -> None:
    server_settings = settings_factory(transport="http", auth_token=TOKEN)
    async with http_client(server_settings, gateway) as http:
        missing = await http.post(MCP_PATH, json=INITIALIZE, headers=MCP_HEADERS)
        wrong = await http.post(
            MCP_PATH, json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": "Bearer nope"}
        )
        good = await http.post(
            MCP_PATH, json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        )
        health = await http.get(HEALTH_PATH)
        ready = await http.get(READY_PATH)

    assert missing.status_code == 401
    assert missing.json()["error"] == "invalid_token"
    assert "Bearer" in missing.headers["www-authenticate"]
    assert wrong.status_code == 401
    assert good.status_code == 200
    assert "serverInfo" in good.text
    assert health.status_code == 200  # /healthz needs no token
    assert ready.status_code == 200  # nor does /readyz


async def test_probes_report_only_state_and_readiness(
    settings: Settings, gateway: Gateway, fake_ib: MagicMock
) -> None:
    async with http_client(settings, gateway) as http:
        live_up = await http.get(HEALTH_PATH)
        ready_up = await http.get(READY_PATH)
        go_offline(fake_ib)
        live_down = await http.get(HEALTH_PATH)
        ready_down = await http.get(READY_PATH)

    # Unauthenticated: nothing about the endpoint, the accounts, or live/paper trading.
    for up in (live_up, ready_up):
        assert up.status_code == 200
        assert up.json() == {"state": "connected", "ready": True}
    # Liveness: the server is fine while the gateway is down. Readiness: it is not ready.
    assert live_down.status_code == 200
    assert ready_down.status_code == 503
    for down in (live_down, ready_down):
        assert set(down.json()) == {"state", "ready"}
        assert down.json()["state"] in {"not_connected", "not_accepting", "connecting"}
        assert down.json()["ready"] is False
        assert PAPER_ACCOUNT not in down.text


async def test_probes_before_the_gateway_exists(settings: Settings) -> None:
    server = build_server(settings)  # no injected gateway; the lifespan is not entered
    app = server.streamable_http_app(streamable_http_path=MCP_PATH, host=settings.http_host)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        live = await http.get(HEALTH_PATH)
        ready = await http.get(READY_PATH)
    assert (live.status_code, live.json()) == (200, {"state": "starting", "ready": False})
    assert (ready.status_code, ready.json()) == (503, {"state": "starting", "ready": False})


def test_http_requires_a_token(settings_factory: Callable[..., Settings]) -> None:
    with pytest.raises(ConfigurationError, match="IBKR_MCP_AUTH_TOKEN"):
        build_server(settings_factory(transport="http"))
    with pytest.raises(ConfigurationError, match="loopback"):
        require_http_auth(
            settings_factory(transport="http", allow_no_auth=True, http_host="0.0.0.0")  # noqa: S104
        )
    # Loopback plus the explicit opt-out is allowed; stdio never needs a token.
    require_http_auth(settings_factory(transport="http", allow_no_auth=True))
    require_http_auth(settings_factory(transport="stdio"))
    build_server(settings_factory(transport="http", auth_token=TOKEN))
    with pytest.raises(ConfigurationError, match="at least 32"):
        build_server(settings_factory(transport="http", auth_token="changeme"))
    # A blank token (compose "${TOKEN}" with the variable unset) means "no token".
    with pytest.raises(ConfigurationError, match="needs a bearer token"):
        build_server(settings_factory(transport="http", auth_token=""))
    build_server(settings_factory(transport="stdio", auth_token="  "))


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.2",
        "localhost",
        "LOCALHOST",
        "ip6-localhost",
        "::1",
        "[::1]",
        "0:0:0:0:0:0:0:1",
    ],
)
async def test_loopback_without_auth_rejects_foreign_hosts(
    settings_factory: Callable[..., Settings], gateway: Gateway, host: str
) -> None:
    """DNS rebinding: a web page must not reach an unauthenticated loopback server."""
    settings = settings_factory(transport="http", allow_no_auth=True, http_host=host)
    server = build_server(settings, gateway=gateway)
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        host=settings.http_host,
        transport_security=transport_security(settings),
    )
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            rebound = await http.post(
                MCP_PATH,
                json=INITIALIZE,
                headers={**MCP_HEADERS, "Host": "attacker.example:8000"},
            )
            foreign_origin = await http.post(
                MCP_PATH,
                json=INITIALIZE,
                headers={**MCP_HEADERS, "Origin": "http://attacker.example:8000"},
            )
            local = await http.post(MCP_PATH, json=INITIALIZE, headers=MCP_HEADERS)
    assert rebound.status_code == 421
    assert foreign_origin.status_code == 403
    assert local.status_code == 200


def test_transport_security_only_for_loopback(settings_factory: Callable[..., Settings]) -> None:
    public = settings_factory(transport="http", auth_token=TOKEN, http_host="0.0.0.0")  # noqa: S104
    assert transport_security(public) is None
    loopback = transport_security(settings_factory(http_host="0:0:0:0:0:0:0:1"))
    assert loopback is not None
    assert "[0:0:0:0:0:0:0:1]:*" in loopback.allowed_hosts
    assert "http://localhost:*" in loopback.allowed_origins


def test_run_passes_transport_security(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        MCPServer, "run", lambda _self, transport, **kwargs: calls.append((transport, kwargs))
    )
    run(settings_factory(transport="http", allow_no_auth=True, http_host="127.0.0.2"))
    ((transport, kwargs),) = calls
    assert transport == "streamable-http"
    assert kwargs["streamable_http_path"] == MCP_PATH
    assert kwargs["transport_security"].enable_dns_rebinding_protection is True
    assert "127.0.0.2:*" in kwargs["transport_security"].allowed_hosts


async def test_static_token_verifier() -> None:
    verifier = StaticTokenVerifier(TOKEN)
    access = await verifier.verify_token(TOKEN)
    assert access is not None
    assert access.client_id == CLIENT_ID
    assert await verifier.verify_token("nope") is None
    assert await verifier.verify_token("") is None
    with pytest.raises(ValueError, match="empty"):
        StaticTokenVerifier("")
