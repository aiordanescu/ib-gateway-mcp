"""Tool modules, one per toolset. Importing this package registers every tool.

Each module declares its tools with :func:`~ib_gateway_mcp.mcp.registry.ib_tool`;
:func:`~ib_gateway_mcp.mcp.server.build_server` exposes only the enabled toolsets.
Do not use ``from __future__ import annotations`` in these modules: the SDK builds
tool schemas from the live signatures.
"""

from ib_gateway_mcp.mcp.tools import (
    account,
    admin,
    advisor,
    contracts,
    fundamentals,
    history,
    market_data,
    news,
    ops,
    options,
    orders,
    scanners,
)

__all__ = [
    "account",
    "admin",
    "advisor",
    "contracts",
    "fundamentals",
    "history",
    "market_data",
    "news",
    "ops",
    "options",
    "orders",
    "scanners",
]
