"""Build and run the MCP server.

:func:`build_server` assembles an ``MCPServer`` from :class:`~ib_gateway_mcp.config.Settings`:
the enabled toolsets' tools, instructions for the model, a lifespan that owns the
:class:`~ib_gateway_mcp.gateway.Gateway`, bearer auth for HTTP, and two unauthenticated
probes:

* ``/healthz`` (liveness): 200 whenever the server answers. The server is designed to
  keep running while the gateway is down (weekly re-login, 2FA), so a gateway outage
  is not a reason to restart it.
* ``/readyz`` (readiness): 200 only while the gateway connection is up, else 503.

Both return only ``{"state": ..., "ready": ...}``: they are unauthenticated, so they
must not say more than a load balancer needs (not the endpoint, client id, raw gateway
messages, or whether the login is live and trading). The full report is the
authenticated ``get_health`` tool.

The lifespan starts the gateway connection but waits at most
:data:`STARTUP_CONNECT_WAIT` seconds for it, so a half-up gateway (accepting sockets,
never finishing the handshake) cannot delay the MCP handshake or the HTTP port.

:func:`run` serves it over stdio or streamable HTTP (path ``/mcp``).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import ib_gateway_mcp.mcp.tools  # noqa: F401  (importing the tool modules registers their tools)
from ib_gateway_mcp import __version__
from ib_gateway_mcp.config import Settings, enabled_toolsets
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.mcp.auth import (
    StaticTokenVerifier,
    auth_settings,
    require_http_auth,
    transport_security,
)
from ib_gateway_mcp.mcp.context import ServerState
from ib_gateway_mcp.mcp.registry import REGISTRY, ToolRegistry, ToolSpec
from ib_gateway_mcp.models.ops import ConnectionState
from ib_gateway_mcp.startup import check_audit_log, limits_summary

__all__ = [
    "HEALTH_PATH",
    "INSTRUCTIONS_MAX_CHARS",
    "MCP_PATH",
    "READY_PATH",
    "SERVER_NAME",
    "STARTUP_CONNECT_WAIT",
    "build_instructions",
    "build_server",
    "run",
]

logger = logging.getLogger(__name__)

SERVER_NAME = "ib-gateway-mcp"
MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
READY_PATH = "/readyz"

STARTUP_CONNECT_WAIT = 1.0
"""Seconds the lifespan waits for the first gateway connection attempt before serving."""


class _GatewayHolder:
    """Shares the lifespan's gateway with routes that run outside MCP requests."""

    def __init__(self, gateway: Gateway | None = None) -> None:
        self.gateway = gateway


INSTRUCTIONS_MAX_CHARS = 2048
"""Longest instructions some clients show in full (Claude Code cuts them here)."""

_LIMITS_CUT = " ... (cut short; a preview names any limit an order breaks)."


def build_instructions(settings: Settings, toolsets: frozenset[str]) -> str:
    """Summarize the enabled toolsets and the safety model for the model.

    The first line stands alone (some clients show only it, cut to 250 characters), and
    the whole text stays within :data:`INSTRUCTIONS_MAX_CHARS`: the order limits come last
    and are cut short when long allowlists would pass it.
    """
    parts = [
        "Interactive Brokers (IBKR) tools over an IB Gateway (TWS API) connection. If a call "
        "fails with not_connected or request_timeout, call get_health to see whether the "
        "gateway is down and what is known about why.",
        f"Enabled toolsets: {', '.join(sorted(toolsets))}.",
        "get_health's state says whether the connection works; not_accepting alone doesn't "
        "say why. Relay its hint; unless the hint or login_state names a cause, report "
        "last_disconnect_at and don't guess one such as 2FA. Never restart the gateway or "
        "this server yourself: the operator does.",
        "Account-scoped tools take an optional account; without one they use the default "
        "account. list_accounts shows which accounts are allowed.",
        "Prices and sizes that IBKR does not report are null, never zero.",
    ]
    if "market_data" in toolsets:
        parts.append(
            "subscribe_* tools return a subscription_id: read it with get_subscription_data, "
            "stop it with unsubscribe, see all with list_subscriptions. A subscription not "
            f"read for {settings.subscription_idle_ttl:g} seconds is cancelled. The market "
            "data type (set_market_data_type) applies to every client of this server and is "
            "kept across gateway reconnects."
        )
    if "orders" not in toolsets:
        parts.append("Order tools are not enabled on this server.")
        return "\n".join(parts)
    check = "get_order_status or get_open_orders" if "account" in toolsets else "get_order_status"
    parts.append(
        "Orders are two-step: a preview_* tool runs a what-if check and returns a token; "
        "submit_order(token) places exactly that order. Tokens are single-use and expire "
        f"after {settings.token_ttl} seconds. submit_order reports the first status only; "
        "confirm fills with get_order_status. If a submit or cancel fails or times out, "
        f"check {check} before trying again: the order may have reached IBKR. Live orders "
        "also need IBKR_MCP_ALLOW_LIVE and, while IBKR_MCP_LIVE_CONFIRM is on, a human "
        "confirmation that the client shows; you cannot confirm on the user's behalf."
    )
    text = "\n".join(parts)
    limits = limits_summary(settings)
    room = INSTRUCTIONS_MAX_CHARS - len(text) - 1  # the newline before the limits
    if len(limits) > room:  # cut at a word boundary, then say so
        head = limits[: room - len(_LIMITS_CUT)].rsplit(" ", 1)[0]
        limits = head.rstrip(" ,;.") + _LIMITS_CUT
    return f"{text}\n{limits}"


@functools.cache
def _sdk_tool(spec: ToolSpec) -> Tool:
    """The SDK's tool for ``spec``, built once per process.

    Building one derives the argument model and JSON schemas from the signature, which
    is most of the time a server build takes. A tool holds only the function and those
    schemas (the gateway comes from the request context), so servers can share it.
    """
    return Tool.from_function(
        spec.fn, name=spec.name, title=spec.title, annotations=spec.annotations
    )


def build_server(
    settings: Settings | None = None,
    gateway: Gateway | None = None,
    *,
    registry: ToolRegistry | None = None,
) -> MCPServer[ServerState]:
    """Build the MCP server.

    Args:
        settings: Configuration; read from the environment when omitted.
        gateway: An already-started gateway to use (tests, embedding). When omitted, the
            server's lifespan creates one, starts it, and stops it on shutdown.
        registry: Where to find tools; defaults to the ``@ib_tool`` registry.

    Raises:
        ConfigurationError: Unknown toolsets, HTTP without a token (see
            :func:`~ib_gateway_mcp.mcp.auth.require_http_auth`), or write tools with an
            audit file that cannot be written.
    """
    settings = settings if settings is not None else Settings()
    toolsets = enabled_toolsets(settings)
    require_http_auth(settings)
    if gateway is None:
        check_audit_log(settings)  # fail at start-up, not at the first order
    registry = registry if registry is not None else REGISTRY

    holder = _GatewayHolder(gateway)
    specs = registry.specs_for(toolsets)
    server = MCPServer[ServerState](
        name=SERVER_NAME,
        title="Interactive Brokers gateway",
        version=__version__,
        instructions=build_instructions(settings, toolsets),
        tools=[_sdk_tool(spec) for spec in specs],
        lifespan=_lifespan(settings, holder),
        log_level=settings.log_level,
        **_auth_kwargs(settings),
    )
    server.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)(
        _health_endpoint(holder, ready_only=False)
    )
    server.custom_route(READY_PATH, methods=["GET"], include_in_schema=False)(
        _health_endpoint(holder, ready_only=True)
    )
    logger.info(
        "MCP server built with %d tools from toolsets: %s",
        len(specs),
        ", ".join(sorted(toolsets)),
    )
    return server


def _auth_kwargs(settings: Settings) -> dict[str, Any]:
    if settings.transport != "http" or settings.auth_token is None:
        return {}
    return {
        "token_verifier": StaticTokenVerifier(settings.auth_token),
        "auth": auth_settings(),
    }


Lifespan = Callable[[MCPServer[ServerState]], AbstractAsyncContextManager[ServerState]]


def _lifespan(settings: Settings, holder: _GatewayHolder) -> Lifespan:
    injected = holder.gateway

    @asynccontextmanager
    async def lifespan(_server: MCPServer[ServerState]) -> AsyncIterator[ServerState]:
        if injected is not None:
            holder.gateway = injected
            yield ServerState(gateway=injected)
            return
        gateway = Gateway(settings)
        holder.gateway = gateway
        try:
            await gateway.start(wait=STARTUP_CONNECT_WAIT)
            yield ServerState(gateway=gateway)
        finally:
            holder.gateway = None
            await gateway.stop()

    return lifespan


def _health_endpoint(
    holder: _GatewayHolder, *, ready_only: bool
) -> Callable[[Request], Awaitable[Response]]:
    """Build ``/healthz`` (``ready_only=False``) or ``/readyz`` (``ready_only=True``).

    Both return ``{"state", "ready"}`` only (see the module docstring). Liveness always
    answers 200; readiness answers 503 unless the gateway connection is up.
    """

    async def probe(_request: Request) -> Response:
        gateway = holder.gateway
        state = gateway.health().state.value if gateway is not None else "starting"
        ready = state == ConnectionState.CONNECTED.value
        status = 503 if ready_only and not ready else 200
        return JSONResponse({"state": state, "ready": ready}, status_code=status)

    return probe


def run(settings: Settings | None = None) -> None:
    """Serve over the configured transport until interrupted."""
    settings = settings if settings is not None else Settings()
    server = build_server(settings)
    if settings.transport == "http":
        logger.info(
            "Serving streamable HTTP on %s:%d%s", settings.http_host, settings.http_port, MCP_PATH
        )
        server.run(
            "streamable-http",
            host=settings.http_host,
            port=settings.http_port,
            streamable_http_path=MCP_PATH,
            transport_security=transport_security(settings),
        )
    else:
        server.run("stdio")
