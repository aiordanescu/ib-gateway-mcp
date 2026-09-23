"""Models for gateway health, server time, connection details and accounts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from ib_gateway_mcp.models.common import MarketDataTypeName

__all__ = [
    "AccountInfo",
    "AccountList",
    "ConnectionInfo",
    "ConnectionState",
    "ErrorInfo",
    "HealthProbe",
    "HealthReport",
    "ServerTime",
    "UserInfo",
]


class ConnectionState(StrEnum):
    """Where the gateway connection stands."""

    NOT_CONNECTED = "not_connected"
    """Not started, stopped, or the connection dropped and a retry is pending."""
    CONNECTING = "connecting"
    """A connection attempt is in progress."""
    CONNECTED = "connected"
    """The API session is up and the gateway reaches IBKR's servers."""
    CONNECTIVITY_LOST = "connectivity_lost"
    """The API session is up but the gateway lost its link to IBKR (error 1100 or 2110)."""
    NOT_ACCEPTING = "not_accepting"
    """The gateway refused or ignored the connection: down, logged out, or waiting on 2FA."""


class ErrorInfo(BaseModel):
    """The most recent connection-level error the gateway reported."""

    code: int = Field(description="TWS API error code, or -1 for a local connection failure.")
    message: str
    at: datetime = Field(description="When it was received (UTC).")


class HealthProbe(BaseModel):
    """The outcome of a live round trip to the gateway (``get_health(probe=true)``)."""

    ok: bool = Field(description="True when the gateway answered a server-time request.")
    round_trip_ms: float | None = Field(
        None, description="How long the answer took, in milliseconds (when ok)."
    )
    server_time: datetime | None = Field(None, description="The gateway's clock (when ok).")
    error: str | None = Field(None, description="Why the probe failed (when not ok).")


class HealthReport(BaseModel):
    """A plain-language snapshot of the gateway connection."""

    state: ConnectionState
    hint: str | None = Field(None, description="What is wrong and what to do, when not connected.")
    host: str
    port: int
    client_id: int
    server_version: int | None = Field(None, description="TWS API server version (when connected).")
    connected_since: datetime | None = None
    last_error: ErrorInfo | None = None
    api_read_only: bool = Field(
        False, description="The gateway rejected a request because its API is read-only (321)."
    )
    accounts: list[str] = Field(
        default_factory=list, description="Accounts this server may use (the allowlist)."
    )
    is_paper: bool | None = Field(
        None, description="True when every managed account is a paper account (None: unknown)."
    )
    trading_enabled: bool = Field(
        description=(
            "Whether the trading gate is open right now: connected, accounts allowed, "
            "live trading permitted, API not read-only. Order submits are also refused "
            "while circuit_open is true."
        )
    )
    circuit_open: bool = Field(
        False,
        description=(
            "True when the order circuit breaker tripped after consecutive IBKR "
            "rejections: submit_order refuses new orders, modifications and exercises "
            "until a human resets it (reset_circuit_breaker, admin toolset). Cancels "
            "still work."
        ),
    )
    circuit_rejections: int = Field(
        0, description="Consecutive IBKR order rejections since the last accepted order."
    )
    circuit_threshold: int | None = Field(
        None, description="Consecutive rejections that trip the breaker; null when disabled."
    )
    market_data_type: MarketDataTypeName | None = Field(
        None,
        description="Market data type requested for this session (live, frozen, delayed...).",
    )
    subscriptions_used: int = Field(0, description="Open streaming subscriptions.")
    subscriptions_max: int | None = Field(
        None, description="Subscription limit (IBKR_MCP_MAX_SUBSCRIPTIONS)."
    )
    orders_synced: bool | None = Field(
        None,
        description=(
            "Whether this session loaded the open and completed orders (skipped when no "
            "order toolset is enabled). Informational only: trading_enabled decides whether "
            "order tools work."
        ),
    )
    probe: HealthProbe | None = Field(
        None, description="Result of the live round trip; null unless probe=true was asked."
    )


class ServerTime(BaseModel):
    """The gateway's clock compared with this machine's."""

    server_time: datetime = Field(description="Time reported by the gateway.")
    local_time: datetime = Field(description="This server's clock when the answer arrived (UTC).")
    skew_seconds: float = Field(
        description="local_time minus server_time; IBKR reports whole seconds."
    )


class ConnectionInfo(BaseModel):
    """Technical details of the API session."""

    host: str
    port: int
    client_id: int
    connected: bool
    server_version: int | None = Field(None, description="Negotiated TWS API server version.")
    client_version_range: str = Field(description="API versions this client speaks, 'min..max'.")
    orders_synced: bool | None = Field(
        None, description="Whether this session loaded the open and completed orders."
    )
    connected_since: datetime | None = None
    bytes_received: int | None = None
    bytes_sent: int | None = None
    messages_received: int | None = None
    messages_sent: int | None = None
    ib_async_version: str
    server_package_version: str = Field(description="Version of ib-gateway-mcp.")


class AccountInfo(BaseModel):
    """One account this server may use."""

    account: str
    is_paper: bool = Field(description="Paper accounts start with D (DU..., DF...).")
    is_default: bool = Field(description="Used when a tool is called without an account.")


class AccountList(BaseModel):
    """The accounts in scope for this server."""

    accounts: list[AccountInfo]
    default_account: str | None = Field(
        None, description="Account used when none is given; None means pass one explicitly."
    )
    other_managed_accounts: int = Field(
        0, description="How many more accounts this login manages outside the allowlist."
    )


class UserInfo(BaseModel):
    """Details about the logged-in user."""

    white_branding_id: str | None = Field(
        None, description="White-branding id of the user's broker, if any (empty for most users)."
    )
