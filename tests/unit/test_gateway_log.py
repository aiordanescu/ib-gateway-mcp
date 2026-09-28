"""_gateway_log: launcher.log parsing, the time rules, the directory reader and the hints.

The fixtures in ``tests/data/gateway_log/`` are scrubbed excerpts of real IB Gateway logs
(IPs 192.0.2.x, hosts ``server.example``, the user directory 40 x "a", dates shifted back),
re-zoned to Asia/Kolkata (UTC+05:30) so they cover a positive half-hour offset. Lines were
thinned, so the gaps between them aren't realistic: every test passes an explicit ``now``
and mtime. ``rejected_unrecognized.log`` is synthetic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ib_gateway_mcp import _gateway_log
from ib_gateway_mcp._gateway_log import (
    DETAILS,
    IDLE_AFTER,
    MAX_FILES,
    PATTERNS,
    PRELOGIN_SILENCE,
    STALL_AFTER,
    TWOFA_SILENCE,
    GatewayLog,
    describe,
    is_silent,
    login_state_from_lines,
)
from ib_gateway_mcp.models import LoginPhase, LoginState

DATA = Path(__file__).resolve().parents[1] / "data" / "gateway_log"
IST = timedelta(hours=5, minutes=30)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SECOND = timedelta(seconds=1)
READER_THREADS = "ib-gateway-mcp-gateway-log"

RESTART = "------------------------------- IB GATEWAY RESTART --------------------------------"
LOGIN = (
    "LauncherLoginThread.runLoginWithUI()[hotBackupStrategy=ON,loginAttempt=1,socketDelay=20000]."
)
AUTH_IB_KEY = (
    " NS_AUTH_START version=52 pwd=1 soft=0 token(s)=[token=5, tokenSubtype=2, suffix=a[IB Key]]"
    " initialTokenType=PWD authenticationType=MARKET_DATA_CONNECTION identityHashcode=1"
)
AUTH_PASSWORD = (
    " NS_AUTH_START version=52 pwd=1 soft=0 token(s)=[token=0] initialTokenType=PWD"
    " authenticationType=MARKET_DATA_CONNECTION identityHashcode=1"
)
CHALLENGE = "Received CHALLENGE"
TWOFA_ENDED = (
    "Authorization failed: SUPPRESS_MESSAGE_BOX. MessageType: null. FailReason: 2147483647"
)
WRONG_PASSWORD = "AUTH_RESULT indicates wrong password"
NOISE = "instance of control is not created yet"

# Text that appears in the fixtures and must never reach a model, a hint or the log.
LOG_TOKENS = (
    "192.0.2.",
    "server.example",
    "a" * 40,
    "XXXXX",
    "userDirname",
    "NOT PRINTED",
    "autorestart",
    "/home/ibgateway",
    "tws_settings",
    "Asia/Kolkata",
    "SUPPRESS_MESSAGE_BOX",
    "FailReason",
    "Synthetic rejection",
    "00:00:5E:00:53:01",
    "00000000.0000",
    "MISC51",
)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def line(local: str, message: str) -> str:
    """A launcher.log line at a local wall-clock time ("2026-11-01 00:50:00.000")."""
    return f"{local} INFO  [JTS-Test-1] - {message}"


def fixture(name: str) -> list[str]:
    return (DATA / name).read_text(encoding="utf-8").splitlines()


def last_line_utc(lines: list[str]) -> datetime:
    """The last timestamped line, as UTC (the fixtures are at UTC+05:30)."""
    stamps = [text[:23] for text in lines if re.match(r"\d{4}-\d\d-\d\d ", text)]
    return (datetime.fromisoformat(stamps[-1]) - IST).replace(tzinfo=UTC)


def at_end(lines: list[str], after: timedelta = SECOND) -> LoginState:
    """The state ``after`` the last line, with the file's mtime at its last line."""
    last = last_line_utc(lines)
    return login_state_from_lines(lines, now=last + after, log_mtime=last)


def install(directory: Path, name: str, lines: list[str], mtime: datetime | None = None) -> None:
    """Write a launcher file and set its mtime (default: its last line)."""
    path = directory / name
    path.write_text("".join(f"{text}\n" for text in lines), encoding="utf-8")
    set_mtime(path, mtime or last_line_utc(lines))


def set_mtime(path: Path, when: datetime) -> None:
    ns = (when - EPOCH) // timedelta(microseconds=1) * 1000
    os.utime(path, ns=(ns, ns))


def shifted(lines: list[str], days: int) -> list[str]:
    """The fixture lines moved ``days`` later (timestamped lines only)."""
    out = []
    for text in lines:
        if re.match(r"\d{4}-\d\d-\d\d ", text):
            moved = datetime.fromisoformat(text[:23]) + timedelta(days=days)
            out.append(moved.isoformat(sep=" ", timespec="milliseconds") + text[23:])
        else:
            out.append(text)
    return out


def join_reader_threads() -> None:
    for thread in threading.enumerate():
        if thread.name.startswith(READER_THREADS):
            thread.join(timeout=5)
            assert not thread.is_alive()


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.fixture
def rotation(tmp_path: Path) -> Path:
    """The rotation pair: the rejection in the rotated file, noise in launcher.log."""
    for name in ("launcher.20260831.log", "launcher.log"):
        install(tmp_path, name, fixture(f"rotation/{name}"))
    return tmp_path


# --- every fixture ------------------------------------------------------------------------


@dataclass(frozen=True)
class Expected:
    phase: LoginPhase
    since: str
    detail: str | None
    attempts: int
    challenges: int
    counted_since: str
    complete: bool
    retry_at: str | None = None


EXPECTED = {
    "2fa_approved.log": Expected(
        LoginPhase.LOGGED_IN,
        "2026-08-29 19:12:14.441",
        DETAILS["second_factor"].format(t="19:12:14 UTC"),
        1,
        1,
        "2026-08-29 19:10:58.441",
        False,
    ),
    "2fa_ended_no_challenge.log": Expected(
        LoginPhase.LOGGING_IN,
        "2026-07-19 12:15:21.652",
        DETAILS["ended_before_challenge_retry"],
        1,
        1,
        "2026-07-19 12:10:23.833",
        False,
    ),
    "2fa_finish_not_authenticated.log": Expected(
        LoginPhase.LOGGING_IN,
        "2026-07-19 10:26:23.540",
        DETAILS["not_completed"],
        1,
        0,
        "2026-07-19 10:25:26.799",
        False,
    ),
    "2fa_timeout_retry.log": Expected(
        LoginPhase.AWAITING_2FA,
        "2026-08-29 22:45:32.511",
        DETAILS["ib_key_push"],
        2,
        2,
        "2026-08-29 22:36:09.043",
        False,
    ),
    "auth_timeout_retries.log": Expected(
        LoginPhase.AWAITING_2FA,
        "2026-08-29 22:52:00.940",
        DETAILS["ib_key_push"],
        3,
        1,
        "2026-08-29 22:50:06.215",
        False,
    ),
    "cold_start.log": Expected(
        LoginPhase.LOGGED_IN,
        "2026-08-31 16:52:00.432",
        DETAILS["password"],
        1,
        0,
        "2026-08-31 16:45:44.266",
        False,
    ),
    "competing_session.log": Expected(
        LoginPhase.LOGGED_IN,
        "2026-08-28 16:59:50.944",
        DETAILS["second_factor"].format(t="16:59:50 UTC"),
        1,
        1,
        "2026-08-28 16:59:01.327",
        False,
    ),
    "login_not_started.log": Expected(
        LoginPhase.LOGIN_IDLE,
        "2026-08-27 01:26:42.182",
        DETAILS["never_attempted"],
        0,
        0,
        "2026-08-27 01:26:42.182",
        False,
    ),
    "rejected_unrecognized.log": Expected(
        LoginPhase.LOGIN_REJECTED,
        "2026-08-31 16:52:00.349",
        DETAILS["unrecognized"],
        1,
        0,
        "2026-08-31 16:51:58.473",
        False,
    ),
    "throttled.log": Expected(
        LoginPhase.THROTTLED,
        "2026-08-29 23:26:04.521",
        DETAILS["throttled_2fa"],
        1,
        1,
        "2026-08-29 23:18:16.694",
        False,
        retry_at="2026-08-29 23:30:59.521",
    ),
    # AUTH_RESULT (…04.000) comes 2 ms before "Authorization failed: Invalid username…".
    "token_rejected_stuck.log": Expected(
        LoginPhase.LOGIN_REJECTED,
        "2026-08-31 05:30:04.000",
        DETAILS["saved_session_refused"],
        1,
        0,
        "2026-08-31 05:30:02.213",
        False,
    ),
    # Starts with the previous JVM's success, so the counts are complete.
    "warm_restart_success.log": Expected(
        LoginPhase.LOGGED_IN,
        "2026-08-29 05:30:03.920",
        DETAILS["saved_session"],
        1,
        0,
        "2026-08-28 17:01:37.184",
        True,
    ),
}


def test_every_fixture_has_expectations() -> None:
    assert sorted(p.name for p in DATA.glob("*.log")) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_state_one_second_after_its_last_line(name: str) -> None:
    lines = fixture(name)
    expected = EXPECTED[name]
    state = at_end(lines)
    assert state.phase == expected.phase
    assert state.since == utc(expected.since)
    assert state.detail == expected.detail
    assert state.retry_at == (utc(expected.retry_at) if expected.retry_at else None)
    assert state.login_attempts == expected.attempts
    assert state.twofa_challenges == expected.challenges
    assert state.counted_since == utc(expected.counted_since)
    assert state.counts_complete is expected.complete
    assert state.log_updated_at == last_line_utc(lines)


def fixture_lines() -> list[str]:
    """Every timestamped line of every fixture."""
    return [
        text
        for path in sorted(DATA.rglob("*.log"))
        for text in path.read_text(encoding="utf-8").splitlines()
        if re.match(r"\d{4}-\d\d-\d\d ", text)
    ]


def test_every_pattern_matches_a_fixture_line() -> None:
    messages = [text.split(" - ", 1)[1].strip() for text in fixture_lines()]
    for key, pattern in PATTERNS.items():
        search = key in _gateway_log._ANCHORS and _gateway_log._ANCHORS[key][0]
        find = pattern.search if search else pattern.match
        assert any(find(message) for message in messages), key


@pytest.mark.parametrize("kind", sorted(_gateway_log._ANCHORS))
def test_every_anchor_kind_gives_the_fixtures_offset(kind: str) -> None:
    search, _to_utc = _gateway_log._ANCHORS[kind]
    pattern = PATTERNS[kind]
    find = pattern.search if search else pattern.match
    hits = [text for text in fixture_lines() if find(text.split(" - ", 1)[1].strip())]
    assert hits, kind
    for text in hits:
        parsed = _gateway_log._parse([text])
        anchors = [event for event in parsed.events if event.kind == _gateway_log._ANCHOR]
        assert [event.offset for event in anchors] == [IST], text


# --- the time rules -----------------------------------------------------------------------


def test_twofa_ended_after_a_challenge_waits_for_the_retry_then_idles() -> None:
    lines = fixture("throttled.log")
    lines = lines[: next(i for i, t in enumerate(lines) if "SUPPRESS_MESSAGE_BOX" in t) + 1]
    ended = last_line_utc(lines)
    waiting = login_state_from_lines(lines, now=ended + IDLE_AFTER, log_mtime=ended)
    assert waiting.phase == LoginPhase.LOGGING_IN
    assert waiting.detail == DETAILS["twofa_ended_retry"]
    assert waiting.since == ended
    idle = login_state_from_lines(lines, now=ended + IDLE_AFTER + SECOND, log_mtime=ended)
    assert idle.phase == LoginPhase.LOGIN_IDLE
    assert idle.detail == DETAILS["twofa_ended"]
    assert idle.since == ended


def test_twofa_ended_without_a_challenge_idles_after_90_seconds() -> None:
    lines = fixture("2fa_ended_no_challenge.log")
    since = utc("2026-07-19 12:15:21.652")
    state = login_state_from_lines(
        lines, now=since + IDLE_AFTER + SECOND, log_mtime=last_line_utc(lines)
    )
    assert state.phase == LoginPhase.LOGIN_IDLE
    assert state.detail == DETAILS["ended_before_challenge"]
    assert state.since == since


def test_throttle_idles_90_seconds_after_the_wait_ends() -> None:
    lines = fixture("throttled.log")
    retry_at = utc("2026-08-29 23:30:59.521")
    mtime = last_line_utc(lines)
    before = login_state_from_lines(lines, now=retry_at + IDLE_AFTER, log_mtime=mtime)
    assert before.phase == LoginPhase.THROTTLED
    assert before.retry_at == retry_at
    after = login_state_from_lines(lines, now=retry_at + IDLE_AFTER + SECOND, log_mtime=mtime)
    assert after.phase == LoginPhase.LOGIN_IDLE
    assert after.detail == DETAILS["throttle_over"]
    assert after.since == retry_at
    assert after.retry_at is None


def test_a_start_without_a_login_idles_after_90_seconds() -> None:
    lines = fixture("login_not_started.log")
    lines = lines[: next(i for i, t in enumerate(lines) if "Started on" in t) + 1]
    started = utc("2026-08-27 01:26:42.182")
    mtime = last_line_utc(lines)
    fresh = login_state_from_lines(lines, now=started + IDLE_AFTER, log_mtime=mtime)
    assert (fresh.phase, fresh.since, fresh.detail) == (LoginPhase.RESTARTING, started, None)
    idle = login_state_from_lines(lines, now=started + IDLE_AFTER + SECOND, log_mtime=mtime)
    assert (idle.phase, idle.detail) == (LoginPhase.LOGIN_IDLE, DETAILS["never_attempted"])


def test_a_warm_restart_is_restarting_with_complete_counts() -> None:
    lines = fixture("warm_restart_success.log")
    lines = lines[: next(i for i, t in enumerate(lines) if "auto-restart mode" in t) + 1]
    state = at_end(lines)
    assert state.phase == LoginPhase.RESTARTING
    assert state.detail == DETAILS["warm"]
    assert state.since == utc("2026-08-29 05:30:02.112")
    assert state.counts_complete is True
    assert state.counted_since == utc("2026-08-28 17:01:37.184")
    assert (state.login_attempts, state.twofa_challenges) == (0, 0)


def test_a_login_attempt_stalls_after_stall_after() -> None:
    lines = fixture("2fa_finish_not_authenticated.log")
    last_event = utc("2026-07-19 10:26:28.975")  # the retry's NS_AUTH_START
    now = last_event + STALL_AFTER
    going = login_state_from_lines(lines, now=now, log_mtime=now - SECOND)
    assert going.phase == LoginPhase.LOGGING_IN
    now += timedelta(milliseconds=1)
    stalled = login_state_from_lines(lines, now=now, log_mtime=now - SECOND)
    assert stalled.phase == LoginPhase.LOGIN_IDLE
    assert stalled.detail == DETAILS["stalled"]
    assert stalled.since == last_event
    assert DETAILS["stalled"].endswith(f"for over {STALL_AFTER // timedelta(minutes=1)} minutes)")


def test_a_silent_log_while_awaiting_2fa_is_unknown_after_90_seconds() -> None:
    lines = fixture("2fa_timeout_retry.log")
    mtime = last_line_utc(lines)
    live = login_state_from_lines(lines, now=mtime + TWOFA_SILENCE, log_mtime=mtime)
    assert live.phase == LoginPhase.AWAITING_2FA
    gone = login_state_from_lines(lines, now=mtime + TWOFA_SILENCE + SECOND, log_mtime=mtime)
    assert gone.phase == LoginPhase.UNKNOWN
    assert gone.since == mtime
    assert gone.detail == DETAILS["silent"].format(t="22:46:12 UTC")
    assert gone.login_attempts == 2  # the counts survive the staleness rule


def test_a_silent_log_before_a_login_is_unknown_after_7_minutes() -> None:
    lines = fixture("token_rejected_stuck.log")
    mtime = last_line_utc(lines)
    live = login_state_from_lines(lines, now=mtime + PRELOGIN_SILENCE, log_mtime=mtime)
    assert live.phase == LoginPhase.LOGIN_REJECTED
    gone = login_state_from_lines(lines, now=mtime + PRELOGIN_SILENCE + SECOND, log_mtime=mtime)
    assert gone.phase == LoginPhase.UNKNOWN


def test_is_silent_only_for_the_staleness_rule(tmp_path: Path) -> None:
    lines = fixture("2fa_timeout_retry.log")
    mtime = last_line_utc(lines)
    silent = login_state_from_lines(lines, now=mtime + TWOFA_SILENCE + SECOND, log_mtime=mtime)
    assert is_silent(silent)
    awaiting = login_state_from_lines(lines, now=mtime + SECOND, log_mtime=mtime)
    no_start = login_state_from_lines([], now=mtime, log_mtime=mtime)
    no_file = GatewayLog(tmp_path).read()
    dir_unreadable = GatewayLog(tmp_path / "missing").read()
    assert [no_start.detail, no_file.detail, dir_unreadable.detail] == [
        DETAILS["no_start"],
        DETAILS["no_file"],
        DETAILS["dir_unreadable"].format(reason="No such file or directory"),
    ]
    for state in (awaiting, no_start, no_file, dir_unreadable):
        assert not is_silent(state), state


def test_logged_in_is_never_stale() -> None:
    lines = fixture("warm_restart_success.log")
    mtime = last_line_utc(lines)
    now = mtime + timedelta(days=3)
    state = login_state_from_lines(lines, now=now, log_mtime=mtime)
    assert state.phase == LoginPhase.LOGGED_IN
    assert "shows a successful login at 2026-08-29 05:30:03 UTC" in describe(state, now)


def test_noise_only_with_fresh_idle_noise_is_login_idle() -> None:
    lines = fixture("rotation/launcher.log")
    state = at_end(lines)
    assert state.phase == LoginPhase.LOGIN_IDLE
    assert state.since == utc("2026-08-31 07:00:07.589")  # the oldest line read
    assert state.detail == DETAILS["idle_since"].format(t="07:00:07 UTC")
    assert state.login_attempts is None
    assert state.counts_complete is False


def test_noise_only_with_stale_idle_noise_is_unknown() -> None:
    lines = fixture("rotation/launcher.log")
    noise = utc("2026-08-31 07:05:07.613")
    now = noise + PRELOGIN_SILENCE + SECOND
    state = login_state_from_lines(lines, now=now, log_mtime=now - SECOND)
    assert state.phase == LoginPhase.UNKNOWN
    assert state.detail == DETAILS["no_start"]
    assert state.login_attempts is None


def test_no_lines_is_unknown() -> None:
    state = login_state_from_lines([], now=utc("2026-09-01 00:00:00"), log_mtime=None)
    assert (state.phase, state.since, state.detail) == (
        LoginPhase.UNKNOWN,
        None,
        DETAILS["no_start"],
    )


def test_counts_restart_after_a_success_but_not_at_a_restart_inside_a_failing_sequence() -> None:
    lines = [
        line("2026-09-01 10:00:00.000", "Switching to a new encrypted log..."),
        line("2026-09-01 11:00:00.000", RESTART),
        line("2026-09-01 11:00:01.000", "Started on 20260901-11:00:01"),
        line("2026-09-01 11:00:02.000", AUTH_IB_KEY),
        line("2026-09-01 11:00:02.100", CHALLENGE),
        line("2026-09-01 11:10:00.000", TWOFA_ENDED),
        line("2026-09-01 11:10:05.000", RESTART),
        line("2026-09-01 11:10:06.000", AUTH_IB_KEY),
        line("2026-09-01 11:10:06.100", CHALLENGE),
    ]
    state = login_state_from_lines(
        lines, now=utc("2026-09-01 11:10:07.1"), log_mtime=utc("2026-09-01 11:10:06.1")
    )
    assert (state.phase, state.since) == (LoginPhase.AWAITING_2FA, utc("2026-09-01 11:10:06.1"))
    assert (state.login_attempts, state.twofa_challenges) == (2, 2)
    assert state.counts_complete is True
    assert state.counted_since == utc("2026-09-01 10:00:00")


def test_a_login_thread_starting_during_a_challenge_keeps_awaiting_2fa() -> None:
    lines = [
        line("2026-09-01 12:00:00.000", RESTART),
        line("2026-09-01 12:00:01.000", AUTH_IB_KEY),
        line("2026-09-01 12:00:01.200", CHALLENGE),
        line("2026-09-01 12:00:05.000", LOGIN),
    ]
    state = login_state_from_lines(lines, now=utc("2026-09-01 12:00:06"), log_mtime=None)
    assert (state.phase, state.since) == (LoginPhase.AWAITING_2FA, utc("2026-09-01 12:00:01.2"))


def test_a_success_alone_says_nothing_about_how() -> None:
    lines = fixture("warm_restart_success.log")[:1]  # the previous JVM's log switch only
    state = at_end(lines)
    assert (state.phase, state.detail) == (LoginPhase.LOGGED_IN, None)
    assert (state.login_attempts, state.counts_complete) == (0, False)


def test_throttle_with_hours_minutes_and_seconds() -> None:
    lines = [
        line(
            "2026-09-01 12:00:00.000",
            "Too many failed login attempts. Please wait 1 hour & 2 minutes & 3 seconds "
            "before attempting to re-login again.",
        )
    ]
    now = utc("2026-09-01 12:00:01")
    state = login_state_from_lines(lines, now=now, log_mtime=utc("2026-09-01 12:00:00"))
    assert state.phase == LoginPhase.THROTTLED
    assert state.retry_at == utc("2026-09-01 13:02:03")
    assert state.detail == DETAILS["throttled"]


def test_a_rejection_stays_rejected_across_later_messages() -> None:
    lines = [
        line("2026-09-01 12:00:00.000", RESTART),
        line("2026-09-01 12:00:01.000", AUTH_PASSWORD),
        line("2026-09-01 12:00:02.000", WRONG_PASSWORD),
        line("2026-09-01 12:00:02.001", "Authorization failed: Something else. FailReason: 7"),
        line(
            "2026-09-01 12:00:02.002",
            "### onAuthenticationCompleted tokenProtocol=0; authenticated=false",
        ),
    ]
    state = login_state_from_lines(lines, now=utc("2026-09-01 12:01"), log_mtime=None)
    assert state.phase == LoginPhase.LOGIN_REJECTED
    assert state.detail == DETAILS["credentials"]
    assert state.since == utc("2026-09-01 12:00:02")


# --- time zones -----------------------------------------------------------------------------


def test_dst_change_between_two_gateway_starts() -> None:
    """UTC-7 until the first JVM ends, UTC-8 after the second starts (local clock goes back)."""
    epoch_ms = (utc("2026-11-01 09:02:02.050") - EPOCH) // timedelta(milliseconds=1)
    lines = [
        line("2026-11-01 00:50:00.000", RESTART),
        line("2026-11-01 00:50:01.000", "Started on 20261101-07:50:01"),
        line("2026-11-01 00:50:02.000", LOGIN),
        line("2026-11-01 00:50:02.100", AUTH_IB_KEY),
        line("2026-11-01 00:50:02.300", CHALLENGE),
        line("2026-11-01 01:05:00.000", TWOFA_ENDED),
        # 02:00 PDT became 01:00 PST: the next start is later in UTC, earlier on the clock.
        line("2026-11-01 01:02:00.000", RESTART),
        line("2026-11-01 01:02:01.000", "Started on 20261101-09:02:01"),
        line("2026-11-01 01:02:02.000", LOGIN),
        line("2026-11-01 01:02:02.050", f"### startLogin startTime={epoch_ms}"),
        line("2026-11-01 01:02:02.100", AUTH_IB_KEY),
        line("2026-11-01 01:02:02.300", CHALLENGE),
        line("2026-11-01 01:02:22.300", "Sent NS_HEART_BEAT:#%#%0000MISC51;531;20261101-09:02:22;"),
    ]
    mtime = utc("2026-11-01 09:02:22.300")
    state = login_state_from_lines(lines, now=utc("2026-11-01 09:02:30"), log_mtime=mtime)
    assert state.phase == LoginPhase.AWAITING_2FA
    assert state.since == utc("2026-11-01 09:02:02.300")
    assert state.counted_since == utc("2026-11-01 07:50:00")
    assert (state.login_attempts, state.twofa_challenges) == (2, 2)

    first_jvm = login_state_from_lines(
        lines[:6], now=utc("2026-11-01 08:05:30"), log_mtime=utc("2026-11-01 08:05:00")
    )
    assert (first_jvm.phase, first_jvm.since) == (LoginPhase.LOGGING_IN, utc("2026-11-01 08:05"))
    # The RESTART line precedes its segment's first anchor and uses it (UTC-8, not UTC-7).
    starting = login_state_from_lines(
        lines[:8], now=utc("2026-11-01 09:02:30"), log_mtime=utc("2026-11-01 09:02:01")
    )
    assert (starting.phase, starting.since) == (LoginPhase.RESTARTING, utc("2026-11-01 09:02"))


def test_a_segment_without_anchors_uses_the_previous_offset() -> None:
    lines = [
        line("2026-09-01 12:00:00.000", RESTART),
        line("2026-09-01 12:00:01.000", "Started on 20260901-06:30:01"),
        line("2026-09-01 13:00:00.000", RESTART),
        line("2026-09-01 13:00:01.000", AUTH_PASSWORD),
        line("2026-09-01 13:00:02.000", WRONG_PASSWORD),
    ]
    state = login_state_from_lines(lines, now=utc("2026-09-01 07:31:00"), log_mtime=None)
    assert state.since == utc("2026-09-01 07:30:02")


def test_segments_without_anchors_borrow_the_previous_else_the_next_offset() -> None:
    local = datetime(2026, 9, 1, 12)
    hour = timedelta(hours=1)

    def segment(*offsets: timedelta) -> list[_gateway_log._Event]:
        anchors = [_gateway_log._Event(local, _gateway_log._ANCHOR, offset=o) for o in offsets]
        return [_gateway_log._Event(local, "restart"), *anchors]

    events = [
        *segment(),  # none before: the next anchored segment's first offset
        *segment(),
        *segment(hour, 2 * hour),
        *segment(),  # the previous segment's last offset
        *segment(3 * hour),
        *segment(),
    ]
    fallback = timedelta(minutes=45)
    assert _gateway_log._resolve_offsets(events, fallback) == [
        hour,
        hour,
        hour,
        hour,
        2 * hour,
        2 * hour,
        3 * hour,
        3 * hour,
        3 * hour,
    ]
    assert _gateway_log._resolve_offsets(segment()[:1] * 3, fallback) == [fallback] * 3


def test_offsets_take_linear_time_in_anchorless_segments() -> None:
    # One segment per minimal RESTART line (62 bytes) of a file at the byte cap; a scan per
    # segment for the next anchor took minutes here.
    restart = _gateway_log._Event(datetime(2026, 9, 1), "restart")
    events = [restart] * (_gateway_log.MAX_FILE_BYTES // 62)
    started = time.monotonic()
    offsets = _gateway_log._resolve_offsets(events, IST)
    assert time.monotonic() - started < 2
    assert offsets == [IST] * len(events)


def test_without_anchors_the_offset_comes_from_the_mtime() -> None:
    lines = [
        line("2026-09-01 12:00:00.000", RESTART),
        line("2026-09-01 12:00:01.000", AUTH_PASSWORD),
        line("2026-09-01 12:00:02.000", WRONG_PASSWORD),
    ]
    mtime = utc("2026-09-01 06:30:02.001")
    state = login_state_from_lines(lines, now=mtime + SECOND, log_mtime=mtime)
    assert state.since == utc("2026-09-01 06:30:02")
    # With neither an anchor nor a plausible mtime, the local time is taken as UTC.
    bare = login_state_from_lines(lines, now=utc("2026-09-01 12:00:03"), log_mtime=None)
    assert bare.since == utc("2026-09-01 12:00:02")
    far = utc("2026-09-02 08:00:02")  # 20 h off
    assert login_state_from_lines(lines, now=far, log_mtime=far).since == bare.since


def test_without_anchors_the_offset_pairs_a_file_with_its_own_mtime(tmp_path: Path) -> None:
    lines = [
        line("2026-09-01 12:00:00.000", RESTART),
        line("2026-09-01 12:00:01.000", AUTH_PASSWORD),
        line("2026-09-01 12:00:02.000", WRONG_PASSWORD),
    ]
    install(tmp_path, "launcher.20260901.log", lines)  # mtime: its last line at UTC+05:30
    now = utc("2026-09-01 06:31:00")
    install(tmp_path, "launcher.log", [], mtime=now)  # just rotated, still empty
    state = GatewayLog(tmp_path).read(now=now)
    assert state.phase == LoginPhase.LOGIN_REJECTED
    assert state.since == utc("2026-09-01 06:30:02")
    assert state.log_updated_at == now


def test_implausible_anchors_are_ignored() -> None:
    lines = [
        line("2026-09-01 12:00:00.000", RESTART),
        line("2026-09-01 12:00:01.000", "Started on 20260902-08:00:01"),  # 20 h off
        line("2026-09-01 12:00:01.500", "Started on 20261399-99:00:01"),  # not a date
        line("2026-09-01 12:00:02.000", "### startLogin startTime=0000000000000"),
        line("2026-09-01 12:00:03.000", WRONG_PASSWORD),
    ]
    state = login_state_from_lines(lines, now=utc("2026-09-01 12:00:04"), log_mtime=None)
    assert state.since == utc("2026-09-01 12:00:03")


def test_legacy_two_letter_tag_and_continuation_lines() -> None:
    lines = [
        f"2021-03-01 10:00:00.000 [AB] INFO  [JTS-Main] - {RESTART}",
        "2021-03-01 10:00:01.000 [AB] INFO  [JTS-Main] - Started on 20210301-04:30:01",
        CHALLENGE,  # continuation lines carry no timestamp and are ignored
        "Switching to a new encrypted log...",
        "",
    ]
    state = login_state_from_lines(lines, now=utc("2021-03-01 04:30:30"), log_mtime=None)
    assert (state.phase, state.since) == (LoginPhase.RESTARTING, utc("2021-03-01 04:30:00"))


def test_naive_now_is_taken_as_utc() -> None:
    lines = fixture("throttled.log")
    last = last_line_utc(lines)
    aware = login_state_from_lines(lines, now=last + SECOND, log_mtime=last)
    naive = login_state_from_lines(
        lines, now=(last + SECOND).replace(tzinfo=None), log_mtime=last.replace(tzinfo=None)
    )
    assert aware == naive


# --- the directory reader -------------------------------------------------------------------


def test_the_rotation_pair_is_read_together(rotation: Path) -> None:
    mtime = last_line_utc(fixture("rotation/launcher.log"))
    state = GatewayLog(rotation).read(now=mtime + SECOND)
    assert state.phase == LoginPhase.LOGIN_REJECTED
    assert state.since == utc("2026-08-31 05:30:04")
    assert state.detail == DETAILS["saved_session_refused"]
    assert (state.login_attempts, state.counts_complete) == (1, False)
    assert state.log_updated_at == mtime


def test_a_rejection_before_two_noise_only_days_is_still_rejected(tmp_path: Path) -> None:
    noise = fixture("rotation/launcher.log")
    install(tmp_path, "launcher.20260831.log", fixture("rotation/launcher.20260831.log"))
    install(tmp_path, "launcher.20260901.log", shifted(noise, 1))
    install(tmp_path, "launcher.log", shifted(noise, 2))
    now = last_line_utc(shifted(noise, 2)) + SECOND
    state = GatewayLog(tmp_path).read(now=now)
    assert state.phase == LoginPhase.LOGIN_REJECTED
    assert state.since == utc("2026-08-31 05:30:04")
    assert "at 2026-08-31 05:30:04 UTC" in describe(state, now)


def test_read_uses_the_current_time_by_default(rotation: Path) -> None:
    state = GatewayLog(rotation).read()
    assert state.phase == LoginPhase.UNKNOWN  # the fixture's mtime is long past
    assert state.detail is not None
    assert state.detail.startswith("the gateway's launcher.log has been silent since 2026-08-31")


def test_missing_directory(tmp_path: Path) -> None:
    state = GatewayLog(tmp_path / "missing").read()
    assert state.phase == LoginPhase.UNKNOWN
    assert state.detail == DETAILS["dir_unreadable"].format(reason="No such file or directory")


def test_empty_directory(tmp_path: Path) -> None:
    state = GatewayLog(tmp_path).read()
    assert (state.phase, state.detail) == (LoginPhase.UNKNOWN, DETAILS["no_file"])


def test_other_files_are_never_opened(tmp_path: Path) -> None:
    install(tmp_path, "launcher.old.log", fixture("cold_start.log"))
    install(tmp_path, "ibgateway.20260831.log", fixture("cold_start.log"))
    install(tmp_path, "launcher.log.1", fixture("cold_start.log"))
    state = GatewayLog(tmp_path).read()
    assert (state.phase, state.detail) == (LoginPhase.UNKNOWN, DETAILS["no_file"])


def test_a_symlink_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "launcher.log").symlink_to(DATA / "cold_start.log")
    state = GatewayLog(tmp_path).read()
    assert (state.phase, state.detail) == (LoginPhase.UNKNOWN, DETAILS["no_regular_file"])
    install(tmp_path, "launcher.20260831.log", fixture("cold_start.log"))
    now = last_line_utc(fixture("cold_start.log")) + SECOND
    assert GatewayLog(tmp_path).read(now=now).phase == LoginPhase.LOGGED_IN


def test_a_directory_named_like_the_log_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "launcher.log").mkdir()
    install(tmp_path, "launcher.20260831.log", fixture("cold_start.log"))
    now = last_line_utc(fixture("cold_start.log")) + SECOND
    assert GatewayLog(tmp_path).read(now=now).phase == LoginPhase.LOGGED_IN


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
async def test_a_fifo_is_skipped_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "launcher.log")
    install(tmp_path, "launcher.20260831.log", fixture("cold_start.log"))
    started = time.monotonic()
    state = await GatewayLog(tmp_path).read_async(5)
    join_reader_threads()
    assert time.monotonic() - started < 4
    assert state.phase == LoginPhase.LOGGED_IN  # from the rotated file, read after the FIFO


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix domain sockets")
def test_a_socket_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Opening a socket fails with ENXIO on Linux and EOPNOTSUPP on macOS. A relative name
    # keeps the address within sun_path's length limit (104 bytes on macOS).
    monkeypatch.chdir(tmp_path)
    install(tmp_path, "launcher.20260831.log", fixture("cold_start.log"))
    now = last_line_utc(fixture("cold_start.log")) + SECOND
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind("launcher.log")
        state = GatewayLog(tmp_path).read(now=now)
    finally:
        server.close()
    assert state.phase == LoginPhase.LOGGED_IN  # from the rotated file, read after the socket


@pytest.mark.parametrize("suffix", ["\n", "\nx"])
async def test_a_name_with_a_trailing_newline_is_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    for name in ("launcher.log", "launcher.20260831.log"):
        install(tmp_path, name + suffix, fixture("cold_start.log"))
    opened: list[str] = []
    real_open = os.open

    def spy(path: str | os.PathLike[str], flags: int, *args: int, **kwargs: int) -> int:
        opened.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    state = GatewayLog(tmp_path).read()
    monkeypatch.undo()
    assert opened == []
    assert (state.phase, state.detail) == (LoginPhase.UNKNOWN, DETAILS["no_file"])
    problems = await GatewayLog(tmp_path).check_async(5)
    join_reader_threads()
    assert any("has no launcher.log" in problem for problem in problems)


@pytest.mark.parametrize(
    ("ghost", "phase"),
    [
        # Gone since the listing and older than everything read: nothing is lost.
        ("launcher.20260101.log", LoginPhase.LOGIN_REJECTED),
        # Between two files: the older one would be folded across the gap, so the read
        # stops at the gap (launcher.log alone holds no gateway start).
        ("launcher.20260901.log", LoginPhase.LOGIN_IDLE),
    ],
)
def test_a_file_that_vanishes_after_the_listing_is_skipped(
    rotation: Path, monkeypatch: pytest.MonkeyPatch, ghost: str, phase: LoginPhase
) -> None:
    listdir = os.listdir

    def with_ghost(path: str | os.PathLike[str]) -> list[str]:
        names = listdir(path)
        return [*names, ghost] if Path(path) == rotation else names

    monkeypatch.setattr(os, "listdir", with_ghost)
    now = last_line_utc(fixture("rotation/launcher.log")) + SECOND
    assert GatewayLog(rotation).read(now=now).phase == phase


@pytest.mark.skipif(is_root(), reason="root reads files regardless of their mode")
def test_an_unreadable_newest_file_is_unknown(rotation: Path) -> None:
    (rotation / "launcher.log").chmod(0)
    try:
        state = GatewayLog(rotation).read()
    finally:
        (rotation / "launcher.log").chmod(0o644)
    assert state.phase == LoginPhase.UNKNOWN
    assert state.detail == DETAILS["file_unreadable"].format(
        name="launcher.log", reason="Permission denied"
    )


@pytest.mark.skipif(is_root(), reason="root reads files regardless of their mode")
def test_an_unreadable_older_file_ends_the_read(rotation: Path) -> None:
    (rotation / "launcher.20260831.log").chmod(0)
    try:
        state = GatewayLog(rotation).read(now=last_line_utc(fixture("rotation/launcher.log")))
    finally:
        (rotation / "launcher.20260831.log").chmod(0o644)
    assert state.phase == LoginPhase.LOGIN_IDLE  # launcher.log alone: noise only


def test_the_newest_files_are_read_first_up_to_max_files(
    rotation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert MAX_FILES == 10
    older = shifted(fixture("rotation/launcher.20260831.log"), -1)
    install(rotation, "launcher.20260830.log", older)  # a day older, the same rejection
    now = last_line_utc(fixture("rotation/launcher.log")) + SECOND
    monkeypatch.setattr(_gateway_log, "MAX_FILES", 2)
    log = GatewayLog(rotation)
    state = log.read(now=now)
    # The oldest file is past MAX_FILES.
    assert sorted(log._cache) == ["launcher.20260831.log", "launcher.log"]
    assert (state.phase, state.login_attempts) == (LoginPhase.LOGIN_REJECTED, 1)
    assert state.counted_since == utc("2026-08-31 05:30:02.213")
    monkeypatch.setattr(_gateway_log, "MAX_FILES", 3)
    assert GatewayLog(rotation).read(now=now).login_attempts == 2  # within the cap, it counts
    monkeypatch.setattr(_gateway_log, "MAX_FILES", 1)
    assert GatewayLog(rotation).read(now=now).phase == LoginPhase.LOGIN_IDLE


def test_the_total_byte_cap_stops_at_older_files(
    rotation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = last_line_utc(fixture("rotation/launcher.log")) + SECOND
    newest = (rotation / "launcher.log").stat().st_size
    monkeypatch.setattr(_gateway_log, "MAX_TOTAL_BYTES", newest)
    assert GatewayLog(rotation).read(now=now).phase == LoginPhase.LOGIN_IDLE
    monkeypatch.setattr(_gateway_log, "MAX_TOTAL_BYTES", newest + 2000)  # the rotated tail
    state = GatewayLog(rotation).read(now=now)
    assert state.phase == LoginPhase.LOGIN_IDLE
    assert state.since is not None
    assert state.since < utc("2026-08-31 07:00")  # the oldest line read is in the rotated file


@pytest.mark.parametrize("extra", [0, 5])
def test_the_file_cap_reads_the_tail_from_a_whole_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: int
) -> None:
    lines = [
        line("2026-09-01 12:00:00.000", RESTART),
        line("2026-09-01 12:00:01.000", AUTH_PASSWORD),
        line("2026-09-01 12:00:02.000", WRONG_PASSWORD),
        *(line(f"2026-09-01 12:0{minute}:00.000", NOISE) for minute in range(1, 7)),
    ]
    install(tmp_path, "launcher.log", lines, mtime=utc("2026-09-01 12:06:00"))
    width = len(lines[-1]) + 1
    monkeypatch.setattr(_gateway_log, "MAX_FILE_BYTES", 3 * width + extra)
    state = GatewayLog(tmp_path).read(now=utc("2026-09-01 12:06:01"))
    assert state.phase == LoginPhase.LOGIN_IDLE  # the rejection is outside the tail
    assert state.since == utc("2026-09-01 12:04")  # the third line from the end, whole


def ist_lines(first: datetime, messages: list[str]) -> list[str]:
    """Lines one minute apart from ``first`` (UTC), on the fixtures' UTC+05:30 clock."""
    return [
        line(f"{first + IST + timedelta(minutes=n):%Y-%m-%d %H:%M:%S}.000", text)
        for n, text in enumerate(messages)
    ]


def cap_to_last(monkeypatch: pytest.MonkeyPatch, lines: list[str], count: int) -> datetime:
    """Cap each file to the bytes of the last ``count`` lines; the first one's time (UTC)."""
    tail = lines[-count:]
    monkeypatch.setattr(_gateway_log, "MAX_FILE_BYTES", sum(len(f"{t}\n".encode()) for t in tail))
    return last_line_utc(tail[:1])


def test_a_file_cut_by_the_cap_is_the_oldest_one_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    success = fixture("cold_start.log")  # logged in at 2026-08-31 16:52 UTC
    install(tmp_path, "launcher.20260831.log", success)
    noise = ist_lines(utc("2026-09-01 00:00"), [NOISE] * 8)
    install(tmp_path, "launcher.log", noise)
    tail = cap_to_last(monkeypatch, noise, 3)
    log = GatewayLog(tmp_path)
    state = log.read(now=last_line_utc(noise) + SECOND)
    assert sorted(log._cache) == ["launcher.log"]
    # Not the older file's success: its end and the tail's start aren't adjacent.
    assert (state.phase, state.since) == (LoginPhase.LOGIN_IDLE, tail)


def test_counts_in_a_cut_file_are_a_lower_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, "launcher.20260831.log", fixture("cold_start.log"))
    rounds = ist_lines(utc("2026-09-01 00:00"), [RESTART, AUTH_PASSWORD, WRONG_PASSWORD] * 4)
    install(tmp_path, "launcher.log", rounds)
    tail = cap_to_last(monkeypatch, rounds, 6)  # the last two rounds, from their RESTART
    state = GatewayLog(tmp_path).read(now=last_line_utc(rounds) + SECOND)
    assert (state.phase, state.detail) == (LoginPhase.LOGIN_REJECTED, DETAILS["credentials"])
    assert (state.login_attempts, state.counts_complete) == (2, False)
    assert state.counted_since == tail


def test_a_last_line_is_parsed_only_once_its_newline_is_written(tmp_path: Path) -> None:
    lines = fixture("2fa_timeout_retry.log")
    ended = next(i for i, text in enumerate(lines) if "SUPPRESS_MESSAGE_BOX" in text)
    at = last_line_utc(lines[: ended + 1])
    install(tmp_path, "launcher.log", lines[:ended], mtime=at)
    cut = lines[ended].index("SUPPRESS") + len("SUPP")
    path = tmp_path / "launcher.log"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(lines[ended][:cut])  # "Authorization failed: SUPP" would read as rejected
    log = GatewayLog(tmp_path)
    assert log.read(now=at + SECOND).phase == LoginPhase.AWAITING_2FA
    with path.open("a", encoding="utf-8") as handle:
        handle.write(lines[ended][cut:] + "\n")
    state = log.read(now=at + SECOND)
    assert (state.phase, state.detail) == (LoginPhase.LOGGING_IN, DETAILS["twofa_ended_retry"])


def test_a_file_of_one_unterminated_line_reads_as_an_empty_file(tmp_path: Path) -> None:
    mtime = utc("2026-09-01 12:00:00")
    install(tmp_path, "launcher.log", [], mtime=mtime)
    empty = GatewayLog(tmp_path).read(now=mtime + SECOND)
    assert (empty.phase, empty.detail) == (LoginPhase.UNKNOWN, DETAILS["no_start"])
    path = tmp_path / "launcher.log"
    path.write_text(line("2026-09-01 12:00:00.000", RESTART), encoding="utf-8")
    set_mtime(path, mtime)
    assert GatewayLog(tmp_path).read(now=mtime + SECOND) == empty


def test_parsed_files_are_cached_by_name_and_the_cache_is_bounded(
    rotation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parses: list[int] = []
    parse = _gateway_log._parse

    def counting(lines: list[str]) -> object:
        parses.append(1)
        return parse(lines)

    monkeypatch.setattr(_gateway_log, "_parse", counting)
    install(rotation, "launcher.20260830.log", shifted(fixture("rotation/launcher.log"), -1))
    log = GatewayLog(rotation)
    log.read()
    assert len(parses) == 3
    log.read()
    assert len(parses) == 3  # nothing changed, nothing parsed
    with (rotation / "launcher.log").open("a", encoding="utf-8") as handle:
        handle.write(line("2026-08-31 12:40:07.000", NOISE) + "\n")
    log.read()
    assert len(parses) == 4  # only the file that grew
    (rotation / "launcher.20260830.log").unlink()
    log.read()
    assert sorted(log._cache) == ["launcher.20260831.log", "launcher.log"]


def test_read_never_raises(
    rotation: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_args: object, **_kwargs: object) -> LoginState:
        raise ValueError(f"line text from {rotation}")

    monkeypatch.setattr(_gateway_log, "_evaluate", broken)
    with caplog.at_level(logging.WARNING, logger="ib_gateway_mcp._gateway_log"):
        state = GatewayLog(rotation).read()
    assert (state.phase, state.detail) == (LoginPhase.UNKNOWN, DETAILS["failed"])
    assert "ValueError" in caplog.text
    assert "line text" not in caplog.text
    assert str(rotation) not in caplog.text


def test_read_range_stops_at_the_end_of_the_file(tmp_path: Path) -> None:
    path = tmp_path / "data"
    path.write_bytes(b"0123456789")
    fd = os.open(path, os.O_RDONLY)
    try:
        assert _gateway_log._read_range(fd, 5, 100) == b"56789"
    finally:
        os.close(fd)


# --- read_async and check_async -------------------------------------------------------------


@pytest.fixture
def release() -> Iterator[threading.Event]:
    """An event the blocked fakes wait on, set (and the threads joined) at the end."""
    event = threading.Event()
    yield event
    event.set()
    join_reader_threads()


def blocked_read(
    release: threading.Event, calls: list[int], started: Callable[[], object] = lambda: None
) -> Callable[..., LoginState]:
    """A stand-in for GatewayLog.read that blocks until ``release`` is set."""

    def read(now: datetime | None = None) -> LoginState:
        calls.append(1)
        started()
        release.wait(5)
        return LoginState(phase=LoginPhase.LOGGED_IN)

    return read


async def test_read_async_reads_on_a_daemon_thread(rotation: Path) -> None:
    log = GatewayLog(rotation)
    assert await log.read_async(5) == log.read()
    join_reader_threads()


async def test_read_async_times_out_then_refuses_while_the_read_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release: threading.Event
) -> None:
    log = GatewayLog(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(log, "read", blocked_read(release, calls))
    first = await log.read_async(0.05)
    assert (first.phase, first.detail) == (
        LoginPhase.UNKNOWN,
        DETAILS["timeout"].format(timeout="0.05"),
    )
    started = time.monotonic()
    second = await log.read_async(5)
    assert time.monotonic() - started < 1  # at once, not after another timeout
    assert (second.phase, second.detail) == (LoginPhase.UNKNOWN, DETAILS["busy"])
    release.set()
    join_reader_threads()
    third = await log.read_async(5)
    join_reader_threads()
    assert third.phase == LoginPhase.LOGGED_IN
    assert len(calls) == 2


async def test_concurrent_read_async_calls_share_one_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release: threading.Event
) -> None:
    log = GatewayLog(tmp_path)
    calls: list[int] = []
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    read = blocked_read(release, calls, lambda: loop.call_soon_threadsafe(started.set))
    monkeypatch.setattr(log, "read", read)
    first = asyncio.create_task(log.read_async(5))
    await asyncio.wait_for(started.wait(), 5)
    second = asyncio.create_task(log.read_async(5))
    await asyncio.sleep(0)  # the second call joins the running read
    release.set()
    results = await asyncio.gather(first, second)
    join_reader_threads()
    assert [r.phase for r in results] == [LoginPhase.LOGGED_IN, LoginPhase.LOGGED_IN]
    assert len(calls) == 1


async def test_read_async_turns_a_failure_into_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    log = GatewayLog(tmp_path)

    def broken(now: datetime | None = None) -> LoginState:
        raise RuntimeError(f"secret {tmp_path}")

    monkeypatch.setattr(log, "read", broken)
    with caplog.at_level(logging.WARNING, logger="ib_gateway_mcp._gateway_log"):
        state = await log.read_async(5)
    join_reader_threads()
    assert (state.phase, state.detail) == (LoginPhase.UNKNOWN, DETAILS["failed"])
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


async def test_read_async_when_no_thread_can_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NoThread(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    log = GatewayLog(tmp_path)
    monkeypatch.setattr(threading, "Thread", NoThread)
    state = await log.read_async(5)
    assert (state.phase, state.detail) == (LoginPhase.UNKNOWN, DETAILS["failed"])


@pytest.fixture
def read_only(rotation: Path) -> Iterator[Path]:
    rotation.chmod(0o555)
    yield rotation
    rotation.chmod(0o755)


@pytest.mark.skipif(is_root(), reason="root may write to any directory")
async def test_check_async_accepts_a_read_only_directory(read_only: Path) -> None:
    assert await GatewayLog(read_only).check_async(5) == []
    join_reader_threads()


async def test_check_async_reports_start_up_problems(tmp_path: Path) -> None:
    problems = await GatewayLog(tmp_path).check_async(5)
    join_reader_threads()
    assert any("has no launcher.log" in problem for problem in problems)
    assert any("mount it read-only" in problem for problem in problems)
    missing = await GatewayLog(tmp_path / "missing").check_async(5)
    join_reader_threads()
    assert len(missing) == 1
    assert "cannot read" in missing[0]
    assert missing[0].endswith(": No such file or directory")


async def test_check_async_times_out_then_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release: threading.Event
) -> None:
    log = GatewayLog(tmp_path)

    def blocked() -> list[str]:
        release.wait(5)
        return []

    monkeypatch.setattr(log, "_check", blocked)
    first = await log.check_async(0.05)
    assert len(first) == 1
    assert "timed out after 0.05 s" in first[0]
    second = await log.check_async(5)
    assert len(second) == 1
    assert "has not finished" in second[0]


async def test_check_async_turns_a_failure_into_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = GatewayLog(tmp_path)

    def broken() -> list[str]:
        raise OSError("secret")

    monkeypatch.setattr(log, "_check", broken)
    problems = await log.check_async(5)
    join_reader_threads()
    assert problems == [f"checking the gateway settings directory {tmp_path} failed (OSError)"]


# --- privacy --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_no_log_text_reaches_the_model_the_hint_or_the_log(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    lines = fixture(name)
    outputs = []
    with caplog.at_level(logging.DEBUG, logger="ib_gateway_mcp._gateway_log"):
        for after in (SECOND, IDLE_AFTER * 2, PRELOGIN_SILENCE * 2, timedelta(days=2)):
            state = at_end(lines, after)
            now = last_line_utc(lines) + after
            outputs += [state.model_dump_json(), describe(state, now)]
    outputs.append(caplog.text)
    for output in outputs:
        for token in LOG_TOKENS:
            assert token not in output, (token, output)


def test_no_log_text_or_path_in_a_directory_read(
    rotation: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="ib_gateway_mcp._gateway_log"):
        states = [
            GatewayLog(rotation).read(),
            GatewayLog(rotation / "missing").read(),
            GatewayLog(rotation / "launcher.log").read(),  # not a directory
        ]
    outputs = [caplog.text]
    for state in states:
        outputs += [state.model_dump_json(), describe(state, datetime.now(UTC))]
    for output in outputs:
        for token in (*LOG_TOKENS, str(rotation)):
            assert token not in output, (token, output)


# --- describe -------------------------------------------------------------------------------

NOW = utc("2026-09-01 12:00:00")


def hint(**fields: object) -> str:
    return describe(LoginState.model_validate(fields), NOW)


def test_describe_awaiting_2fa() -> None:
    assert hint(
        phase="awaiting_2fa",
        since=NOW - timedelta(seconds=30),
        detail=DETAILS["ib_key_push"],
        twofa_challenges=2,
        counts_complete=True,
    ) == (
        "The gateway is waiting for two-factor approval: IBKR sent an IB Key push to IBKR "
        "Mobile at 11:59:30 UTC (2 challenges since the last successful login). The account "
        "holder must approve it; an unanswered challenge ends after about 4 to 15 minutes, and "
        "the gateway may then send a new one."
    )
    other = hint(phase="awaiting_2fa", since=NOW, detail=DETAILS["challenge"], twofa_challenges=1)
    assert (
        "IBKR sent a second-factor challenge at 12:00:00 UTC (at least 1 challenge in this" in other
    )
    assert "(" not in hint(phase="awaiting_2fa", since=NOW)


def test_describe_throttled() -> None:
    assert hint(
        phase="throttled", retry_at=NOW + timedelta(minutes=5), detail=DETAILS["throttled_2fa"]
    ) == (
        "The gateway is pausing logins after repeated failures until 12:05:00 UTC; nothing "
        "needs to be done before then. After the pause it logs in again only if its login "
        "automation is set to retry (ib-gateway-docker: RELOGIN_AFTER_TWOFA_TIMEOUT=yes); that "
        "login will send a new second-factor challenge, so the account holder should be ready "
        "to approve it. If nothing retries, the phase turns login_idle and the operator needs "
        "to restart the gateway container."
    )
    assert hint(phase="throttled", detail=DETAILS["throttled"]) == (
        "The gateway is pausing logins after repeated failures until a set time; nothing needs "
        "to be done before then. After the pause it logs in again only if its login automation "
        "is set to retry (ib-gateway-docker: RELOGIN_AFTER_TWOFA_TIMEOUT=yes). If nothing "
        "retries, the phase turns login_idle and the operator needs to restart the gateway "
        "container."
    )


def test_describe_a_login_ended_promises_no_retry() -> None:
    since = NOW - timedelta(seconds=10)
    assert hint(phase="logging_in", since=since, detail=DETAILS["twofa_ended_retry"]) == (
        "The second-factor challenge ended unanswered at 11:59:50 UTC. The gateway logs in "
        "again within seconds only if its login automation is set to retry (ib-gateway-docker: "
        "RELOGIN_AFTER_TWOFA_TIMEOUT=yes); check again in a minute. If nothing retries, the "
        "phase turns login_idle and the operator needs to restart the gateway container."
    )
    before = hint(phase="logging_in", since=since, detail=DETAILS["ended_before_challenge_retry"])
    assert before.startswith(
        "IBKR ended the gateway's login before any second-factor challenge at 11:59:50 UTC. The "
        "gateway logs in again within seconds only if its login automation is set to retry"
    )
    for detail in ("twofa_ended_retry", "ended_before_challenge_retry"):
        assert DETAILS[detail].endswith("only if the gateway's login automation is set to retry")


def test_describe_login_rejected() -> None:
    since = NOW - timedelta(days=1)
    refused = hint(phase="login_rejected", since=since, detail=DETAILS["saved_session_refused"])
    assert refused.startswith("IBKR rejected the gateway's login at 2026-08-31 12:00:00 UTC: the")
    assert "(a cold restart). Approving a 2FA prompt won't help." in refused
    credentials = hint(phase="login_rejected", since=NOW, detail=DETAILS["credentials"])
    assert credentials.startswith("IBKR rejected the gateway's username or password at 12:00")
    assert "check the credentials" in credentials
    other = hint(phase="login_rejected", detail=DETAILS["unrecognized"])
    assert other.startswith("IBKR rejected the gateway's login at an unknown time for a reason")
    assert "over VNC" in other


def test_describe_login_idle() -> None:
    never = hint(phase="login_idle", since=NOW, detail=DETAILS["never_attempted"])
    assert never.startswith("The gateway started at 12:00:00 UTC but its login automation never")
    assert "restarting alone may not fix it" in never
    assert hint(phase="login_idle", detail=DETAILS["throttle_over"]) == (
        "The gateway is running but not logged in, and nothing is retrying: the pause after "
        "failed logins ended and no new attempt followed. The operator needs to restart the "
        "gateway container."
    )
    assert "nothing is retrying: no login is in progress." in hint(phase="login_idle")


def test_describe_restarting() -> None:
    assert hint(phase="restarting", since=NOW, detail=DETAILS["warm"]) == (
        "The gateway started at 12:00:00 UTC (daily auto-restart) and is about to log in; this "
        "normally takes under a minute."
    )
    assert "(daily" not in hint(phase="restarting", since=NOW)


def test_describe_logging_in() -> None:
    assert hint(phase="logging_in", login_attempts=2, detail=DETAILS["no_answer"]) == (
        "The gateway is logging in to IBKR (IBKR did not answer in time; the gateway retries); "
        "check again in a minute."
    )
    assert hint(phase="logging_in") == (
        "The gateway is logging in to IBKR; check again in a minute."
    )
    since = NOW - timedelta(hours=1)
    assert hint(
        phase="logging_in", login_attempts=26, counted_since=since, counts_complete=True
    ) == (
        "IBKR has not completed the gateway's login: 26 attempts since the last successful "
        "login at 11:00:00 UTC; the gateway keeps retrying."
    )
    assert "at least 3 attempts since 11:00:00 UTC;" in hint(
        phase="logging_in", login_attempts=3, counted_since=since
    )
    assert "at least 3 attempts since the oldest line read;" in hint(
        phase="logging_in", login_attempts=3
    )


def test_describe_logged_in() -> None:
    assert hint(phase="logged_in", since=NOW - timedelta(seconds=30)) == (
        "The gateway logged in at 11:59:30 UTC and is starting its API; check again in a minute."
    )
    older = hint(phase="logged_in", since=NOW - timedelta(minutes=2))
    assert older.startswith("The gateway's launcher.log shows a successful login at 11:58:00 UTC")
    # One text for a refused connection and for a handshake nobody answers.
    assert "but its API refuses or doesn't answer connections." in older
    assert "the gateway may have stopped (a stopped gateway leaves this log unchanged) or hung" in (
        older
    )
    assert "IB_GATEWAY_SETTINGS_DIR may belong to another gateway." in older
    assert "at an unknown time" in hint(phase="logged_in")


def test_describe_unknown() -> None:
    assert hint(phase="unknown", detail=DETAILS["no_start"]) == (
        "No gateway start or login in launcher.log."
    )
    assert hint(phase="unknown") == "The gateway's login phase is unknown."


def all_hints() -> Iterator[str]:
    for name in EXPECTED:
        lines = fixture(name)
        for after in (SECOND, IDLE_AFTER * 2, timedelta(days=2)):
            yield describe(at_end(lines, after), last_line_utc(lines) + after)
    for phase in LoginPhase:
        for detail in (None, *DETAILS.values()):
            yield hint(phase=phase, detail=detail, login_attempts=5, since=NOW)


def test_no_hint_tells_the_reader_to_restart_anything() -> None:
    for text in all_hints():
        assert not re.search(r"(?:^|[.;:]\s+)Restart", text), text
        for match in re.finditer(r"(?i)\brestart\b", text):
            before = text[: match.start()]
            assert before.endswith(("needs to ", "credentials and ", "cold ", "auto-")), text


# --- the model ------------------------------------------------------------------------------


def test_login_phase_docstring_reaches_the_schema() -> None:
    schema = LoginState.model_json_schema()
    description = schema["$defs"]["LoginPhase"]["description"]
    for phase in LoginPhase:
        assert f"- {phase.value}: " in description
    assert "later events" in description
    for name, field in schema["properties"].items():
        assert field.get("description"), name
