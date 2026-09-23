"""How tools reach the :class:`~ib_gateway_mcp.gateway.Gateway`.

The server's lifespan yields a :class:`ServerState`; the SDK hands it to every request
as ``ctx.request_context.lifespan_context``. Tools annotate their context parameter as
:data:`ToolContext` and call :func:`gateway_from`::

    async def get_server_time(ctx: ToolContext) -> ServerTime:
        return await gateway_from(ctx).ops.server_time()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import Context

from ib_gateway_mcp.errors import IbGatewayMcpError
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.safety import AuditEvent

__all__ = ["ServerState", "ToolContext", "gateway_from", "require_trading"]


@dataclass(frozen=True)
class ServerState:
    """What the server lifespan shares with every request."""

    gateway: Gateway


ToolContext = Context[ServerState, Any]
"""The context type tools declare; the SDK injects it and hides it from the schema."""


def gateway_from(ctx: Context[Any, Any]) -> Gateway:
    """Return the gateway for this request.

    Raises:
        RuntimeError: Called outside a request, or on a server not built by
            :func:`~ib_gateway_mcp.mcp.server.build_server`.
    """
    state = ctx.request_context.lifespan_context
    if not isinstance(state, ServerState):
        raise RuntimeError("the MCP server lifespan did not provide a Gateway (use build_server)")
    return state.gateway


def require_trading(gateway: Gateway, tool: str) -> None:
    """Run the trading gate for ``tool``; audit a refusal before raising it.

    The audit trail then shows attempted writes while trading was off, or on a live
    account without ``IBKR_MCP_ALLOW_LIVE``, not only the ones that got through.

    Raises:
        NotConnectedError, ConfigurationError, LiveTradingDisabledError: As
            ``ConnectionManager.require_trading``.
    """
    try:
        gateway.connection.require_trading()
    except IbGatewayMcpError as exc:
        gateway.safety.audit.record(
            AuditEvent.REJECTED, stage="gate", tool=tool, code=exc.code, reason=str(exc)
        )
        raise
