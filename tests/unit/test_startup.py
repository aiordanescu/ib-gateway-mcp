"""startup: the safety summary, risky combinations and the audit file check."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import ConfigurationError
from ib_gateway_mcp.mcp.server import build_instructions, build_server
from ib_gateway_mcp.startup import (
    check_audit_log,
    log_safety_configuration,
    risky_settings,
    safety_summary,
)


def test_summary_names_every_switch(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(
        profile="trading", max_notional=5000, allowed_currencies=["USD"], audit_log="/a/x.jsonl"
    )
    summary = safety_summary(settings)
    for part in (
        "live trading off",
        "live confirmation on",
        "global cancel off",
        "regulatory snapshots off",
        "max notional 5,000 per order",
        "currencies USD",
        "At most 10 orders and 60 previews per minute",
        "after 5 consecutive rejections",
        "audit file /a/x.jsonl (breaker state /a/x.breaker.json)",
    ):
        assert part in summary


def test_risky_combinations(settings_factory: Callable[..., Settings]) -> None:
    risky = risky_settings(
        settings_factory(profile="trading", allow_live=True, live_confirm=False, max_notional=100)
    )
    assert any("without a human confirming" in w for w in risky)
    assert any("without IBKR_MCP_AUDIT_LOG" in w for w in risky)
    assert any("IBKR_MCP_ALLOWED_CURRENCIES" in w for w in risky)
    unlimited = risky_settings(settings_factory(profile="trading", allow_live=True))
    assert any("nothing limits the size of an order" in w for w in unlimited)
    assert risky_settings(settings_factory()) == []  # readonly: nothing to warn about


def test_the_summary_and_warnings_are_logged(
    settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ib_gateway_mcp.startup")
    log_safety_configuration(settings_factory(profile="trading"))
    levels = [(r.levelname, r.getMessage()[:7]) for r in caplog.records]
    assert ("INFO", "Safety:") in levels
    assert any(level == "WARNING" for level, _ in levels)


def test_an_unwritable_audit_file_stops_a_trading_server(
    settings_factory: Callable[..., Settings], tmp_path: Path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    bad = settings_factory(profile="trading", audit_log=blocker / "audit.jsonl")
    with pytest.raises(ConfigurationError, match="cannot be written"):
        check_audit_log(bad)
    with pytest.raises(ConfigurationError, match="IBKR_MCP_AUDIT_LOG"):
        build_server(bad)
    check_audit_log(settings_factory(audit_log=blocker / "audit.jsonl"))  # readonly: no writes
    check_audit_log(settings_factory(profile="trading", audit_log=tmp_path / "ok.jsonl"))


def test_the_instructions_state_the_limits(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(profile="trading", max_quantity=100)
    text = build_instructions(settings, frozenset({"ops", "orders"}))
    assert "Order limits: max quantity 100." in text
