"""SafetyRails: built from the real Settings and shared through the Gateway."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import OrderLimitError, TokenNotFoundError
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.safety import (
    AuditLog,
    CircuitBreaker,
    OrderPolicy,
    OrderSummary,
    PreviewStore,
    RateLimiter,
    SafetyRails,
)
from ib_gateway_mcp.services.base import BaseService
from tests.fakes import PAPER_ACCOUNT


def test_from_settings_reads_every_safety_field(
    settings_factory: Callable[..., Settings], tmp_path: Path
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    settings = settings_factory(
        token_ttl=30,
        max_notional=5000,
        max_quantity=100,
        allowed_symbols=["aapl"],
        allowed_sec_types=["stk"],
        max_orders_per_minute=3,
        breaker_rejects=2,
        audit_log=audit_path,
    )
    rails = SafetyRails.from_settings(settings)

    assert isinstance(rails.previews, PreviewStore)
    assert rails.previews.ttl == 30
    assert isinstance(rails.policy, OrderPolicy)
    assert rails.policy.max_notional == Decimal(5000)
    assert rails.policy.max_quantity == Decimal(100)
    assert rails.policy.allowed_symbols == frozenset({"AAPL"})
    assert rails.policy.allowed_sec_types == frozenset({"STK"})
    assert isinstance(rails.rate_limiter, RateLimiter)
    assert rails.rate_limiter.limit == 3
    assert isinstance(rails.breaker, CircuitBreaker)
    assert rails.breaker.threshold == 2
    assert isinstance(rails.audit, AuditLog)
    assert rails.audit.path == audit_path


def test_defaults_match_the_spec(settings: Settings) -> None:
    rails = SafetyRails.from_settings(settings)
    assert rails.previews.ttl == 120
    assert rails.policy.max_notional is None
    assert rails.policy.allowed_symbols == frozenset()
    assert rails.rate_limiter.limit == 10
    assert rails.breaker.threshold == 5
    assert rails.audit.path is None


def test_tokens_round_trip_with_a_random_key_per_store(settings: Settings) -> None:
    store = SafetyRails.from_settings(settings).previews
    issued = store.issue({"action": "BUY", "quantity": 1}, PAPER_ACCOUNT, "order")
    assert store.consume(issued.token) == {"action": "BUY", "quantity": 1}
    with pytest.raises(TokenNotFoundError):
        store.consume(issued.token)
    other = SafetyRails.from_settings(settings).previews
    assert len(store._key) == 32
    assert other._key != store._key


def test_policy_enforces_settings_limits(settings_factory: Callable[..., Settings]) -> None:
    policy = SafetyRails.from_settings(settings_factory(allowed_symbols=["AAPL"])).policy
    summary = OrderSummary(
        symbol="MSFT", sec_type="STK", action="BUY", quantity=1, order_type="MKT"
    )
    with pytest.raises(OrderLimitError, match="MSFT"):
        policy.check(summary)


def test_gateway_builds_one_shared_set_of_rails(settings: Settings, fake_ib: MagicMock) -> None:
    gw = Gateway(settings, ib_factory=lambda: fake_ib)
    assert isinstance(gw.safety, SafetyRails)
    assert gw.orders.safety is gw.safety
    assert gw.advisor.safety is gw.safety
    assert BaseService(gw).safety is gw.safety


def test_gateway_accepts_injected_rails(settings: Settings, fake_ib: MagicMock) -> None:
    rails = SafetyRails.from_settings(settings)
    gw = Gateway(settings, ib_factory=lambda: fake_ib, safety=rails)
    assert gw.safety is rails
    assert gw.orders.safety is rails


def test_rails_are_immutable(settings: Settings) -> None:
    rails = SafetyRails.from_settings(settings)
    with pytest.raises(AttributeError):
        rails.previews = PreviewStore()  # type: ignore[misc]
