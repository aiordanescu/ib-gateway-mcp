"""RateLimiter sliding window and CircuitBreaker trip/reset behaviour."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ib_gateway_mcp.errors import CircuitOpenError, RateLimitError, SafetyError
from ib_gateway_mcp.safety import CircuitBreaker, RateLimiter

START = 1_790_000_000.0


class FakeClock:
    """Manually advanced seconds clock."""

    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# -- RateLimiter ------------------------------------------------------------


def test_allows_up_to_the_limit_then_refuses(clock: FakeClock) -> None:
    limiter = RateLimiter(3, clock=clock)
    for _ in range(3):
        limiter.acquire()

    with pytest.raises(
        RateLimitError, match=r"at most 3 orders per 60 s.*next slot opens in 60\.0 s"
    ):
        limiter.acquire()
    assert limiter.remaining() == 0


def test_wait_time_counts_down(clock: FakeClock) -> None:
    limiter = RateLimiter(1, clock=clock)
    limiter.acquire()
    clock.advance(59.9)

    with pytest.raises(RateLimitError, match=r"in 0\.1 s"):
        limiter.acquire()
    assert limiter.retry_after() == pytest.approx(0.1)


def test_slot_frees_exactly_one_window_later(clock: FakeClock) -> None:
    limiter = RateLimiter(1, clock=clock)
    limiter.acquire()
    clock.advance(60)
    limiter.acquire()


def test_window_slides(clock: FakeClock) -> None:
    limiter = RateLimiter(3, clock=clock)
    for _ in range(3):
        limiter.acquire()  # at t=0, 20, 40
        clock.advance(20)
    # t=60: the t=0 order has left the window.
    assert limiter.remaining() == 1
    clock.advance(1)
    limiter.acquire()  # t=61

    with pytest.raises(RateLimitError, match=r"in 19\.0 s"):  # t=20 order leaves at t=80
        limiter.acquire()
    assert limiter.retry_after() == pytest.approx(19.0)


def test_refused_attempts_do_not_take_slots(clock: FakeClock) -> None:
    limiter = RateLimiter(2, clock=clock)
    limiter.acquire()
    clock.advance(30)
    limiter.acquire()
    for _ in range(5):
        with pytest.raises(RateLimitError):
            limiter.acquire()
    clock.advance(30)  # first order leaves the window

    limiter.acquire()
    with pytest.raises(RateLimitError):
        limiter.acquire()


def test_remaining_and_retry_after_when_free(clock: FakeClock) -> None:
    limiter = RateLimiter(5, clock=clock)
    assert limiter.remaining() == 5
    assert limiter.retry_after() == 0.0
    limiter.acquire()
    assert limiter.remaining() == 4
    assert limiter.retry_after() == 0.0


def test_custom_window(clock: FakeClock) -> None:
    limiter = RateLimiter(1, clock=clock, window=10)
    limiter.acquire()
    with pytest.raises(RateLimitError, match=r"per 10 s"):
        limiter.acquire()
    clock.advance(10)
    limiter.acquire()
    assert limiter.window == 10.0


def test_an_action_takes_one_slot_per_order(clock: FakeClock) -> None:
    """A bracket (3 orders) or an OCA group (up to 10) must not pass as one order."""
    limiter = RateLimiter(5, clock=clock)
    limiter.acquire(3)
    assert limiter.remaining() == 2
    with pytest.raises(
        RateLimitError, match=r"Room for the 3 orders of this action opens in 60\.0 s"
    ):
        limiter.acquire(3)
    assert limiter.remaining() == 2  # a refused action takes nothing
    limiter.acquire(2)
    assert limiter.remaining() == 0


def test_room_for_several_orders_waits_for_enough_expiries(clock: FakeClock) -> None:
    limiter = RateLimiter(3, clock=clock)
    for _ in range(3):
        limiter.acquire()  # at t=0, 10, 20
        clock.advance(10)
    # t=30: two orders need the t=0 and t=10 stamps gone, i.e. t=70.
    with pytest.raises(RateLimitError, match=r"in 40\.0 s"):
        limiter.acquire(2)


def test_more_orders_than_the_limit_can_never_pass(clock: FakeClock) -> None:
    limiter = RateLimiter(2, clock=clock)
    with pytest.raises(RateLimitError, match=r"places 3 orders, more than .* allows at once"):
        limiter.check(3)
    assert limiter.remaining() == 2


def test_check_takes_no_slot(clock: FakeClock) -> None:
    limiter = RateLimiter(1, clock=clock)
    limiter.check()
    limiter.check()
    assert limiter.remaining() == 1
    limiter.acquire()
    with pytest.raises(RateLimitError):
        limiter.check()
    with pytest.raises(ValueError, match="at least 1"):
        limiter.check(0)


def test_unlimited(clock: FakeClock) -> None:
    limiter = RateLimiter(None, clock=clock)
    for _ in range(1_000):
        limiter.acquire()
    assert limiter.limit is None
    assert limiter.remaining() is None
    assert limiter.retry_after() == 0.0


def test_reset_clears_the_window(clock: FakeClock) -> None:
    limiter = RateLimiter(1, clock=clock)
    limiter.acquire()
    limiter.reset()
    limiter.acquire()


@pytest.mark.parametrize(
    ("limit", "window", "match"),
    [(0, 60.0, "max_per_minute"), (-1, 60.0, "max_per_minute"), (1, 0.0, "window")],
)
def test_rate_limiter_rejects_bad_config(limit: int, window: float, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        RateLimiter(limit, window=window)


def test_rate_limiter_from_settings(clock: FakeClock) -> None:
    limiter = RateLimiter.from_settings(SimpleNamespace(max_orders_per_minute=2), clock=clock)
    assert limiter.limit == 2
    limiter.acquire()
    limiter.acquire()
    with pytest.raises(RateLimitError):
        limiter.acquire()


# -- CircuitBreaker ---------------------------------------------------------


def test_opens_after_threshold_consecutive_rejections(clock: FakeClock) -> None:
    breaker = CircuitBreaker(3, clock=clock)
    assert breaker.record_rejection("Order rejected - reason: margin") is False
    assert breaker.record_rejection() is False
    breaker.check()  # still closed

    assert breaker.record_rejection("201: Order rejected - no trading permissions") is True
    assert breaker.is_open
    assert breaker.consecutive_rejections == 3
    assert breaker.opened_at == datetime.fromtimestamp(START, UTC)

    with pytest.raises(CircuitOpenError) as excinfo:
        breaker.check()
    message = str(excinfo.value)
    assert "after 3 consecutive order rejections" in message
    assert "Last rejection: 201: Order rejected - no trading permissions." in message
    assert datetime.fromtimestamp(START, UTC).isoformat() in message
    # The model must stop and hand over to a human, not clear the halt itself.
    assert "Stop placing orders and tell the user" in message
    assert "A human has to" in message


def test_success_resets_the_consecutive_count(clock: FakeClock) -> None:
    breaker = CircuitBreaker(2, clock=clock)
    breaker.record_rejection()
    breaker.record_success()
    assert breaker.consecutive_rejections == 0
    breaker.record_rejection()
    assert not breaker.is_open
    breaker.record_rejection()
    assert breaker.is_open


def test_success_does_not_close_an_open_breaker(clock: FakeClock) -> None:
    breaker = CircuitBreaker(1, clock=clock)
    breaker.record_rejection()
    breaker.record_success()
    assert breaker.is_open
    with pytest.raises(CircuitOpenError):
        breaker.check()


def test_further_rejections_while_open_do_not_retrip(clock: FakeClock) -> None:
    breaker = CircuitBreaker(1, clock=clock)
    assert breaker.record_rejection() is True
    clock.advance(30)
    assert breaker.record_rejection() is False
    assert breaker.opened_at == datetime.fromtimestamp(START, UTC)


def test_reset_closes_the_breaker(clock: FakeClock) -> None:
    breaker = CircuitBreaker(1, clock=clock)
    breaker.record_rejection("bad")
    breaker.reset()

    breaker.check()
    assert not breaker.is_open
    assert breaker.opened_at is None
    assert breaker.consecutive_rejections == 0
    assert breaker.last_reason is None


def test_message_without_reason(clock: FakeClock) -> None:
    breaker = CircuitBreaker(1, clock=clock)
    breaker.record_rejection()
    with pytest.raises(CircuitOpenError) as excinfo:
        breaker.check()
    assert "Last rejection" not in str(excinfo.value)


def test_long_reasons_are_truncated(clock: FakeClock) -> None:
    breaker = CircuitBreaker(5, clock=clock)
    breaker.record_rejection("x" * 1_000)
    assert breaker.last_reason == "x" * 300


def test_disabled_breaker_never_opens(clock: FakeClock) -> None:
    breaker = CircuitBreaker(None, clock=clock)
    for _ in range(100):
        assert breaker.record_rejection() is False
    breaker.check()
    assert breaker.threshold is None


@pytest.mark.parametrize("threshold", [0, -3])
def test_breaker_rejects_bad_threshold(threshold: int) -> None:
    with pytest.raises(ValueError, match="threshold"):
        CircuitBreaker(threshold)


def test_breaker_from_settings(clock: FakeClock) -> None:
    breaker = CircuitBreaker.from_settings(SimpleNamespace(breaker_rejects=2), clock=clock)
    assert breaker.threshold == 2


def test_errors_are_safety_errors() -> None:
    assert issubclass(RateLimitError, SafetyError)
    assert issubclass(CircuitOpenError, SafetyError)


def test_a_preview_limiter_names_previews(clock: FakeClock) -> None:
    limiter = RateLimiter(1, clock=clock, unit="preview")
    limiter.acquire()
    with pytest.raises(RateLimitError, match="Preview rate limit reached: at most 1 previews"):
        limiter.acquire()


def test_an_open_breaker_survives_a_restart(clock: FakeClock, tmp_path: Path) -> None:
    state = tmp_path / "audit.breaker.json"
    breaker = CircuitBreaker(2, clock=clock, state_path=state)
    breaker.record_rejection("margin")
    assert not state.exists()
    assert breaker.record_rejection("margin") is True
    assert json.loads(state.read_text(encoding="utf-8"))["last_reason"] == "margin"

    restarted = CircuitBreaker(2, clock=clock, state_path=state)
    assert restarted.is_open
    assert restarted.last_reason == "margin"
    with pytest.raises(CircuitOpenError) as caught:
        restarted.check()
    assert "restart" not in str(caught.value)
    assert "reset_circuit_breaker" in str(caught.value)

    restarted.reset()
    assert not state.exists()
    assert not CircuitBreaker(2, clock=clock, state_path=state).is_open


def test_an_unreadable_breaker_state_counts_as_open(clock: FakeClock, tmp_path: Path) -> None:
    state = tmp_path / "audit.breaker.json"
    state.write_text("{not json", encoding="utf-8")
    breaker = CircuitBreaker(5, clock=clock, state_path=state)
    assert breaker.is_open
    assert "unreadable" in (breaker.last_reason or "")
