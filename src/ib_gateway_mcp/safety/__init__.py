"""Safety rails for order tools: preview tokens, order limits, throttling, audit.

The orders service composes these:

* :class:`PreviewStore` binds a previewed order to a single-use, expiring token.
* :class:`OrderPolicy` enforces symbol/sec-type allowlists and size limits on
  an :class:`OrderSummary`, at preview and again at submit.
* :class:`RateLimiter` and :class:`CircuitBreaker` throttle submission.
* :class:`AuditLog` records every order action as redacted JSONL.
* :class:`SafetyRails` bundles one of each, built from settings; a
  :class:`~ib_gateway_mcp.gateway.Gateway` exposes it as ``gw.safety``.

Nothing here talks to the gateway or depends on the MCP layer.
"""

from ib_gateway_mcp.safety.audit import AUDIT_LOGGER_NAME, AuditEvent, AuditLog
from ib_gateway_mcp.safety.policy import (
    LegSummary,
    NotionalEstimate,
    OrderPolicy,
    OrderSummary,
    estimate_notional,
)
from ib_gateway_mcp.safety.rails import SafetyRails, SafetySettings
from ib_gateway_mcp.safety.ratelimit import CircuitBreaker, RateLimiter
from ib_gateway_mcp.safety.tokens import PreviewRecord, PreviewStore, PreviewToken

__all__ = [
    "AUDIT_LOGGER_NAME",
    "AuditEvent",
    "AuditLog",
    "CircuitBreaker",
    "LegSummary",
    "NotionalEstimate",
    "OrderPolicy",
    "OrderSummary",
    "PreviewRecord",
    "PreviewStore",
    "PreviewToken",
    "RateLimiter",
    "SafetyRails",
    "SafetySettings",
    "estimate_notional",
]
