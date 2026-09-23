"""Models for display groups, the server log level and the order circuit breaker."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, Field

from ib_gateway_mcp.models.common import ContractOut

__all__ = [
    "SERVER_LOG_LEVELS",
    "CircuitBreakerReset",
    "CircuitBreakerStatus",
    "DisplayGroupList",
    "DisplayGroupSnapshot",
    "DisplayGroupUpdate",
    "DisplayGroupUpdated",
    "ServerLogLevel",
    "ServerLogLevelOut",
]

ServerLogLevel = Literal["system", "error", "warning", "information", "detail"]
"""Verbosity of the gateway's own API log, least to most."""

SERVER_LOG_LEVELS: Mapping[ServerLogLevel, int] = MappingProxyType(
    {"system": 1, "error": 2, "warning": 3, "information": 4, "detail": 5}
)
"""TWS API codes for :data:`ServerLogLevel` (``setServerLogLevel``)."""


class CircuitBreakerStatus(BaseModel):
    """The order circuit breaker: open means order submission is halted."""

    is_open: bool
    opened_at: datetime | None = Field(None, description="When it opened (UTC).")
    consecutive_rejections: int = Field(description="IBKR rejections since the last success.")
    threshold: int | None = Field(
        None, description="Consecutive rejections that open it; null when disabled."
    )
    last_reason: str | None = Field(None, description="The most recent rejection reason.")

    @property
    def needs_reset(self) -> bool:
        """True when a reset would change something: open, or rejections are counted."""
        return self.is_open or self.consecutive_rejections > 0


class CircuitBreakerReset(BaseModel):
    """The outcome of ``reset_circuit_breaker``."""

    reset: bool = Field(description="False when there was nothing to reset.")
    before: CircuitBreakerStatus = Field(description="The breaker's state before the reset.")
    reason: str = Field(description="Why it was reset, as recorded in the audit log.")
    human_confirmed: bool


class DisplayGroupList(BaseModel):
    """TWS display groups (the colour-linked window groups)."""

    groups: list[int] = Field(description="Group ids for subscribe_display_group.")


class DisplayGroupUpdate(BaseModel):
    """What a display group showed at one moment."""

    time: datetime = Field(description="When IBKR reported it (UTC).")
    contract_info: str = Field(description="IBKR's encoding: 'conId@exchange', 'none' or 'combo'.")
    selection: Literal["contract", "none", "combo"]
    con_id: int | None = None
    exchange: str | None = None


class DisplayGroupSnapshot(BaseModel):
    """The state of a display group subscription."""

    group_id: int
    current: DisplayGroupUpdate | None = Field(
        None, description="The latest selection; null until IBKR reports one."
    )
    updates: list[DisplayGroupUpdate] = Field(
        default_factory=list, description="Recent selections, oldest first."
    )
    error: str | None = Field(None, description="The last IBKR error for this subscription.")


class DisplayGroupUpdated(BaseModel):
    """The outcome of ``update_display_group``."""

    subscription_id: str
    group_id: int
    contract: ContractOut
    contract_info: str = Field(description="What was sent to IBKR: 'conId@exchange'.")


class ServerLogLevelOut(BaseModel):
    """The gateway API log level that was set."""

    level: ServerLogLevel
    code: int = Field(description="TWS API code: 1 system ... 5 detail.")
