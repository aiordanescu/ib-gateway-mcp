"""Append-only JSONL audit trail of order activity.

Every :meth:`AuditLog.record` call emits one JSON object with a UTC ``ts``,
the ``event`` name and the caller's fields. It always goes to the
``ib_gateway_mcp.audit`` logger at INFO, and is also appended to a file when
a path is configured (``IBKR_MCP_AUDIT_LOG``). That logger has its own INFO
level, so a quieter ``IBKR_MCP_LOG_LEVEL`` (WARNING, say) does not silence the
audit trail; set a level on it explicitly to change that.

Secrets never reach the log. Any field whose key contains ``token``,
``secret``, ``password``, ``authorization``, ``credential`` or ``api_key``
(case-insensitive, at any nesting depth) is replaced by ``"[redacted]"``. The
one exception is the preview token id under the key ``token`` or
``preview_token``, which is kept as its first six characters plus ``…`` so
entries can be correlated without the log holding a usable token.

Auditing never breaks the order flow: serialization and I/O errors are logged
as errors and swallowed. :meth:`AuditLog.check_writable` lets a server refuse to
start with an audit file it cannot write. Writes are small, synchronous appends
(each opens, writes one line and closes the file, so rotation is safe); the file
is created with mode 0600 and its parent directory on demand.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time
from enum import Enum, StrEnum
from pathlib import Path
from typing import Final, Protocol

__all__ = [
    "AUDIT_LOGGER_NAME",
    "REDACTED",
    "AuditEvent",
    "AuditLog",
    "AuditSettings",
]

AUDIT_LOGGER_NAME: Final = "ib_gateway_mcp.audit"
REDACTED: Final = "[redacted]"

_PREVIEW_TOKEN_KEYS: Final = frozenset({"token", "preview_token"})
_SENSITIVE_MARKERS: Final = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "credential",
    "api_key",
    "apikey",
)
_TOKEN_PREFIX_CHARS: Final = 6
_MAX_DEPTH: Final = 20


class AuditEvent(StrEnum):
    """Standard event names. :meth:`AuditLog.record` accepts any string."""

    PREVIEW = "preview"
    SUBMIT = "submit"
    SUBMIT_RESULT = "submit_result"
    MODIFY = "modify"
    CANCEL = "cancel"
    CANCEL_ALL = "cancel_all"
    EXERCISE = "exercise"
    REPLACE_FA = "replace_fa"
    REJECTED = "rejected"
    CIRCUIT_OPEN = "circuit_open"
    CIRCUIT_RESET = "circuit_reset"
    REGULATORY_SNAPSHOT = "regulatory_snapshot"


class AuditSettings(Protocol):
    """The settings field :meth:`AuditLog.from_settings` reads."""

    @property
    def audit_log(self) -> Path | str | None:
        """JSONL file path; ``None`` logs to the audit logger only."""
        ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditLog:
    """Structured, append-only audit trail.

    Args:
        path: JSONL file to append to; ``None`` (or empty) means logger only.
        logger: Logger to emit to; defaults to ``ib_gateway_mcp.audit``.
        clock: Returns the current time; naive datetimes are read as UTC.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        logger: logging.Logger | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._path = Path(path).expanduser() if path else None
        if logger is None:
            logger = logging.getLogger(AUDIT_LOGGER_NAME)
            if logger.level == logging.NOTSET:
                # Independent of the root level: audit lines are INFO and must not
                # vanish because the server logs at WARNING.
                logger.setLevel(logging.INFO)
        self._logger = logger
        self._clock = clock
        self._lock = threading.Lock()

    @classmethod
    def from_settings(
        cls, settings: AuditSettings, *, clock: Callable[[], datetime] = _utc_now
    ) -> AuditLog:
        """Build an audit log from ``settings.audit_log``."""
        return cls(settings.audit_log, clock=clock)

    @property
    def path(self) -> Path | None:
        """The JSONL file, or ``None`` when only logging."""
        return self._path

    def check_writable(self) -> None:
        """Make sure the audit file can be appended to (creating it and its directory).

        Raises:
            OSError: The file or its directory cannot be created or opened for append.
        """
        if self._path is None:
            return
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8", opener=_private_opener):
                pass

    def record(self, event: str, /, **fields: object) -> None:
        """Write one audit entry. Never raises.

        Args:
            event: What happened; see :class:`AuditEvent`.
            **fields: Context such as ``account``, ``order_id``, ``summary`` or
                ``reason``. Pydantic models and dataclasses are expanded;
                other non-JSON values are stringified. A field named ``ts`` or
                ``event`` is stored as ``field_ts`` / ``field_event``.
        """
        try:
            entry: dict[str, object] = {"ts": self._timestamp(), "event": str(event)}
            for key, value in fields.items():
                entry[f"field_{key}" if key in entry else key] = _redact(key, value, 0)
            line = json.dumps(entry, default=_json_default, ensure_ascii=False)
        except Exception:  # auditing must never break the order flow
            self._logger.error("Could not serialize audit event %r", event, exc_info=True)
            return
        self._logger.info("%s", line)
        if self._path is not None:
            self._append(self._path, line)

    def _timestamp(self) -> str:
        now = self._clock()
        now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        return now.isoformat(timespec="milliseconds")

    def _append(self, path: Path, line: str) -> None:
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8", opener=_private_opener) as handle:
                    handle.write(line + "\n")
                    handle.flush()
        except OSError as exc:
            self._logger.error("Could not write audit log %s: %s", path, exc)


def _private_opener(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def _redact(key: str, value: object, depth: int) -> object:
    lowered = key.lower()
    if lowered in _PREVIEW_TOKEN_KEYS:
        if value is None:
            return None
        if isinstance(value, str):
            return value[:_TOKEN_PREFIX_CHARS] + "…"
        return REDACTED
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        return REDACTED
    return _scrub(value, depth + 1)


def _scrub(value: object, depth: int) -> object:
    """Expand models and containers, redacting sensitive keys at every level."""
    if depth > _MAX_DEPTH:
        return REDACTED
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump) and not isinstance(value, type):
        value = model_dump(mode="json")
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _redact(str(k), v, depth) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_scrub(item, depth + 1) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _json_default(value: object) -> object:
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return str(value)
