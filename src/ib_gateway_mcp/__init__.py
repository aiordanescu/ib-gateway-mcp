"""ib-gateway-mcp: an MCP server and Python library for the Interactive Brokers TWS API.

The library core (:class:`Gateway`, the services, models and safety rails) has no
runtime dependency on MCP; :mod:`ib_gateway_mcp.mcp` is a thin layer on top of it.
Every input and output model is importable from :mod:`ib_gateway_mcp.models`, every
error from :mod:`ib_gateway_mcp.errors`.

Error messages are written for the MCP tools and name them where they point to a next
step (for example "check get_health"); ``docs/tools.md`` maps each tool to the service
method behind it (``gw.ops.health_report()`` for ``get_health``).
"""

from ib_gateway_mcp._version import __version__
from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import IbGatewayMcpError
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.common import ContractSpec

__all__ = ["ContractSpec", "Gateway", "IbGatewayMcpError", "Settings", "__version__"]
