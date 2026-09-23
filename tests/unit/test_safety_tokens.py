"""PreviewStore: single use, expiry, integrity binding and lookup errors."""

from __future__ import annotations

import string
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from ib_gateway_mcp.errors import (
    SafetyError,
    TokenExpiredError,
    TokenMismatchError,
    TokenNotFoundError,
)
from ib_gateway_mcp.safety import PreviewRecord, PreviewStore, PreviewToken

ACCOUNT = "DU1234567"
START = 1_790_000_000.0
TTL = 120.0


class FakeClock:
    """Manually advanced epoch-seconds clock."""

    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_payload() -> dict[str, Any]:
    return {
        "contract": {"symbol": "AAPL", "sec_type": "STK", "exchange": "SMART"},
        "order": {"action": "BUY", "quantity": 10, "order_type": "LMT", "limit_price": 150.25},
        "tags": ["a", "b"],
    }


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> PreviewStore:
    return PreviewStore(ttl=TTL, secret=b"test-secret", clock=clock)


def issue(store: PreviewStore, kind: str = "order") -> str:
    return store.issue(make_payload(), ACCOUNT, kind).token


# -- issue ------------------------------------------------------------------


def test_issue_returns_urlsafe_token_and_utc_expiry(store: PreviewStore) -> None:
    preview = store.issue(make_payload(), ACCOUNT, "order")

    assert isinstance(preview, PreviewToken)
    assert len(preview.token) == 32  # token_urlsafe(24) -> 32 chars
    assert set(preview.token) <= set(string.ascii_letters + string.digits + "-_")
    assert preview.expires_at == datetime.fromtimestamp(START + TTL, UTC)
    assert preview.expires_at.tzinfo is UTC
    assert len(store) == 1


def test_issue_generates_distinct_tokens(store: PreviewStore) -> None:
    tokens = {issue(store) for _ in range(50)}
    assert len(tokens) == 50


def test_issue_keeps_a_deep_copy_of_the_payload(store: PreviewStore) -> None:
    payload = make_payload()
    token = store.issue(payload, ACCOUNT, "order").token
    payload["order"]["quantity"] = 10_000
    payload["tags"].append("mutated")

    assert store.consume(token) == make_payload()


def test_issue_normalizes_payload_to_plain_json(store: PreviewStore) -> None:
    token = store.issue({"legs": (1, 2), "nested": {"x": None}}, ACCOUNT, "combo").token
    assert store.consume(token) == {"legs": [1, 2], "nested": {"x": None}}


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"when": datetime(2026, 1, 1, tzinfo=UTC)}, TypeError),
        ({"obj": object()}, TypeError),
        ({"price": float("nan")}, ValueError),
        ({"price": float("inf")}, ValueError),
    ],
)
def test_issue_rejects_non_json_payloads(
    store: PreviewStore, payload: dict[str, Any], error: type[Exception]
) -> None:
    with pytest.raises(error):
        store.issue(payload, ACCOUNT, "order")
    assert len(store) == 0


@pytest.mark.parametrize(("account", "kind"), [("", "order"), (ACCOUNT, "")])
def test_issue_requires_account_and_kind(store: PreviewStore, account: str, kind: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        store.issue(make_payload(), account, kind)


# -- consume ----------------------------------------------------------------


def test_consume_returns_the_stored_payload(store: PreviewStore) -> None:
    token = issue(store)
    assert store.consume(token) == make_payload()
    assert len(store) == 0


def test_consume_record_returns_account_kind_and_times(
    store: PreviewStore, clock: FakeClock
) -> None:
    token = issue(store, kind="bracket")
    clock.advance(5)

    record = store.consume_record(token)

    assert isinstance(record, PreviewRecord)
    assert record.token == token
    assert record.account == ACCOUNT
    assert record.kind == "bracket"
    assert record.payload == make_payload()
    assert record.created_at == datetime.fromtimestamp(START, UTC)
    assert record.expires_at == datetime.fromtimestamp(START + TTL, UTC)


def test_token_is_single_use(store: PreviewStore) -> None:
    token = issue(store)
    store.consume(token)

    with pytest.raises(TokenNotFoundError, match="already used"):
        store.consume(token)
    with pytest.raises(TokenNotFoundError, match="already used"):
        store.peek(token)


def test_a_discarded_token_says_nothing_was_sent(store: PreviewStore) -> None:
    token = issue(store)
    record = store.discard(token, note="the human   declined\nthe live order")
    assert record.kind == "order"
    assert record.payload == make_payload()
    for lookup in (store.consume, store.peek, store.discard):
        with pytest.raises(
            TokenNotFoundError,
            match=r"discarded without sending anything \(the human declined the live order\)",
        ) as caught:
            lookup(token)
        assert "already used" not in str(caught.value)
    assert len(store) == 0


def test_discard_note_is_optional_and_bounded(store: PreviewStore) -> None:
    plain = issue(store)
    store.discard(plain)
    with pytest.raises(TokenNotFoundError, match=r"discarded without sending anything\. Run"):
        store.peek(plain)
    long = issue(store)
    store.discard(long, note="x" * 1_000)
    with pytest.raises(TokenNotFoundError) as caught:
        store.peek(long)
    assert "x" * 200 in str(caught.value)
    assert "x" * 201 not in str(caught.value)


def test_discard_checks_the_token_like_consume(store: PreviewStore, clock: FakeClock) -> None:
    with pytest.raises(TokenNotFoundError, match="Unknown preview token"):
        store.discard("no-such-token")
    token = issue(store)
    clock.advance(TTL)
    with pytest.raises(TokenExpiredError):
        store.discard(token)


def test_unknown_token(store: PreviewStore) -> None:
    issue(store)
    for bogus in ("", "not-a-token", "x" * 32):
        with pytest.raises(TokenNotFoundError, match="Unknown preview token"):
            store.consume(bogus)


def test_tokens_do_not_cross_stores(clock: FakeClock) -> None:
    first = PreviewStore(secret=b"k", clock=clock)
    second = PreviewStore(secret=b"k", clock=clock)
    token = issue(first)
    with pytest.raises(TokenNotFoundError):
        second.consume(token)


# -- expiry -----------------------------------------------------------------


def test_token_valid_until_just_before_expiry(store: PreviewStore, clock: FakeClock) -> None:
    token = issue(store)
    clock.advance(TTL - 0.001)
    assert store.consume(token) == make_payload()


def test_token_expires_at_ttl(store: PreviewStore, clock: FakeClock) -> None:
    token = issue(store)
    clock.advance(TTL)

    with pytest.raises(TokenExpiredError, match=r"expired at .*valid for 120 s") as excinfo:
        store.consume(token)
    assert datetime.fromtimestamp(START + TTL, UTC).isoformat() in str(excinfo.value)
    assert len(store) == 0


def test_expired_error_survives_purge_by_other_calls(store: PreviewStore, clock: FakeClock) -> None:
    token = issue(store)
    clock.advance(TTL + 1)
    issue(store)  # purges the expired entry

    with pytest.raises(TokenExpiredError):
        store.consume(token)
    with pytest.raises(TokenExpiredError):
        store.peek(token)


def test_expired_token_is_forgotten_after_one_more_ttl(
    store: PreviewStore, clock: FakeClock
) -> None:
    token = issue(store)
    clock.advance(2 * TTL)
    with pytest.raises(TokenNotFoundError, match="Unknown preview token"):
        store.consume(token)


def test_used_token_tombstone_is_forgotten_eventually(
    store: PreviewStore, clock: FakeClock
) -> None:
    token = issue(store)
    store.consume(token)
    clock.advance(2 * TTL)
    with pytest.raises(TokenNotFoundError, match="Unknown preview token"):
        store.consume(token)


def test_purge_expired_counts_and_len(store: PreviewStore, clock: FakeClock) -> None:
    issue(store)
    clock.advance(60)
    issue(store)
    assert len(store) == 2

    clock.advance(60)  # first one reaches its TTL
    assert store.purge_expired() == 1
    assert len(store) == 1
    assert store.purge_expired() == 0


def test_datetime_clocks_are_supported() -> None:
    now = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    aware = PreviewStore(ttl=TTL, clock=lambda: now)
    naive = PreviewStore(ttl=TTL, clock=lambda: now.replace(tzinfo=None))

    for store in (aware, naive):
        preview = store.issue(make_payload(), ACCOUNT, "order")
        assert preview.expires_at == now + timedelta(seconds=TTL)


# -- peek -------------------------------------------------------------------


def test_peek_does_not_consume(store: PreviewStore) -> None:
    token = issue(store, kind="combo")

    first = store.peek(token)
    second = store.peek(token)

    assert first == second
    assert first.kind == "combo"
    assert first.account == ACCOUNT
    assert first.payload == make_payload()
    assert store.consume(token) == make_payload()


def test_peek_returns_a_copy(store: PreviewStore) -> None:
    token = issue(store)
    peeked = store.peek(token)
    peeked.payload["order"]["quantity"] = 999

    assert store.consume(token)["order"]["quantity"] == 10


def test_peek_expired(store: PreviewStore, clock: FakeClock) -> None:
    token = issue(store)
    clock.advance(TTL)
    with pytest.raises(TokenExpiredError):
        store.peek(token)


# -- integrity --------------------------------------------------------------


def _set_quantity(store: PreviewStore, token: str) -> None:
    store._entries[token].payload["order"]["quantity"] = 1_000


def _add_payload_key(store: PreviewStore, token: str) -> None:
    store._entries[token].payload["extra"] = True


def _non_json_payload(store: PreviewStore, token: str) -> None:
    store._entries[token].payload["order"] = object()


def _swap_account(store: PreviewStore, token: str) -> None:
    store._entries[token].account = "U1234567"


def _swap_kind(store: PreviewStore, token: str) -> None:
    store._entries[token].kind = "cancel_all"


def _extend_expiry(store: PreviewStore, token: str) -> None:
    store._entries[token].expires_at += 3_600


def _forge_digest(store: PreviewStore, token: str) -> None:
    store._entries[token].digest = bytes(32)


TAMPERS = [
    _set_quantity,
    _add_payload_key,
    _non_json_payload,
    _swap_account,
    _swap_kind,
    _extend_expiry,
    _forge_digest,
]


@pytest.mark.parametrize("tamper", TAMPERS, ids=lambda f: f.__name__.lstrip("_"))
def test_tampered_entry_is_rejected_and_discarded(store: PreviewStore, tamper: Any) -> None:
    token = issue(store)
    tamper(store, token)

    with pytest.raises(TokenMismatchError, match="integrity check"):
        store.consume(token)
    assert len(store) == 0
    # Still refused afterwards: the entry is gone for good.
    with pytest.raises(TokenMismatchError):
        store.consume(token)


@pytest.mark.parametrize("tamper", TAMPERS[:2], ids=lambda f: f.__name__.lstrip("_"))
def test_peek_detects_tampering(store: PreviewStore, tamper: Any) -> None:
    token = issue(store)
    tamper(store, token)
    with pytest.raises(TokenMismatchError):
        store.peek(token)
    with pytest.raises(TokenMismatchError):
        store.consume(token)


def test_entry_moved_to_another_token_is_rejected(store: PreviewStore) -> None:
    first = issue(store)
    second = store.issue({"order": "other"}, ACCOUNT, "order").token
    store._entries[second] = store._entries[first]

    with pytest.raises(TokenMismatchError):
        store.consume(second)
    assert store.consume(first) == make_payload()


def test_digest_is_bound_to_the_secret(clock: FakeClock) -> None:
    source = PreviewStore(secret="alpha", clock=clock)
    same_key = PreviewStore(secret=b"alpha", clock=clock)
    other_key = PreviewStore(secret="beta", clock=clock)
    token = issue(source)

    same_key._entries[token] = source._entries[token]
    other_key._entries[token] = source._entries[token]

    assert same_key.consume(token) == make_payload()
    with pytest.raises(TokenMismatchError):
        other_key.consume(token)


# -- capacity ---------------------------------------------------------------


def test_oldest_preview_is_evicted_when_full(clock: FakeClock) -> None:
    store = PreviewStore(ttl=TTL, clock=clock, max_entries=2)
    first, second, third = issue(store), issue(store), issue(store)

    assert len(store) == 2
    with pytest.raises(TokenNotFoundError, match="more than 2 previews were pending"):
        store.consume(first)
    assert store.consume(second) == make_payload()
    assert store.consume(third) == make_payload()


# -- construction -----------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"ttl": 0}, "ttl must be positive"),
        ({"ttl": -1}, "ttl must be positive"),
        ({"ttl": float("nan")}, "ttl must be positive"),
        ({"secret": b""}, "secret must not be empty"),
        ({"secret": ""}, "secret must not be empty"),
        ({"max_entries": 0}, "max_entries"),
    ],
)
def test_invalid_construction(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        PreviewStore(**kwargs)


def test_ttl_property(store: PreviewStore) -> None:
    assert store.ttl == TTL


def test_from_settings_uses_the_ttl_and_a_random_key(clock: FakeClock) -> None:
    store = PreviewStore.from_settings(SimpleNamespace(token_ttl=30), clock=clock)
    other = PreviewStore.from_settings(SimpleNamespace(token_ttl=30), clock=clock)
    assert store.ttl == 30
    assert store._key != other._key
    token = issue(store)
    assert store.consume(token) == make_payload()


def test_token_errors_are_safety_errors() -> None:
    for error in (TokenExpiredError, TokenNotFoundError, TokenMismatchError):
        assert issubclass(error, SafetyError)
