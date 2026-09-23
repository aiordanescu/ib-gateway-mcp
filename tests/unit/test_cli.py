"""Command line: argument parsing, settings overrides and exit codes."""

from __future__ import annotations

import pytest

from ib_gateway_mcp import cli
from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import ConfigurationError


@pytest.fixture
def captured_run(monkeypatch: pytest.MonkeyPatch) -> list[Settings]:
    """Replace the server's run() so main() returns right after building settings."""
    calls: list[Settings] = []
    monkeypatch.setattr("ib_gateway_mcp.mcp.server.run", calls.append)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    return calls


def test_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    for flag in ("--help", "--version"):
        with pytest.raises(SystemExit) as info:
            cli.main([flag])
        assert info.value.code == 0
    assert "ib-gateway-mcp" in capsys.readouterr().out


def test_flags_override_the_environment(
    monkeypatch: pytest.MonkeyPatch, captured_run: list[Settings]
) -> None:
    monkeypatch.setenv("IBKR_MCP_PROFILE", "full")
    exit_code = cli.main(
        [
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            "8123",
            "--profile",
            "trading",
            "--toolsets",
            "contracts,orders",
            "--log-level",
            "debug",
        ]
    )
    assert exit_code == 0
    (settings,) = captured_run
    assert settings.transport == "http"
    assert settings.http_port == 8123
    assert settings.profile == "trading"
    assert settings.toolsets == ["contracts", "orders"]
    assert settings.log_level == "DEBUG"


def test_environment_is_used_without_flags(
    monkeypatch: pytest.MonkeyPatch, captured_run: list[Settings]
) -> None:
    monkeypatch.setenv("IB_PORT", "4002")
    assert cli.main([]) == 0
    assert captured_run[0].ib_port == 4002
    assert captured_run[0].transport == "stdio"


def test_invalid_configuration_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("IB_PORT", "not-a-port")
    with pytest.raises(SystemExit) as info:
        cli.main([])
    assert info.value.code == 2
    assert "invalid configuration" in capsys.readouterr().err


def test_configuration_errors_from_the_server_exit_2(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(_settings: Settings) -> None:
        raise ConfigurationError("The HTTP transport needs a bearer token.")

    monkeypatch.setattr("ib_gateway_mcp.mcp.server.run", refuse)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    assert cli.main(["--transport", "http"]) == 2
    assert "bearer token" in caplog.text


def test_keyboard_interrupt_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(_settings: Settings) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("ib_gateway_mcp.mcp.server.run", interrupted)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    assert cli.main([]) == 130
