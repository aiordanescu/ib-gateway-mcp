"""Settings: environment parsing, defaults, secret files, toolsets and profiles."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ib_gateway_mcp.config import (
    PROFILES,
    READONLY_TOOLSETS,
    TOOLSETS,
    WRITE_TOOLSETS,
    Settings,
    enabled_toolsets,
    is_loopback_host,
)
from ib_gateway_mcp.errors import ConfigurationError


def test_defaults_match_the_documented_table() -> None:
    settings = Settings()
    assert (settings.ib_host, settings.ib_port, settings.ib_client_id) == ("127.0.0.1", 4004, 80)
    assert settings.ib_account is None
    assert settings.connect_timeout == 10.0
    assert settings.request_timeout == 30.0
    assert settings.accounts_allowlist == []
    assert settings.profile == "readonly"
    assert settings.toolsets is None
    assert settings.allow_live is False
    assert settings.live_confirm is True
    assert settings.token_ttl == 120
    assert settings.max_notional is None
    assert settings.max_quantity is None
    assert settings.allowed_symbols == []
    assert settings.allowed_sec_types == []
    assert settings.max_orders_per_minute == 10
    assert settings.breaker_rejects == 5
    assert settings.audit_log is None
    assert settings.max_subscriptions == 50
    assert settings.subscription_idle_ttl == 900
    assert settings.market_data_type == 1
    assert settings.transport == "stdio"
    assert (settings.http_host, settings.http_port) == ("127.0.0.1", 8000)
    assert settings.auth_token is None
    assert settings.allow_no_auth is False
    assert settings.log_level == "INFO"


def test_reads_explicit_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IB_HOST", "ib-gateway")
    monkeypatch.setenv("IB_PORT", "4003")
    monkeypatch.setenv("IB_CLIENT_ID", "81")
    monkeypatch.setenv("IB_ACCOUNT", "DU1234567")
    monkeypatch.setenv("IBKR_MCP_ACCOUNTS", "DU1234567, DU7654321,,DU1234567")
    monkeypatch.setenv("IBKR_MCP_ALLOWED_SYMBOLS", "aapl, msft")
    monkeypatch.setenv("IBKR_MCP_ALLOWED_SEC_TYPES", "stk,opt")
    monkeypatch.setenv("IBKR_MCP_MAX_NOTIONAL", "25000")
    monkeypatch.setenv("IBKR_MCP_PROFILE", "Trading")
    monkeypatch.setenv("IBKR_MCP_LOG_LEVEL", "debug")
    monkeypatch.setenv("IBKR_MCP_MARKET_DATA_TYPE", "3")

    settings = Settings()

    assert (settings.ib_host, settings.ib_port, settings.ib_client_id) == ("ib-gateway", 4003, 81)
    assert settings.ib_account == "DU1234567"
    assert settings.accounts_allowlist == ["DU1234567", "DU7654321"]
    assert settings.allowed_symbols == ["AAPL", "MSFT"]
    assert settings.allowed_sec_types == ["STK", "OPT"]
    assert settings.max_notional == 25000
    assert settings.profile == "trading"
    assert settings.log_level == "DEBUG"
    assert settings.market_data_type == 3


def test_field_names_override_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IB_PORT", "4003")
    assert Settings(ib_port=4002).ib_port == 4002


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ALLOW_LIVE", "true"),
        ("allow_live", "true"),
        ("LIVE_CONFIRM", "false"),
        ("ALLOW_NO_AUTH", "true"),
        ("PROFILE", "full"),
        ("TOOLSETS", "orders"),
        ("AUTH_TOKEN", "x" * 40),
        ("MAX_NOTIONAL", "1"),
        ("IB_CLIENT_ID", "81"),  # the documented name still works
    ],
)
def test_only_documented_variable_names_are_read(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """A stray PROFILE or ALLOW_LIVE in a shared env file must not flip a safety switch."""
    monkeypatch.setenv(name, value)
    settings = Settings()
    assert settings.allow_live is False
    assert settings.live_confirm is True
    assert settings.allow_no_auth is False
    assert settings.profile == "readonly"
    assert settings.toolsets is None
    assert settings.auth_token is None
    assert settings.max_notional is None
    assert settings.ib_client_id == (81 if name == "IB_CLIENT_ID" else 80)


@pytest.mark.parametrize("client_id", [0, -1])
def test_client_id_zero_is_refused(client_id: int) -> None:
    with pytest.raises(ValidationError, match="IB_CLIENT_ID"):
        Settings(ib_client_id=client_id)


def test_blank_secrets_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IBKR_MCP_AUTH_TOKEN", "")
    settings = Settings()
    assert settings.auth_token is None


def test_errors_never_echo_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = "Z1Z2Z3Z4Z5Z6Z7Z8Z9" * 3
    monkeypatch.setenv("IBKR_MCP_AUTH_TOKEN", token)
    monkeypatch.setenv("IB_ACCOUNT", "U7654321")
    monkeypatch.setenv("IB_PORT", token)  # not a port number
    with pytest.raises(ValidationError) as info:
        Settings()
    text = str(info.value)
    assert "IB_PORT" in text
    assert "Z1Z2" not in text
    assert "U7654321" not in text
    monkeypatch.delenv("IB_PORT")
    monkeypatch.delenv("IBKR_MCP_AUTH_TOKEN")
    monkeypatch.setenv("IBKR_MCP_AUTH_TOKEN_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValidationError, match="cannot read") as info:
        Settings()
    assert "U7654321" not in str(info.value)


def test_every_blank_variable_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """``KEY=`` (a compose ``${KEY}`` with the variable unset) falls back to the default.

    Without this, a blank number fails validation and a blank path becomes ``.``, which
    would send the audit log nowhere.
    """
    aliases = [
        str(field.validation_alias)
        for field in Settings.model_fields.values()
        if field.validation_alias is not None
    ]
    assert "IBKR_MCP_AUDIT_LOG" in aliases
    for alias in aliases:
        monkeypatch.setenv(alias, "")
    assert Settings() == Settings.model_validate({})
    assert Settings().audit_log is None
    assert Settings().auth_token_file is None


def test_blank_account_means_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "  ")
    assert Settings().ib_account is None


def test_toolsets_parse_and_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IBKR_MCP_TOOLSETS", "Contracts, market_data")
    assert Settings().toolsets == ["contracts", "market_data"]
    assert Settings(toolsets="").toolsets is None
    with pytest.raises(ValidationError, match="unknown toolset"):
        Settings(toolsets="contracts,bogus")


@pytest.mark.parametrize("value", ["streamable-http", "HTTP", "http"])
def test_transport_aliases(value: str) -> None:
    assert Settings(transport=value).transport == "http"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ib_port", 0),
        ("ib_port", 70000),
        ("connect_timeout", 0),
        ("max_notional", -1),
        ("market_data_type", 5),
        ("profile", "yolo"),
        ("max_orders_per_minute", 0),
        ("ib_client_id", 0),
    ],
)
def test_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_secret_files_are_read_and_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("  bearer-value\n", encoding="utf-8")
    monkeypatch.setenv("IBKR_MCP_AUTH_TOKEN_FILE", str(token_file))

    settings = Settings()

    assert settings.auth_token is not None
    assert settings.auth_token.get_secret_value() == "bearer-value"
    assert "bearer-value" not in repr(settings)


def test_secret_file_errors(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="is empty"):
        Settings(auth_token_file=empty)
    with pytest.raises(ValidationError, match="cannot read"):
        Settings(auth_token_file=tmp_path / "missing")
    good = tmp_path / "good"
    good.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError, match="not both"):
        Settings(auth_token="y", auth_token_file=good)


def test_the_token_secret_setting_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preview tokens live in one process, so a configured key would add nothing."""
    monkeypatch.setenv("IBKR_MCP_TOKEN_SECRET", "ignored")
    assert not hasattr(Settings(), "token_secret")


READONLY_NAMES = frozenset(
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


def test_toolset_names_are_pinned() -> None:
    assert READONLY_TOOLSETS == READONLY_NAMES
    assert {"orders", "advisor", "admin"} == WRITE_TOOLSETS
    assert READONLY_NAMES | {"orders", "advisor", "admin"} == TOOLSETS


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("readonly", READONLY_NAMES),
        ("trading", READONLY_NAMES | {"orders"}),
        ("full", READONLY_NAMES | {"orders", "advisor", "admin"}),
    ],
)
def test_profiles(profile: str, expected: frozenset[str]) -> None:
    settings = Settings(profile=profile)
    assert enabled_toolsets(settings) == expected
    assert PROFILES[profile] == expected
    assert settings.needs_write_access == bool(expected & WRITE_TOOLSETS)


def test_toolsets_override_profile_and_always_include_ops() -> None:
    settings = Settings(profile="full", toolsets="contracts,orders")
    assert enabled_toolsets(settings) == {"ops", "contracts", "orders"}
    assert settings.needs_write_access is True


@pytest.mark.parametrize("streaming", ["scanners", "news", "admin"])
def test_streaming_toolsets_bring_the_subscription_tools(streaming: str) -> None:
    """subscribe_* tools are useless without get_subscription_data and unsubscribe."""
    settings = Settings(toolsets=f"contracts,{streaming}")
    assert enabled_toolsets(settings) == {"ops", "contracts", streaming, "market_data"}


def test_enabled_toolsets_rejects_unknown_names_that_bypassed_validation() -> None:
    settings = Settings.model_construct(toolsets=["ops", "nope"], profile="readonly")
    with pytest.raises(ConfigurationError, match="nope"):
        enabled_toolsets(settings)
    with pytest.raises(ConfigurationError, match="profile"):
        enabled_toolsets(Settings.model_construct(toolsets=None, profile="nope"))


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("localhost", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),  # noqa: S104  (the value under test)
        ("ib-gateway-mcp", False),
        ("10.0.0.5", False),
    ],
)
def test_is_loopback_host(host: str, expected: bool) -> None:
    assert is_loopback_host(host) is expected
    assert Settings(http_host=host).http_host_is_loopback is expected
