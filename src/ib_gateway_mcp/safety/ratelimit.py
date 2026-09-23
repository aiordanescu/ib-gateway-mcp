"""Order throttling: a sliding-window rate limit and a rejection circuit breaker.

Both are synchronous and never await, so each call is atomic with respect to
the asyncio event loop; neither is meant to be shared between threads.

The breaker can keep its open state in a small JSON file (next to the audit log,
see :class:`~ib_gateway_mcp.safety.rails.SafetyRails`), so restarting the server
does not re-arm trading: only :meth:`CircuitBreaker.reset` (the human-confirmed
``reset_circuit_breaker`` tool) or an operator deleting the file does.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from ib_gateway_mcp.errors import CircuitOpenError, RateLimitError

__all__ = [
    "BreakerSettings",
    "CircuitBreaker",
    "RateLimitSettings",
    "RateLimiter",
]

logger = logging.getLogger(__name__)

_MAX_REASON_CHARS: Final = 300


class RateLimitSettings(Protocol):
    """The settings field :meth:`RateLimiter.from_settings` reads."""

    @property
    def max_orders_per_minute(self) -> int | None:
        """Orders allowed per rolling minute; ``None`` disables the limit."""
        ...


class BreakerSettings(Protocol):
    """The settings field :meth:`CircuitBreaker.from_settings` reads."""

    @property
    def breaker_rejects(self) -> int | None:
        """Consecutive rejections that open the breaker; ``None`` disables it."""
        ...


class RateLimiter:
    """Allow at most ``max_per_minute`` orders in any rolling window.

    Args:
        max_per_minute: Orders allowed per window; ``None`` means unlimited.
        clock: Monotonic seconds; defaults to :func:`time.monotonic`.
        window: Window length in seconds (60 unless testing or tuning).
        unit: What is counted, singular (``"order"``, or ``"preview"`` for the
            preview limiter); used in the error messages.

    Raises:
        ValueError: ``max_per_minute`` is below 1 or ``window`` is not positive.
    """

    def __init__(
        self,
        max_per_minute: int | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        window: float = 60.0,
        unit: str = "order",
    ) -> None:
        if max_per_minute is not None and max_per_minute < 1:
            raise ValueError(f"max_per_minute must be at least 1 or None, got {max_per_minute!r}")
        if not window > 0:
            raise ValueError(f"window must be positive, got {window!r}")
        self._limit = max_per_minute
        self._clock = clock
        self._window = float(window)
        self._unit = unit
        self._stamps: deque[float] = deque()

    @classmethod
    def from_settings(
        cls, settings: RateLimitSettings, *, clock: Callable[[], float] = time.monotonic
    ) -> RateLimiter:
        """Build a limiter from ``settings.max_orders_per_minute``."""
        return cls(settings.max_orders_per_minute, clock=clock)

    @property
    def limit(self) -> int | None:
        """Orders allowed per window, or ``None`` when unlimited."""
        return self._limit

    @property
    def window(self) -> float:
        """Window length in seconds."""
        return self._window

    def remaining(self) -> int | None:
        """Slots left in the current window, or ``None`` when unlimited."""
        if self._limit is None:
            return None
        self._expire(self._clock())
        return max(self._limit - len(self._stamps), 0)

    def retry_after(self) -> float:
        """Seconds until a slot frees up; ``0.0`` if one is free now."""
        if self._limit is None:
            return 0.0
        now = self._clock()
        self._expire(now)
        if len(self._stamps) < self._limit:
            return 0.0
        return max(self._stamps[0] + self._window - now, 0.0)

    def check(self, count: int = 1) -> None:
        """Raise unless ``count`` orders fit in the window now. Takes no slot.

        Raises:
            RateLimitError: The orders do not fit; the message says when they will,
                or that they never can (``count`` above the limit).
            ValueError: ``count`` is below 1.
        """
        if count < 1:
            raise ValueError(f"count must be at least 1, got {count!r}")
        if self._limit is None:
            return
        now = self._clock()
        self._expire(now)
        unit = self._unit
        if count > self._limit:
            raise RateLimitError(
                f"This action places {count} {unit}s, more than the {unit} rate limit of "
                f"{self._limit} {unit}s per {self._window:g} s allows at once, so it can never "
                "be sent as one action. Nothing was sent."
            )
        used = len(self._stamps)
        if used + count > self._limit:
            # The oldest stamps expire first; this many must go before the orders fit.
            wait = max(self._stamps[used + count - self._limit - 1] + self._window - now, 0.0)
            when = (
                f"Room for the {count} {unit}s of this action opens in {wait:.1f} s"
                if count > 1
                else f"The next slot opens in {wait:.1f} s"
            )
            raise RateLimitError(
                f"{unit.capitalize()} rate limit reached: at most {self._limit} {unit}s per "
                f"{self._window:g} s. {when}; retry after that."
            )

    def acquire(self, count: int = 1) -> None:
        """Take slots for ``count`` orders (one action may place several).

        Raises:
            RateLimitError: The orders do not fit in the window (see :meth:`check`);
                no slot is taken then.
            ValueError: ``count`` is below 1.
        """
        self.check(count)
        if self._limit is None:
            return
        now = self._clock()
        self._stamps.extend([now] * count)

    def reset(self) -> None:
        """Forget every recorded order."""
        self._stamps.clear()

    def _expire(self, now: float) -> None:
        cutoff = now - self._window
        while self._stamps and self._stamps[0] <= cutoff:
            self._stamps.popleft()


class CircuitBreaker:
    """Halt order submission after ``threshold`` consecutive IBKR rejections.

    Once open, the breaker stays open until :meth:`reset`; a later success does not
    close it. With a ``state_path`` it also stays open across restarts: the trip is
    written there and read back at start-up, so restarting the server is no way
    around it. The MCP ``reset_circuit_breaker`` tool lives in the ``admin`` toolset
    (not in the trading profile) and asks a human to confirm, so the model whose
    orders tripped the breaker cannot clear it by itself.

    Args:
        threshold: Consecutive rejections that open the breaker; ``None``
            disables it.
        clock: Wall-clock epoch seconds, used to timestamp the trip; defaults
            to :func:`time.time`.
        state_path: JSON file that keeps an open breaker open across restarts;
            ``None`` keeps the state in memory only. A file that cannot be read
            counts as an open breaker (fail closed).

    Raises:
        ValueError: ``threshold`` is below 1.
    """

    def __init__(
        self,
        threshold: int | None,
        *,
        clock: Callable[[], float] = time.time,
        state_path: Path | str | None = None,
    ) -> None:
        if threshold is not None and threshold < 1:
            raise ValueError(f"threshold must be at least 1 or None, got {threshold!r}")
        self._threshold = threshold
        self._clock = clock
        self._consecutive = 0
        self._opened_at: float | None = None
        self._last_reason: str | None = None
        self._state_path = Path(state_path).expanduser() if state_path else None
        self._load()

    @classmethod
    def from_settings(
        cls,
        settings: BreakerSettings,
        *,
        clock: Callable[[], float] = time.time,
        state_path: Path | str | None = None,
    ) -> CircuitBreaker:
        """Build a breaker from ``settings.breaker_rejects``."""
        return cls(settings.breaker_rejects, clock=clock, state_path=state_path)

    @property
    def state_path(self) -> Path | None:
        """Where an open breaker is kept across restarts, or ``None``."""
        return self._state_path

    @property
    def threshold(self) -> int | None:
        """Consecutive rejections that open the breaker, or ``None`` if disabled."""
        return self._threshold

    @property
    def is_open(self) -> bool:
        """Whether order submission is halted."""
        return self._opened_at is not None

    @property
    def consecutive_rejections(self) -> int:
        """Rejections since the last success or reset."""
        return self._consecutive

    @property
    def opened_at(self) -> datetime | None:
        """When the breaker opened (UTC), or ``None`` while closed."""
        return None if self._opened_at is None else datetime.fromtimestamp(self._opened_at, UTC)

    @property
    def last_reason(self) -> str | None:
        """The most recent rejection reason, if any."""
        return self._last_reason

    def check(self) -> None:
        """Raise :class:`CircuitOpenError` if submission is halted."""
        opened_at = self.opened_at
        if opened_at is None:
            return
        last = f" Last rejection: {self._last_reason}." if self._last_reason else ""
        raise CircuitOpenError(
            f"Order submission is halted: the circuit breaker opened at "
            f"{opened_at.isoformat()} after {self._threshold} consecutive order "
            f"rejections.{last} Stop placing orders and tell the user. A human has to "
            "investigate the rejections and resume trading by confirming "
            "reset_circuit_breaker (admin toolset)."
        )

    def record_success(self) -> None:
        """Note an accepted order; clears the consecutive count (not an open breaker)."""
        if not self.is_open:
            self._consecutive = 0

    def record_rejection(self, reason: str | None = None) -> bool:
        """Note an IBKR rejection; return ``True`` if this one opened the breaker."""
        self._consecutive += 1
        if reason:
            self._last_reason = reason[:_MAX_REASON_CHARS]
        if self.is_open or self._threshold is None or self._consecutive < self._threshold:
            return False
        self._opened_at = self._clock()
        self._save()
        return True

    def reset(self) -> None:
        """Close the breaker and clear the rejection count (and the saved state)."""
        self._consecutive = 0
        self._opened_at = None
        self._last_reason = None
        if self._state_path is not None:
            try:
                self._state_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.error(
                    "Could not remove the circuit breaker state %s: %s", self._state_path, exc
                )

    def _save(self) -> None:
        if self._state_path is None:
            return
        state = {
            "opened_at": self._opened_at,
            "rejections": self._consecutive,
            "last_reason": self._last_reason,
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
        except OSError as exc:
            logger.error(
                "Could not save the circuit breaker state to %s (it stays open until a "
                "reset, but a restart would forget it): %s",
                self._state_path,
                exc,
            )

    def _load(self) -> None:
        path = self._state_path
        if path is None or not path.exists():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            opened_at = float(state["opened_at"])
            rejections = int(state.get("rejections") or 0)
            reason = state.get("last_reason")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logger.error("Unreadable circuit breaker state %s (%s): treating it as open", path, exc)
            opened_at, rejections, reason = self._clock(), 0, f"unreadable state file {path}"
        self._opened_at = opened_at
        self._consecutive = rejections
        self._last_reason = str(reason)[:_MAX_REASON_CHARS] if reason else None
        logger.warning(
            "The order circuit breaker is open (saved in %s): order submission stays halted "
            "until a human resets it",
            path,
        )
