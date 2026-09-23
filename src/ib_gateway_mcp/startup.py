"""The safety configuration, checked and stated once at start-up.

:func:`log_safety_configuration` writes one INFO line with every switch that governs
writes (profile, live trading, confirmation, limits, rates, breaker, audit file) and a
WARNING for each risky combination, so an operator reading the log sees how the server
is armed. :func:`check_audit_log` refuses to start a server that may write when its
audit file cannot be written. The MCP server and :meth:`~ib_gateway_mcp.gateway.Gateway.start`
call both.
"""

from __future__ import annotations

import logging

from ib_gateway_mcp.config import Settings, enabled_toolsets
from ib_gateway_mcp.errors import ConfigurationError
from ib_gateway_mcp.safety import AuditLog, OrderPolicy
from ib_gateway_mcp.safety.rails import breaker_state_path

__all__ = [
    "check_audit_log",
    "limits_summary",
    "log_safety_configuration",
    "risky_settings",
    "safety_summary",
]

logger = logging.getLogger(__name__)


def _switch(value: bool) -> str:
    return "on" if value else "off"


def limits_summary(settings: Settings) -> str:
    """The order limits and rates, in one sentence (also part of the server instructions)."""
    policy = OrderPolicy.from_settings(settings)
    return (
        f"Order limits: {policy.describe()}. At most {settings.max_orders_per_minute} "
        f"orders and {settings.max_previews_per_minute} previews per minute; the circuit "
        f"breaker halts submission after {settings.breaker_rejects} consecutive rejections."
    )


def safety_summary(settings: Settings) -> str:
    """One line with every setting that governs writes."""
    audit = settings.audit_log
    state = breaker_state_path(audit)
    audit_text = (
        f"audit file {audit} (breaker state {state})"
        if audit
        else "audit to the log only (breaker state in memory)"
    )
    return (
        f"Safety: toolsets {', '.join(sorted(enabled_toolsets(settings)))}; "
        f"live trading {_switch(settings.allow_live)}, "
        f"live confirmation {_switch(settings.live_confirm)}, "
        f"global cancel {_switch(settings.allow_global_cancel)}, "
        f"regulatory snapshots {_switch(settings.allow_regulatory_snapshots)}; "
        f"{limits_summary(settings)} Preview tokens last {settings.token_ttl} s; {audit_text}."
    )


def risky_settings(settings: Settings) -> list[str]:
    """Combinations SECURITY.md advises against, worded for the log."""
    warnings: list[str] = []
    writes = settings.needs_write_access
    if settings.allow_live and not settings.live_confirm:
        warnings.append(
            "IBKR_MCP_ALLOW_LIVE is on and IBKR_MCP_LIVE_CONFIRM is off: live orders, "
            "cancels and FA changes go out without a human confirming them."
        )
    if settings.allow_live and settings.max_notional is None and settings.max_quantity is None:
        warnings.append(
            "Live trading is allowed without IBKR_MCP_MAX_NOTIONAL or IBKR_MCP_MAX_QUANTITY: "
            "nothing limits the size of an order."
        )
    if writes and not settings.audit_log:
        warnings.append(
            "Write tools are enabled without IBKR_MCP_AUDIT_LOG: the audit trail goes to the "
            "log only, and an open circuit breaker does not survive a restart."
        )
    if writes and settings.max_notional is not None and not settings.allowed_currencies:
        warnings.append(
            "IBKR_MCP_MAX_NOTIONAL applies in each order's own currency (no FX conversion); "
            "set IBKR_MCP_ALLOWED_CURRENCIES to make it a cap in one currency."
        )
    if settings.allow_live and settings.allow_global_cancel:
        warnings.append(
            "IBKR_MCP_ALLOW_GLOBAL_CANCEL is on with live trading: a confirmed global cancel "
            "stops every working order on the login, other programs' included."
        )
    return warnings


def check_audit_log(settings: Settings, audit: AuditLog | None = None) -> None:
    """Refuse to start a server that may write when its audit file cannot be written.

    Args:
        settings: The configuration; nothing is checked unless a write toolset is on.
        audit: The audit log in use; built from ``settings`` when omitted.

    Raises:
        ConfigurationError: The audit file or its directory cannot be created or appended to.
    """
    if not settings.needs_write_access:
        return
    log = audit if audit is not None else AuditLog(settings.audit_log)
    try:
        log.check_writable()
    except OSError as exc:
        raise ConfigurationError(
            f"IBKR_MCP_AUDIT_LOG={log.path} cannot be written ({exc.strerror or exc}). Write "
            "tools need a working audit trail: fix the path or its permissions (in Docker, "
            "mount a volume writable by the container user)."
        ) from exc


def log_safety_configuration(settings: Settings) -> None:
    """Log :func:`safety_summary` at INFO and each of :func:`risky_settings` at WARNING."""
    logger.info("%s", safety_summary(settings))
    for warning in risky_settings(settings):
        logger.warning("%s", warning)
