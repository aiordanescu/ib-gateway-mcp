"""The bundle of safety objects one :class:`~ib_gateway_mcp.gateway.Gateway` shares.

Order, advisor and admin services reach the rails through ``gateway.safety``, so
every write path in a process uses the same preview store, rate window, breaker
and audit file. With an audit file configured, an open circuit breaker is also
kept next to it (:func:`breaker_state_path`), so it survives a restart.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ib_gateway_mcp.safety.audit import AuditLog, AuditSettings
from ib_gateway_mcp.safety.policy import OrderPolicy, PolicySettings
from ib_gateway_mcp.safety.ratelimit import (
    BreakerSettings,
    CircuitBreaker,
    RateLimiter,
    RateLimitSettings,
)
from ib_gateway_mcp.safety.tokens import PreviewStore, TokenSettings

__all__ = ["SafetyRails", "SafetySettings", "breaker_state_path"]


class SafetySettings(
    TokenSettings, PolicySettings, RateLimitSettings, BreakerSettings, AuditSettings, Protocol
):
    """Every settings field :meth:`SafetyRails.from_settings` reads."""

    @property
    def max_previews_per_minute(self) -> int | None:
        """Previews (what-if checks) allowed per rolling minute; ``None``: unlimited."""
        ...


def breaker_state_path(audit_log: Path | str | None) -> Path | None:
    """Where an open circuit breaker is kept: next to the audit file, or nowhere.

    ``/audit/audit.jsonl`` keeps it in ``/audit/audit.breaker.json``.
    """
    if not audit_log:
        return None
    path = Path(audit_log).expanduser()
    return path.with_name(f"{path.stem}.breaker.json")


@dataclass(frozen=True, slots=True)
class SafetyRails:
    """Preview tokens, order limits, throttling and audit, built once per gateway.

    Attributes:
        previews: Single-use, server-side preview tokens.
        policy: Symbol/sec-type allowlists and size limits.
        rate_limiter: Orders per rolling minute.
        breaker: Halts trading after consecutive rejections.
        audit: JSONL audit trail of every order action.
        preview_limiter: Previews per rolling minute (each sends what-if checks and
            snapshots to IBKR, and fills the preview store); unlimited by default.
    """

    previews: PreviewStore
    policy: OrderPolicy
    rate_limiter: RateLimiter
    breaker: CircuitBreaker
    audit: AuditLog
    preview_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(None, unit="preview"))

    @classmethod
    def from_settings(
        cls,
        settings: SafetySettings,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> SafetyRails:
        """Build every rail from the ``IBKR_MCP_*`` safety settings.

        Args:
            settings: The safety settings.
            clock: Wall-clock epoch seconds, for token expiry, the breaker's trip time
                and audit timestamps. Tests pass a fake (``tests.fakes.FakeClock.time``).
            monotonic: Monotonic seconds, for the order rate window.
        """
        return cls(
            previews=PreviewStore.from_settings(settings, clock=clock),
            policy=OrderPolicy.from_settings(settings),
            rate_limiter=RateLimiter.from_settings(settings, clock=monotonic),
            breaker=CircuitBreaker.from_settings(
                settings, clock=clock, state_path=breaker_state_path(settings.audit_log)
            ),
            audit=AuditLog.from_settings(
                settings, clock=lambda: datetime.fromtimestamp(clock(), UTC)
            ),
            preview_limiter=RateLimiter(
                getattr(settings, "max_previews_per_minute", None), clock=monotonic, unit="preview"
            ),
        )
