"""Configuration from environment variables.

Two families of variables:

* ``IB_*`` describe the gateway connection (host, port, client id, account).
* ``IBKR_MCP_*`` describe how this server behaves (profile, safety limits, transport).

There is no class-level env prefix: every field names its variable explicitly through
``validation_alias``, so the table in the README maps one-to-one onto this module.
Only those names are read from the environment: a bare ``ALLOW_LIVE`` or ``PROFILE``
in a shared env file is ignored, so an audit of ``IB_*`` and ``IBKR_MCP_*`` variables
sees every switch. Fields can also be set by name in code (``Settings(ib_port=4002)``),
which is how the CLI and tests override the environment. A blank variable (``KEY=``)
counts as unset. Secrets accept a ``*_FILE`` variant (Docker secrets): when it is set,
the file is read and stripped. Validation errors never echo input values, so a bad
setting cannot leak a token into the logs.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from ib_gateway_mcp.errors import ConfigurationError

__all__ = [
    "PROFILES",
    "READONLY_TOOLSETS",
    "STREAMING_TOOLSETS",
    "TOOLSETS",
    "WRITE_TOOLSETS",
    "LogLevel",
    "MarketDataType",
    "Profile",
    "Settings",
    "Transport",
    "enabled_toolsets",
]

Profile = Literal["readonly", "trading", "full"]
Transport = Literal["stdio", "http"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
MarketDataType = Literal[1, 2, 3, 4]

READONLY_TOOLSETS: frozenset[str] = frozenset(
    {
        "ops",
        "contracts",
        "market_data",
        "history",
        "scanners",
        "news",
        "fundamentals",
        "account",
        "options",
    }
)
"""Toolsets that only read from the gateway."""

WRITE_TOOLSETS: frozenset[str] = frozenset({"orders", "advisor", "admin"})
"""Toolsets that can change state at IBKR; enabling any of them needs a read-write session."""

TOOLSETS: frozenset[str] = READONLY_TOOLSETS | WRITE_TOOLSETS
"""Every toolset name."""

STREAMING_TOOLSETS: frozenset[str] = frozenset({"scanners", "news", "admin"})
"""Toolsets with ``subscribe_*`` tools whose streams are read and cancelled only through
market_data's list_subscriptions, get_subscription_data and unsubscribe."""

PROFILES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "readonly": READONLY_TOOLSETS,
        "trading": READONLY_TOOLSETS | {"orders"},
        "full": TOOLSETS,
    }
)
"""Named bundles of toolsets, selected with ``IBKR_MCP_PROFILE``."""

_LOOPBACK_NAMES = frozenset({"localhost", "ip6-localhost"})

CsvList = Annotated[list[str], NoDecode]
"""A list read from a comma-separated environment variable (not JSON)."""


def _split_csv(value: object) -> object:
    """Split ``"a, b,,c"`` into ``["a", "b", "c"]``; pass lists and None through."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def _dedupe(items: Iterable[str]) -> list[str]:
    """Drop duplicates, keeping first-seen order."""
    return list(dict.fromkeys(items))


class _AliasOnlyEnvSource(PydanticBaseSettingsSource):
    """Wraps an environment source so only the documented variable names count.

    ``validate_by_name`` lets code pass field names (``Settings(allow_live=True)``), but
    pydantic-settings then also reads environment variables named after the fields
    (``ALLOW_LIVE``, ``PROFILE``), case-insensitively. This source drops those.
    """

    def __init__(self, settings_cls: type[BaseSettings], inner: PydanticBaseSettingsSource) -> None:
        super().__init__(settings_cls)
        self._inner = inner
        self._bare_names = frozenset(
            name
            for name, field in settings_cls.model_fields.items()
            if field.validation_alias is not None
        )

    def get_field_value(self, field: object, field_name: str) -> tuple[object, str, bool]:
        """Unused: :meth:`__call__` delegates to the wrapped source."""
        return None, field_name, False

    def __call__(self) -> dict[str, object]:
        values: dict[str, object] = self._inner()
        return {key: value for key, value in values.items() if key not in self._bare_names}


def _read_secret_file(path: Path, variable: str) -> SecretStr:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"{variable}: cannot read {path}: {exc.strerror}") from exc
    if not content:
        raise ValueError(f"{variable}: {path} is empty")
    return SecretStr(content)


class Settings(BaseSettings):
    """Runtime configuration for the library and the MCP server.

    Build it from the environment with ``Settings()``, or pass fields by name to
    override: ``Settings(ib_port=4002, profile="trading")``. Treat an instance as
    immutable once a :class:`~ib_gateway_mcp.gateway.Gateway` uses it.
    """

    model_config = SettingsConfigDict(
        validate_by_name=True,
        validate_by_alias=True,
        extra="ignore",
        validate_default=True,
        hide_input_in_errors=True,
        # ``KEY=`` (often a compose ``${KEY}`` whose variable is unset) means "not set":
        # the default applies, instead of a validation error or an empty path.
        env_ignore_empty=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Read the environment by the documented variable names only (see the module)."""
        return (
            init_settings,
            _AliasOnlyEnvSource(settings_cls, env_settings),
            _AliasOnlyEnvSource(settings_cls, dotenv_settings),
            file_secret_settings,
        )

    # --- gateway connection ---------------------------------------------------
    ib_host: str = Field("127.0.0.1", validation_alias="IB_HOST", min_length=1)
    ib_port: int = Field(4004, validation_alias="IB_PORT", ge=1, le=65535)
    ib_client_id: int = Field(80, validation_alias="IB_CLIENT_ID")
    ib_account: str | None = Field(None, validation_alias="IB_ACCOUNT")
    connect_timeout: float = Field(10.0, validation_alias="IB_CONNECT_TIMEOUT", gt=0)
    request_timeout: float = Field(30.0, validation_alias="IB_REQUEST_TIMEOUT", gt=0)

    # --- accounts and toolsets --------------------------------------------------
    accounts_allowlist: CsvList = Field(default_factory=list, validation_alias="IBKR_MCP_ACCOUNTS")
    profile: Profile = Field("readonly", validation_alias="IBKR_MCP_PROFILE")
    toolsets: Annotated[list[str] | None, NoDecode] = Field(
        None, validation_alias="IBKR_MCP_TOOLSETS"
    )

    # --- live trading and order safety -----------------------------------------
    allow_live: bool = Field(False, validation_alias="IBKR_MCP_ALLOW_LIVE")
    live_confirm: bool = Field(True, validation_alias="IBKR_MCP_LIVE_CONFIRM")
    token_ttl: int = Field(120, validation_alias="IBKR_MCP_TOKEN_TTL", gt=0)
    max_notional: float | None = Field(None, validation_alias="IBKR_MCP_MAX_NOTIONAL", gt=0)
    max_quantity: float | None = Field(None, validation_alias="IBKR_MCP_MAX_QUANTITY", gt=0)
    allowed_symbols: CsvList = Field(
        default_factory=list, validation_alias="IBKR_MCP_ALLOWED_SYMBOLS"
    )
    allowed_sec_types: CsvList = Field(
        default_factory=list, validation_alias="IBKR_MCP_ALLOWED_SEC_TYPES"
    )
    allowed_currencies: CsvList = Field(
        default_factory=list, validation_alias="IBKR_MCP_ALLOWED_CURRENCIES"
    )
    max_orders_per_minute: int = Field(10, validation_alias="IBKR_MCP_MAX_ORDERS_PER_MINUTE", ge=1)
    max_previews_per_minute: int = Field(
        60, validation_alias="IBKR_MCP_MAX_PREVIEWS_PER_MINUTE", ge=1
    )
    allow_global_cancel: bool = Field(False, validation_alias="IBKR_MCP_ALLOW_GLOBAL_CANCEL")
    breaker_rejects: int = Field(5, validation_alias="IBKR_MCP_CIRCUIT_BREAKER_REJECTS", ge=1)
    audit_log: Path | None = Field(None, validation_alias="IBKR_MCP_AUDIT_LOG")

    # --- subscriptions and market data -------------------------------------------
    max_subscriptions: int = Field(50, validation_alias="IBKR_MCP_MAX_SUBSCRIPTIONS", ge=1)
    subscription_idle_ttl: float = Field(
        900, validation_alias="IBKR_MCP_SUBSCRIPTION_IDLE_TTL", gt=0
    )
    market_data_type: MarketDataType = Field(1, validation_alias="IBKR_MCP_MARKET_DATA_TYPE")
    allow_regulatory_snapshots: bool = Field(
        False, validation_alias="IBKR_MCP_ALLOW_REGULATORY_SNAPSHOTS"
    )

    # --- MCP transport ------------------------------------------------------------
    transport: Transport = Field("stdio", validation_alias="IBKR_MCP_TRANSPORT")
    http_host: str = Field("127.0.0.1", validation_alias="IBKR_MCP_HTTP_HOST", min_length=1)
    http_port: int = Field(8000, validation_alias="IBKR_MCP_HTTP_PORT", ge=1, le=65535)
    auth_token: SecretStr | None = Field(None, validation_alias="IBKR_MCP_AUTH_TOKEN")
    auth_token_file: Path | None = Field(None, validation_alias="IBKR_MCP_AUTH_TOKEN_FILE")
    allow_no_auth: bool = Field(False, validation_alias="IBKR_MCP_ALLOW_NO_AUTH")
    log_level: LogLevel = Field("INFO", validation_alias="IBKR_MCP_LOG_LEVEL")

    # --- validators -------------------------------------------------------------------

    @field_validator("accounts_allowlist", mode="before")
    @classmethod
    def _parse_accounts(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("accounts_allowlist")
    @classmethod
    def _dedupe_accounts(cls, value: list[str]) -> list[str]:
        return _dedupe(item.strip() for item in value if item.strip())

    @field_validator("allowed_symbols", "allowed_sec_types", "allowed_currencies", mode="before")
    @classmethod
    def _parse_upper_csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("allowed_symbols", "allowed_sec_types", "allowed_currencies")
    @classmethod
    def _normalize_upper(cls, value: list[str]) -> list[str]:
        return _dedupe(item.strip().upper() for item in value if item.strip())

    @field_validator("toolsets", mode="before")
    @classmethod
    def _parse_toolsets(cls, value: object) -> object:
        parsed = _split_csv(value)
        return parsed or None

    @field_validator("toolsets")
    @classmethod
    def _check_toolsets(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        names = _dedupe(item.strip().lower() for item in value)
        unknown = sorted(set(names) - TOOLSETS)
        if unknown:
            raise ValueError(
                f"unknown toolset(s) {', '.join(unknown)}; valid: {', '.join(sorted(TOOLSETS))}"
            )
        return names

    @field_validator("ib_client_id")
    @classmethod
    def _check_client_id(cls, value: int) -> int:
        if value == 0:
            raise ValueError(
                "IB_CLIENT_ID=0 is reserved: ib_async binds orders entered by hand in TWS or "
                "the gateway to client 0, so this server could modify or cancel them. Use a "
                "free id such as 80"
            )
        if value < 0:
            raise ValueError("IB_CLIENT_ID must be a positive integer")
        return value

    @field_validator("ib_account", "auth_token", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        # ``KEY=`` (or a compose ``${KEY}`` with the variable unset) means "not set".
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("profile", mode="before")
    @classmethod
    def _lowercase_profile(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("market_data_type", mode="before")
    @classmethod
    def _parse_market_data_type(cls, value: object) -> object:
        # Environment values arrive as strings, which a Literal of ints does not coerce.
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
        return value

    @field_validator("transport", mode="before")
    @classmethod
    def _normalize_transport(cls, value: object) -> object:
        if isinstance(value, str):
            lowered = value.strip().lower()
            return "http" if lowered in {"streamable-http", "streamable_http"} else lowered
        return value

    @model_validator(mode="after")
    def _load_secret_files(self) -> Self:
        if self.auth_token_file is not None:
            if self.auth_token is not None:
                raise ValueError("set IBKR_MCP_AUTH_TOKEN or IBKR_MCP_AUTH_TOKEN_FILE, not both")
            self.auth_token = _read_secret_file(self.auth_token_file, "IBKR_MCP_AUTH_TOKEN_FILE")
        return self

    # --- derived values ------------------------------------------------------------------

    @property
    def needs_write_access(self) -> bool:
        """True when an enabled toolset can change state at IBKR (orders, advisor, admin)."""
        return bool(enabled_toolsets(self) & WRITE_TOOLSETS)

    @property
    def http_host_is_loopback(self) -> bool:
        """True when the HTTP transport only listens on this machine."""
        return is_loopback_host(self.http_host)


def is_loopback_host(host: str) -> bool:
    """Return True for ``localhost`` and loopback IP literals (``127.0.0.0/8``, ``::1``)."""
    name = host.strip().strip("[]").lower()
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def enabled_toolsets(settings: Settings) -> frozenset[str]:
    """Resolve which toolsets are on: ``IBKR_MCP_TOOLSETS`` if set, else the profile.

    ``ops`` is always included, and ``market_data`` comes with any of
    :data:`STREAMING_TOOLSETS`: without its generic subscription tools, their streams
    could be opened but never read or cancelled.

    Raises:
        ConfigurationError: A toolset or profile name is unknown.
    """
    if settings.toolsets is not None:
        names = frozenset(settings.toolsets)
        unknown = sorted(names - TOOLSETS)
        if unknown:
            raise ConfigurationError(
                f"Unknown toolset(s) in IBKR_MCP_TOOLSETS: {', '.join(unknown)}. "
                f"Valid toolsets: {', '.join(sorted(TOOLSETS))}."
            )
    else:
        try:
            names = PROFILES[settings.profile]
        except KeyError:
            raise ConfigurationError(
                f"Unknown profile {settings.profile!r} in IBKR_MCP_PROFILE. "
                f"Valid profiles: {', '.join(PROFILES)}."
            ) from None
    if names & STREAMING_TOOLSETS:
        names |= {"market_data"}
    return names | {"ops"}
