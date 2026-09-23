"""Domain services: the library API behind every MCP tool.

One class per domain, each a :class:`~ib_gateway_mcp.services.base.BaseService`. A
:class:`~ib_gateway_mcp.gateway.Gateway` builds them all and exposes them as attributes
(``gw.ops``, ``gw.contracts``, ``gw.orders``, ...).
"""

from ib_gateway_mcp.services.account import AccountService
from ib_gateway_mcp.services.admin import AdminService
from ib_gateway_mcp.services.advisor import AdvisorService
from ib_gateway_mcp.services.base import BaseService
from ib_gateway_mcp.services.contracts import ContractsService
from ib_gateway_mcp.services.fundamentals import FundamentalsService
from ib_gateway_mcp.services.history import HistoryService
from ib_gateway_mcp.services.market_data import MarketDataService
from ib_gateway_mcp.services.news import NewsService
from ib_gateway_mcp.services.ops import OpsService
from ib_gateway_mcp.services.options import OptionsService
from ib_gateway_mcp.services.orders import OrdersService
from ib_gateway_mcp.services.scanners import ScannersService

__all__ = [
    "AccountService",
    "AdminService",
    "AdvisorService",
    "BaseService",
    "ContractsService",
    "FundamentalsService",
    "HistoryService",
    "MarketDataService",
    "NewsService",
    "OpsService",
    "OptionsService",
    "OrdersService",
    "ScannersService",
]
