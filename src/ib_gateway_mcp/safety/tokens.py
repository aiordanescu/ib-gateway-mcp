"""Preview tokens: the server-side store behind the two-step order flow.

A ``preview_*`` call validates an order, stores the exact payload here and
returns an opaque token. ``submit_order(token)`` can only execute what was
stored, so the order cannot change between preview and submit.

Guarantees:

* **Opaque:** tokens are ``secrets.token_urlsafe(24)`` (192 bits of entropy).
* **Single-use:** :meth:`PreviewStore.consume` removes the entry;
  :meth:`PreviewStore.discard` removes one that will never be submitted (for
  example, a human declined it), and later lookups say nothing was sent.
* **Short-lived:** entries expire after ``ttl`` seconds and are purged on
  every access.
* **Server-side:** the order itself never leaves the server; the token is only
  a key into this store, so a caller cannot alter what a token submits.
* **Integrity check:** each entry also carries an HMAC-SHA256 digest (under a
  random per-process key) over the token, kind, account, expiry and canonical
  JSON of the payload, so an accidental in-process change to a stored entry
  makes the next lookup fail with
  :class:`~ib_gateway_mcp.errors.TokenMismatchError` instead of sending
  something else. It is a consistency check, not a secret: nothing outside the
  process ever sees an entry.

The store lives in memory: tokens die with the process, so a server restart
invalidates every outstanding preview.

Concurrency: every method is synchronous and never awaits, so each call is
atomic with respect to the asyncio event loop. A lock additionally makes the
store safe to share between threads.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Protocol

from ib_gateway_mcp.errors import (
    TokenExpiredError,
    TokenMismatchError,
    TokenNotFoundError,
)

__all__ = [
    "Clock",
    "PreviewRecord",
    "PreviewStore",
    "PreviewToken",
    "TokenSettings",
]

logger = logging.getLogger(__name__)

type Clock = Callable[[], float | datetime]
"""Returns the current time as epoch seconds or a datetime (naive means UTC)."""

DEFAULT_TTL: Final = 120.0
DEFAULT_MAX_ENTRIES: Final = 1024
_TOKEN_BYTES: Final = 24
_MAX_NOTE_CHARS: Final = 200
_DIGEST_VERSION: Final = 1


class TokenSettings(Protocol):
    """The settings fields :meth:`PreviewStore.from_settings` reads."""

    @property
    def token_ttl(self) -> float:
        """Token lifetime in seconds."""
        ...


@dataclass(frozen=True, slots=True)
class PreviewToken:
    """What a preview hands back to the caller."""

    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PreviewRecord:
    """A verified stored preview, as returned by ``peek`` and ``consume_record``."""

    token: str
    kind: str
    account: str
    payload: dict[str, Any]
    created_at: datetime
    expires_at: datetime


@dataclass(slots=True)
class _Entry:
    kind: str
    account: str
    payload: dict[str, Any]
    digest: bytes
    created_at: float
    expires_at: float


class _Gone(StrEnum):
    """Why a token that was once issued is no longer usable."""

    USED = "used"
    DISCARDED = "discarded"
    EXPIRED = "expired"
    EVICTED = "evicted"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class _Tombstone:
    reason: _Gone
    expires_at: float
    forget_at: float
    note: str | None = None


class PreviewStore:
    """In-memory, single-use store of previewed orders.

    Args:
        ttl: Seconds a token stays valid. Must be positive.
        secret: Key of the entries' integrity digest. ``None`` (the default, and
            what :meth:`from_settings` uses) generates a random 32-byte key for
            this process.
        clock: Returns the current wall-clock time, as epoch seconds or a
            datetime (naive datetimes are read as UTC). Defaults to
            :func:`time.time`; tests inject a fake.
        max_entries: Cap on pending previews. When full, the oldest pending
            preview is discarded to make room.
    """

    def __init__(
        self,
        *,
        ttl: float = DEFAULT_TTL,
        secret: bytes | str | None = None,
        clock: Clock = time.time,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if not ttl > 0:
            raise ValueError(f"token ttl must be positive, got {ttl!r}")
        if max_entries < 1:
            raise ValueError(f"max_entries must be at least 1, got {max_entries!r}")
        if secret is None:
            key = secrets.token_bytes(32)
        elif isinstance(secret, str):
            key = secret.encode()
        else:
            key = bytes(secret)
        if not key:
            raise ValueError("token secret must not be empty")
        self._ttl = float(ttl)
        self._key = key
        self._clock = clock
        self._max_entries = max_entries
        self._entries: dict[str, _Entry] = {}
        self._tombstones: dict[str, _Tombstone] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: TokenSettings, *, clock: Clock = time.time) -> PreviewStore:
        """Build a store from ``settings.token_ttl``, with a random per-process key."""
        return cls(ttl=settings.token_ttl, clock=clock)

    @property
    def ttl(self) -> float:
        """Token lifetime in seconds."""
        return self._ttl

    def __len__(self) -> int:
        """Number of pending (unexpired, unused) previews."""
        with self._lock:
            self._purge(self._now())
            return len(self._entries)

    def issue(self, payload: Mapping[str, Any], account: str, kind: str) -> PreviewToken:
        """Store a previewed order and return a fresh single-use token.

        Args:
            payload: The exact order to execute on submit. It must be
                JSON-serializable without custom encoders (use
                ``model.model_dump(mode="json")`` for pydantic models); NaN and
                infinity are rejected. The store keeps a deep copy.
            account: The account the order is bound to.
            kind: What the payload describes, e.g. ``"order"``, ``"bracket"``
                or ``"cancel_all"``; the submit path dispatches on it.

        Returns:
            The token and its expiry (UTC).

        Raises:
            TypeError: ``payload`` is not a JSON object of plain JSON values.
            ValueError: ``account`` or ``kind`` is empty, or the payload holds
                NaN or infinity.
        """
        if not account:
            raise ValueError("account must not be empty")
        if not kind:
            raise ValueError("kind must not be empty")
        stored = json.loads(_canonical_json(dict(payload)))
        with self._lock:
            now = self._now()
            self._purge(now)
            self._make_room()
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            while token in self._entries or token in self._tombstones:  # pragma: no cover
                token = secrets.token_urlsafe(_TOKEN_BYTES)
            expires_at = now + self._ttl
            self._entries[token] = _Entry(
                kind=kind,
                account=account,
                payload=stored,
                digest=self._digest(token, kind, account, stored, expires_at),
                created_at=now,
                expires_at=expires_at,
            )
        return PreviewToken(token=token, expires_at=_to_datetime(expires_at))

    def peek(self, token: str) -> PreviewRecord:
        """Return a verified copy of a pending preview without consuming it.

        Raises:
            TokenNotFoundError: Unknown, already used or discarded token.
            TokenExpiredError: The token's TTL has passed.
            TokenMismatchError: The stored entry failed its integrity check
                (the entry is discarded).
        """
        with self._lock:
            self._purge(self._now())
            entry = self._verified(token)
            return self._record(token, entry, copy.deepcopy(entry.payload))

    def consume(self, token: str) -> dict[str, Any]:
        """Verify, remove and return the stored payload. Single-use.

        Raises:
            TokenNotFoundError: Unknown, already used or discarded token.
            TokenExpiredError: The token's TTL has passed.
            TokenMismatchError: The stored entry failed its integrity check
                (the entry is discarded).
        """
        return self.consume_record(token).payload

    def consume_record(self, token: str) -> PreviewRecord:
        """Like :meth:`consume`, but also return the bound account and kind."""
        with self._lock:
            self._purge(self._now())
            entry = self._verified(token)
            del self._entries[token]
            self._bury(token, _Gone.USED, entry.expires_at)
            return self._record(token, entry, entry.payload)

    def discard(self, token: str, *, note: str | None = None) -> PreviewRecord:
        """Verify and remove a preview that will not be submitted. Nothing is executed.

        Later lookups of the token raise :class:`TokenNotFoundError` saying it was
        discarded without sending anything, with ``note`` (e.g. why) appended.

        Raises:
            TokenNotFoundError: Unknown, already used or discarded token.
            TokenExpiredError: The token's TTL has passed.
            TokenMismatchError: The stored entry failed its integrity check.
        """
        with self._lock:
            self._purge(self._now())
            entry = self._verified(token)
            del self._entries[token]
            clean = " ".join(note.split())[:_MAX_NOTE_CHARS] if note else None
            self._bury(token, _Gone.DISCARDED, entry.expires_at, note=clean or None)
            return self._record(token, entry, entry.payload)

    def purge_expired(self) -> int:
        """Drop expired previews now; return how many were dropped."""
        with self._lock:
            return self._purge(self._now())

    # -- internals ---------------------------------------------------------

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.timestamp()
        return float(value)

    def _digest(
        self, token: str, kind: str, account: str, payload: dict[str, Any], expires_at: float
    ) -> bytes:
        material = _canonical_json(
            {
                "v": _DIGEST_VERSION,
                "token": token,
                "kind": kind,
                "account": account,
                "expires_at": expires_at,
                "payload": payload,
            }
        )
        return hmac.new(self._key, material.encode(), hashlib.sha256).digest()

    def _verified(self, token: str) -> _Entry:
        entry = self._entries.get(token) if isinstance(token, str) else None
        if entry is None:
            raise self._gone_error(token)
        try:
            expected = self._digest(
                token, entry.kind, entry.account, entry.payload, entry.expires_at
            )
        except (TypeError, ValueError):  # payload mutated into something non-JSON
            expected = b""
        if not hmac.compare_digest(expected, entry.digest):
            del self._entries[token]
            self._bury(token, _Gone.INVALIDATED, entry.expires_at)
            logger.error("Preview token %s… failed its integrity check; discarded", token[:6])
            raise TokenMismatchError(_MISMATCH_MESSAGE)
        return entry

    def _gone_error(
        self, token: object
    ) -> TokenNotFoundError | TokenExpiredError | TokenMismatchError:
        tomb = self._tombstones.get(token) if isinstance(token, str) else None
        if tomb is None:
            return TokenNotFoundError(
                "Unknown preview token. Tokens come from a preview tool, are single-use "
                "and do not survive a server restart; run the preview again."
            )
        if tomb.reason is _Gone.EXPIRED:
            return TokenExpiredError(
                f"Preview token expired at {_to_datetime(tomb.expires_at).isoformat()} "
                f"(tokens are valid for {self._ttl:g} s). Run the preview again."
            )
        if tomb.reason is _Gone.USED:
            return TokenNotFoundError(
                "Preview token was already used; each token submits exactly once. "
                "Run the preview again to place another order."
            )
        if tomb.reason is _Gone.DISCARDED:
            why = f" ({tomb.note})" if tomb.note else ""
            return TokenNotFoundError(
                f"Preview token was discarded without sending anything{why}. Run the "
                "preview again if the action is still wanted."
            )
        if tomb.reason is _Gone.EVICTED:
            return TokenNotFoundError(
                f"Preview token was discarded because more than {self._max_entries} "
                "previews were pending. Run the preview again."
            )
        return TokenMismatchError(_MISMATCH_MESSAGE)

    def _purge(self, now: float) -> int:
        expired = [t for t, e in self._entries.items() if e.expires_at <= now]
        for token in expired:
            entry = self._entries.pop(token)
            self._bury(token, _Gone.EXPIRED, entry.expires_at)
        stale = [t for t, s in self._tombstones.items() if s.forget_at <= now]
        for token in stale:
            del self._tombstones[token]
        return len(expired)

    def _make_room(self) -> None:
        while len(self._entries) >= self._max_entries:
            token = next(iter(self._entries))
            entry = self._entries.pop(token)
            self._bury(token, _Gone.EVICTED, entry.expires_at)
            logger.warning(
                "Preview store full (%d pending); discarded oldest token %s…",
                self._max_entries,
                token[:6],
            )

    def _bury(
        self, token: str, reason: _Gone, expires_at: float, *, note: str | None = None
    ) -> None:
        """Remember why a token is gone until one TTL past its expiry, to explain lookups."""
        self._tombstones[token] = _Tombstone(
            reason=reason, expires_at=expires_at, forget_at=expires_at + self._ttl, note=note
        )
        while len(self._tombstones) > self._max_entries:
            del self._tombstones[next(iter(self._tombstones))]

    @staticmethod
    def _record(token: str, entry: _Entry, payload: dict[str, Any]) -> PreviewRecord:
        return PreviewRecord(
            token=token,
            kind=entry.kind,
            account=entry.account,
            payload=payload,
            created_at=_to_datetime(entry.created_at),
            expires_at=_to_datetime(entry.expires_at),
        )


_MISMATCH_MESSAGE: Final = (
    "Preview token failed its integrity check: the stored order no longer matches "
    "what was previewed, so it was discarded. Run the preview again."
)


def _canonical_json(value: object) -> str:
    """Deterministic JSON: sorted keys, no whitespace, strict (no NaN, no custom types)."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _to_datetime(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)
