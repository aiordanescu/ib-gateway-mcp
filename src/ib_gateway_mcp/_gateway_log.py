"""The gateway's login phase, read from IB Gateway's ``launcher.log``.

IB Gateway writes ``launcher.log`` into its settings directory (ib-gateway-docker's
``TWS_SETTINGS_PATH``) from the moment it starts until a login succeeds. Then it switches
to an encrypted log, and ``launcher.log`` stays silent until the next start. So the file
shows what happens *before* the API can answer: a start, a login in progress, a
second-factor challenge waiting for approval, a pause after failed logins, a rejection.
:class:`GatewayLog` reads it, :func:`login_state_from_lines` folds lines into a
:class:`~ib_gateway_mcp.models.LoginState`, and :func:`describe` words that state as a
``get_health`` hint.

The format is IBKR's, undocumented, and may change with any gateway build. Every pattern
lives in :data:`PATTERNS` with an example line, and whatever isn't recognized ends in
``unknown``.

**Fold.** Events are folded oldest first into a phase and the time it began. A login
*sequence* runs from one successful login to the next; ``login_attempts`` counts its
authentication rounds (``NS_AUTH_START``) and ``twofa_challenges`` its challenges. The counts
restart at the first start, login or round after a success, never at a restart inside a
failing sequence (retry loops span gateway restarts).

**Time rules**, applied at ``now``: a login that IBKR ended (an unanswered challenge) reads
as ``logging_in`` for :data:`IDLE_AFTER`, then as ``login_idle``: the gateway logs in again,
within seconds, only if its login automation is set to retry (ib-gateway-docker's
``RELOGIN_AFTER_TWOFA_TIMEOUT=yes``, which also ends a pause after failed logins with a new
login). A start with no login attempt for :data:`IDLE_AFTER`, a pause
:data:`IDLE_AFTER` past its wait and a login with no progress for :data:`STALL_AFTER` are
``login_idle`` too. Before a login the gateway writes every few minutes, so a log silent
for :data:`PRELOGIN_SILENCE` (:data:`TWOFA_SILENCE` while a challenge is pending) is
``unknown``: the gateway is probably not running. After a login it writes nothing, so
``logged_in`` never goes stale.

**Time.** Line timestamps are the gateway container's wall clock with no offset. Some lines
also carry UTC (``Started on``, heartbeats) or epoch milliseconds (``startTime=``...); the
difference, rounded to 15 minutes, gives the offset. Offsets are resolved per segment, a
segment being the lines from one ``IB GATEWAY RESTART`` to the next, so a zone or DST change
between gateway starts is handled without a timezone database. Lines before a segment's
first anchor use it; a segment without anchors borrows the previous segment's offset (else
the next one's); with no anchor at all, the newest file's last line against its mtime gives
the offset.

**Reading.** Newest file first, each from its last :data:`MAX_FILE_BYTES` at most and
:data:`MAX_TOTAL_BYTES` in all. A file cut by either cap is the oldest one read, so the lines
folded are one continuous stretch. A last line without its newline (one the gateway is still
writing) is read once it is complete.

**Privacy.** The log holds IP and MAC addresses, session ids, masked token prefixes and
hashes, a log upload key, the hash of the login's user directory and ad ids that encode a
user identifier. Nothing from a line leaves this module: not in a model, not in a hint, not
in this module's log records. Outputs are phases, times, counts and the fixed wording in
:data:`DETAILS`, plus an OS error's ``strerror`` and a file's base name.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import errno
import logging
import os
import re
import stat
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Final

from ib_gateway_mcp.models.ops import LoginPhase, LoginState

__all__ = [
    "DETAILS",
    "IDLE_AFTER",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "PATTERNS",
    "PRELOGIN_SILENCE",
    "STALL_AFTER",
    "TWOFA_SILENCE",
    "GatewayLog",
    "describe",
    "is_silent",
    "login_state_from_lines",
]

logger = logging.getLogger(__name__)

IDLE_AFTER: Final = timedelta(seconds=90)
"""How long a start, an ended challenge or an ended pause may go without a new attempt."""
TWOFA_SILENCE: Final = timedelta(seconds=90)
"""Longest silence while a challenge is pending (the gateway logs a heartbeat every 20 s)."""
PRELOGIN_SILENCE: Final = timedelta(minutes=7)
"""Longest silence before a login (the gateway logs something every 3 to 5 minutes)."""
STALL_AFTER: Final = timedelta(minutes=10)
"""How long a login attempt may go without any progress before it counts as stalled."""
_JUST_LOGGED_IN: Final = timedelta(minutes=2)

MAX_FILES: Final = 10
"""Newest ``launcher*.log`` files considered per read."""
MAX_TOTAL_BYTES: Final = 16 * 1024 * 1024
"""Bytes read per read, over all files."""
MAX_FILE_BYTES: Final = 8 * 1024 * 1024
"""Bytes read from the end of one file; a file cut to them is the oldest one read."""

_FILE_NAME: Final = re.compile(r"^launcher(?:\.(\d{8}))?\.log\Z")  # \Z: no trailing newline
_OPEN_FLAGS: Final = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
# Skipped: a symlink (ELOOP, from O_NOFOLLOW), a file that vanished since the listing
# (ENOENT), a socket (ENXIO on Linux, EOPNOTSUPP on macOS and the BSDs).
_SKIPPED_ERRNOS: Final = frozenset({errno.ELOOP, errno.ENOENT, errno.ENXIO, errno.EOPNOTSUPP})
_READ_CHUNK: Final = 1024 * 1024

# "2026-08-28 22:30:02.112 INFO  [JTS-Main] - message"; builds up to 10.30 add a two-letter
# tag: "2021-03-01 10:00:00.000 [AB] INFO  [JTS-Main] - message". Lines without a timestamp
# continue the previous line and are ignored.
_LINE: Final = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) (?:\[[A-Z]{2}\] )?[A-Z]+\s+\[[^\]]*\] - (.*)$"
)

PATTERNS: Final[Mapping[str, re.Pattern[str]]] = MappingProxyType(
    {
        # "------------------------------- IB GATEWAY RESTART --------------------------------"
        # A new gateway process: a container start, an IBC restart or the daily auto-restart.
        "restart": re.compile(r"^-{5,} IB GATEWAY RESTART -{5,}$"),
        # "Running in auto-restart mode: restoring session token and session ID
        #  [userDirname=...]..." The daily auto-restart, reusing the saved session.
        "warm": re.compile(r"^Running in auto-restart mode\b"),
        # "Started on 20260829-05:30:03" (UTC)
        "started_utc": re.compile(r"^Started on (\d{8}-\d\d:\d\d:\d\d)$"),
        # "### startLogin startTime=1787981403368" (epoch ms); also "socketOpenStart=",
        # "socketOpenEnd=", "loginMessageReq=" and SocketTracker's "lastReadTimestamp=".
        "epoch_anchor": re.compile(
            r"\b(?:startTime|socketOpenStart|socketOpenEnd|loginMessageReq|lastReadTimestamp)"
            r"=(\d{13})\b"
        ),
        # "Sent NS_HEART_BEAT:#%#%0000MISC51;531;20260719-12:10:23;" (UTC), every 20 s while
        # a second-factor challenge is pending.
        "heartbeat_utc": re.compile(r"^Sent NS_HEART_BEAT:.*;(\d{8}-\d\d:\d\d:\d\d);$"),
        # "LauncherLoginThread.runLoginWithUI()[hotBackupStrategy=ON,loginAttempt=1,
        #  socketDelay=20000]." One connection attempt.
        "login_start": re.compile(
            r"^LauncherLoginThread\.runLoginWithUI\(\)\[.*\bloginAttempt=(\d+)"
        ),
        # " NS_AUTH_START version=52 pwd=1 soft=0 token(s)=[token=5, tokenSubtype=2,
        #  suffix=a[IB Key]] initialTokenType=PWD authenticationType=MARKET_DATA_CONNECTION
        #  identityHashcode=1" (leading space). IBKR opened an authentication round; pwd=1
        # asks for the password, [IB Key] names the second factor, initialTokenType SOFT or
        # TST means the gateway brought a saved session.
        "auth_start": re.compile(
            r"^NS_AUTH_START version=\d+ pwd=(\d) soft=(\d+) token\(s\)=\[(.*)\] "
            r"initialTokenType=(\w+)"
        ),
        # "Received CHALLENGE": IBKR sent the second-factor challenge (an IB Key push).
        "challenge": re.compile(r"^Received CHALLENGE$"),
        # "Authentication timeout": IBKR did not answer in time; the gateway retries.
        "auth_timeout": re.compile(r"^Authentication timeout$"),
        # "SPLASH Attempt 13: network error, will retry in seconds"
        "auto_retry": re.compile(r"^SPLASH Attempt (\d+): (network|server) error, will retry"),
        # "### onAuthenticationCompleted tokenProtocol=5; authenticated=true"
        # (tokenProtocol 5: a second factor was used.)
        "completed": re.compile(
            r"^### onAuthenticationCompleted tokenProtocol=(\d+); authenticated=(true|false)$"
        ),
        # "AUTH_RESULT indicates wrong password", then "Authorization failed: Invalid username
        # or password. " with the rest of IBKR's message on continuation lines.
        "rejected_credentials": re.compile(
            r"^Authorization failed: Invalid username or password\b"
            r"|^AUTH_RESULT indicates wrong password$"
        ),
        # "Authorization failed: SUPPRESS_MESSAGE_BOX. MessageType: null. FailReason: 2147483647"
        # IBKR closed the login: an unanswered challenge ended, or it ended before one.
        "twofa_ended": re.compile(r"^Authorization failed: SUPPRESS_MESSAGE_BOX\."),
        # "Authorization failed: <any other reason>. MessageType: null. FailReason: 99"
        "rejected_other": re.compile(r"^Authorization failed: (?!SUPPRESS_MESSAGE_BOX)"),
        # "Too many failed login attempts. Please wait 4 minutes & 55 seconds before
        #  attempting to re-login again." A dialog the gateway shows after repeated failures
        # (no network I/O precedes it, so it seems to time the pause itself). Its login
        # automation logs in again after the wait only when set to retry.
        "throttle": re.compile(
            r"^Too many failed login attempts\. Please wait (?:(\d+) hours? & )?"
            r"(?:(\d+) minutes? & )?(\d+) seconds? before"
        ),
        # "Switching to a new encrypted log...": the login succeeded; the log goes silent.
        "logged_in": re.compile(r"^Switching to a new encrypted log"),
        # "instance of control is not created yet": every 5 minutes while not logged in.
        "idle_noise": re.compile(r"^instance of control is not created yet$"),
    }
)
"""Every pattern read from launcher.log, matched against a line's message, in match order."""


def _utc_text(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y%m%d-%H:%M:%S")
    except ValueError:
        return None


def _epoch_ms(text: str) -> datetime | None:
    milliseconds = int(text)
    return datetime(1970, 1, 1) + timedelta(milliseconds=milliseconds) if milliseconds else None


# The patterns that carry UTC: kind -> (searched anywhere in the message rather than matched
# at its start, group(1) as a naive UTC datetime or None). Every other pattern is an event.
_ANCHORS: Final[Mapping[str, tuple[bool, Callable[[str], datetime | None]]]] = MappingProxyType(
    {
        "started_utc": (False, _utc_text),
        "heartbeat_utc": (False, _utc_text),
        "epoch_anchor": (True, _epoch_ms),
    }
)
_EVENT_PATTERNS: Final = tuple((k, p) for k, p in PATTERNS.items() if k not in _ANCHORS)
_ANCHOR: Final = "anchor"
_IDLE_NOISE: Final = "idle_noise"

DETAILS: Final[Mapping[str, str]] = MappingProxyType(
    {
        # awaiting_2fa
        "ib_key_push": "IB Key push sent to IBKR Mobile",
        "challenge": "second-factor challenge sent",
        # throttled
        "throttled_2fa": (
            "pausing logins after repeated failures; any new login will send a new "
            "second-factor challenge"
        ),
        "throttled": "pausing logins after repeated failures",
        # login_rejected
        "saved_session_refused": (
            "IBKR refused the saved session of the daily auto-restart and asked for the password"
        ),
        "credentials": "IBKR rejected the username or password",
        "unrecognized": "IBKR rejected the login for a reason this server doesn't recognize",
        # restarting
        "warm": "daily auto-restart, reusing the saved session",
        # logging_in
        "twofa_ended_retry": (
            "the second-factor challenge ended unanswered; a new login follows only if the "
            "gateway's login automation is set to retry"
        ),
        "ended_before_challenge_retry": (
            "IBKR ended the login before any second-factor challenge; a new login follows "
            "only if the gateway's login automation is set to retry"
        ),
        "no_answer": "IBKR did not answer in time; the gateway retries",
        "not_completed": "IBKR ended the authentication without a login; the gateway retries",
        # login_idle
        "never_attempted": "no login attempt since the gateway started",
        "twofa_ended": "the second-factor challenge ended unanswered and no retry followed",
        "ended_before_challenge": (
            "IBKR ended the login before any second-factor challenge and no retry followed"
        ),
        "throttle_over": "the pause after failed logins ended and no new attempt followed",
        "stalled": (
            "the login attempt stalled (nothing about it was logged for over "
            f"{STALL_AFTER // timedelta(minutes=1)} minutes)"
        ),
        "idle_since": "it has been running without logging in since at least {t}",
        # logged_in
        "second_factor": "after a second-factor approval at {t}",
        "saved_session": "with the saved session (daily auto-restart)",
        "password": "with the password (no second factor)",
        # unknown
        "silent": (
            "the gateway's launcher.log has been silent since {t}; the gateway process is "
            "probably not running, or hung"
        ),
        "no_start": "no gateway start or login in launcher.log",
        "no_file": "no launcher.log in the settings directory",
        "no_regular_file": (
            "no launcher.log in the settings directory is a regular file (symlinks and pipes "
            "are not read)"
        ),
        "dir_unreadable": "cannot read the settings directory: {reason}",
        "file_unreadable": "cannot read {name}: {reason}",
        "busy": (
            "the previous read of the settings directory has not finished (slow or hung file "
            "system)"
        ),
        "timeout": (
            "reading the settings directory timed out after {timeout} s (slow or hung file system)"
        ),
        "failed": "reading launcher.log failed unexpectedly",
    }
)
"""The fixed wording of ``LoginState.detail``; ``{t}`` is a time as :func:`describe` writes it."""

# --- parsing ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Event:
    """One recognized line: a :data:`PATTERNS` key (or ``anchor``) at a local time."""

    local: datetime
    kind: str
    groups: tuple[str | None, ...] = ()
    offset: timedelta | None = None  # anchors only: local minus UTC


@dataclass(frozen=True, slots=True)
class _Parsed:
    """The events of one file (or of the lines given), with its first and last line time."""

    events: tuple[_Event, ...]
    first_line: datetime | None
    last_line: datetime | None


def _round_offset(delta: timedelta) -> timedelta | None:
    """Round a local-minus-UTC difference to 15 minutes; None beyond +/-14 h."""
    quarters = round(delta.total_seconds() / 900)
    if abs(quarters) > 14 * 4:
        return None
    return timedelta(minutes=15 * quarters)


def _anchor(message: str, stamp: str) -> _Event | None:
    """The UTC anchor a message carries (see :data:`_ANCHORS`), as an event with the offset."""
    for kind, (search, to_utc) in _ANCHORS.items():
        pattern = PATTERNS[kind]
        hit = pattern.search(message) if search else pattern.match(message)
        if hit and (utc := to_utc(hit.group(1))) is not None:
            local = datetime.fromisoformat(stamp)
            offset = _round_offset(local - utc)
            return None if offset is None else _Event(local, _ANCHOR, offset=offset)
    return None


def _parse(lines: Iterable[str]) -> _Parsed:
    events: list[_Event] = []
    first: str | None = None
    last: str | None = None
    for line in lines:
        match = _LINE.match(line)
        if match is None:
            continue
        stamp, message = match.groups()
        if first is None:
            first = stamp
        last = stamp
        message = message.strip()
        for kind, pattern in _EVENT_PATTERNS:
            if hit := pattern.match(message):
                events.append(_Event(datetime.fromisoformat(stamp), kind, hit.groups()))
                break
        if (anchor := _anchor(message, stamp)) is not None:
            events.append(anchor)
    return _Parsed(
        tuple(events),
        datetime.fromisoformat(first) if first else None,
        datetime.fromisoformat(last) if last else None,
    )


def _resolve_offsets(events: Sequence[_Event], fallback: timedelta) -> list[timedelta]:
    """Each event's offset: the latest anchor before it in its segment (see the module doc).

    Lines before a segment's first anchor use that anchor; a segment without anchors uses
    the previous segment's last offset, else the next segment's first, else ``fallback``.
    Linear in the number of events, however many segments lack anchors.
    """
    segments: list[list[int]] = []
    for index, event in enumerate(events):
        if not segments or event.kind == "restart":
            segments.append([])
        segments[-1].append(index)
    offsets: list[timedelta | None] = [None] * len(events)
    firsts: list[timedelta | None] = []
    lasts: list[timedelta | None] = []
    for segment in segments:
        first, last = _fill_segment(events, segment, offsets)
        firsts.append(first)
        lasts.append(last)
    # For each segment, the first offset of the next segment with an anchor (one pass back).
    following: list[timedelta] = [fallback] * len(segments)
    carry = fallback
    for number in range(len(segments) - 1, -1, -1):
        following[number] = carry
        if (first := firsts[number]) is not None:
            carry = first
    previous: timedelta | None = None
    for number, segment in enumerate(segments):
        if firsts[number] is not None:
            previous = lasts[number]
            continue
        borrowed = previous if previous is not None else following[number]
        for index in segment:
            offsets[index] = borrowed
    return [fallback if offset is None else offset for offset in offsets]


def _fill_segment(
    events: Sequence[_Event], segment: Sequence[int], offsets: list[timedelta | None]
) -> tuple[timedelta | None, timedelta | None]:
    """Fill a segment's offsets from its own anchors; return its first and last anchor's."""
    first: timedelta | None = None
    current: timedelta | None = None
    for position, index in enumerate(segment):
        anchor = events[index].offset
        if anchor is not None:
            if first is None:  # the segment's first anchor also covers what precedes it
                first = anchor
                for earlier in segment[:position]:
                    offsets[earlier] = anchor
            current = anchor
        offsets[index] = current
    return first, current


# --- folding ---------------------------------------------------------------------------

_TWOFA_ENDED: Final = "twofa_ended"  # internal phase: public logging_in, then login_idle

type _Groups = tuple[str | None, ...]


@dataclass(slots=True)
class _Fold:
    """The login state machine (see "Fold" in the module doc), fed events in UTC."""

    counted_since: datetime | None
    phase: str | None = None  # a LoginPhase value, or _TWOFA_ENDED
    since: datetime | None = None
    detail: str | None = None  # logging_in only: why the gateway retries
    warm: bool = False
    token_refused: bool = False
    ib_key: bool = False
    pw_asked: bool | None = None  # pwd=1 in the latest NS_AUTH_START
    challenge_pending: bool = False
    had_challenge: bool = False
    token_protocol: int | None = None
    completed_at: datetime | None = None
    retry_at: datetime | None = None
    reject_reason: str | None = None
    attempts: int = 0
    challenges: int = 0
    counts_complete: bool = False
    after_success: bool = False
    last_success_at: datetime | None = None
    last_lifecycle_at: datetime | None = None
    idle_noise_at: datetime | None = None

    def feed(self, kind: str, at: datetime, groups: _Groups) -> None:
        if kind == _IDLE_NOISE:
            self.idle_noise_at = at
            return
        self.last_lifecycle_at = at
        _HANDLERS[kind](self, at, groups)

    def go(self, phase: str, at: datetime, detail: str | None = None) -> None:
        """Enter ``phase``; ``since`` moves only when the phase changes."""
        if phase != self.phase:
            self.since = at
            self.detail = detail
        elif detail is not None:
            self.detail = detail
        self.phase = phase

    def new_sequence(self) -> None:
        """Count again from zero at the first sign of a new login after a success."""
        if self.after_success:
            self.after_success = False
            self.attempts = 0
            self.challenges = 0
            self.counted_since = self.last_success_at
            self.counts_complete = True


def _on_restart(fold: _Fold, at: datetime, _groups: _Groups) -> None:
    fold.new_sequence()
    fold.go(LoginPhase.RESTARTING, at)
    fold.since = at
    fold.warm = fold.token_refused = fold.ib_key = fold.challenge_pending = False
    fold.pw_asked = None
    fold.token_protocol = None
    fold.retry_at = None
    fold.reject_reason = None


def _on_warm(fold: _Fold, _at: datetime, _groups: _Groups) -> None:
    fold.warm = True


def _on_login_start(fold: _Fold, at: datetime, _groups: _Groups) -> None:
    fold.new_sequence()
    if fold.phase != LoginPhase.AWAITING_2FA:
        fold.go(LoginPhase.LOGGING_IN, at)


def _on_auth_start(fold: _Fold, at: datetime, groups: _Groups) -> None:
    fold.new_sequence()
    pw_flag, _soft, tokens, initial = groups
    fold.attempts += 1
    fold.pw_asked = pw_flag == "1"
    fold.token_refused = fold.pw_asked and initial in {"SOFT", "TST"}
    fold.ib_key = "[IB Key]" in (tokens or "")
    fold.challenge_pending = False
    fold.token_protocol = None
    fold.go(LoginPhase.LOGGING_IN, at)


def _on_challenge(fold: _Fold, at: datetime, _groups: _Groups) -> None:
    fold.challenges += 1
    fold.challenge_pending = True
    fold.go(LoginPhase.AWAITING_2FA, at)
    fold.since = at


def _on_auth_timeout(fold: _Fold, at: datetime, _groups: _Groups) -> None:
    fold.go(LoginPhase.LOGGING_IN, at, DETAILS["no_answer"])


def _on_auto_retry(fold: _Fold, at: datetime, groups: _Groups) -> None:
    why = "no_answer" if groups[1] == "network" else "not_completed"
    fold.go(LoginPhase.LOGGING_IN, at, DETAILS[why])


def _on_completed(fold: _Fold, at: datetime, groups: _Groups) -> None:
    if groups[1] == "true":
        fold.token_protocol = int(groups[0] or 0)
        fold.completed_at = at
    elif fold.phase not in {LoginPhase.LOGIN_REJECTED, _TWOFA_ENDED}:
        fold.go(LoginPhase.LOGGING_IN, at, DETAILS["not_completed"])


def _on_rejected_credentials(fold: _Fold, at: datetime, _groups: _Groups) -> None:
    if fold.phase != LoginPhase.LOGIN_REJECTED:
        fold.reject_reason = "saved_session_refused" if fold.token_refused else "credentials"
    fold.go(LoginPhase.LOGIN_REJECTED, at)


def _on_rejected_other(fold: _Fold, at: datetime, _groups: _Groups) -> None:
    if fold.phase != LoginPhase.LOGIN_REJECTED:
        fold.reject_reason = "unrecognized"
    fold.go(LoginPhase.LOGIN_REJECTED, at)


def _on_twofa_ended(fold: _Fold, at: datetime, _groups: _Groups) -> None:
    fold.had_challenge = fold.challenge_pending
    fold.challenge_pending = False
    fold.go(_TWOFA_ENDED, at)


def _on_throttle(fold: _Fold, at: datetime, groups: _Groups) -> None:
    hours, minutes, seconds = (int(group or 0) for group in groups)
    fold.retry_at = at + timedelta(hours=hours, minutes=minutes, seconds=seconds)
    fold.go(LoginPhase.THROTTLED, at)


def _on_logged_in(fold: _Fold, at: datetime, _groups: _Groups) -> None:
    fold.go(LoginPhase.LOGGED_IN, at)
    fold.since = at
    fold.after_success = True
    fold.last_success_at = at
    fold.challenge_pending = False
    fold.retry_at = None


_HANDLERS: Final[Mapping[str, Callable[[_Fold, datetime, _Groups], None]]] = MappingProxyType(
    {
        "restart": _on_restart,
        "warm": _on_warm,
        "login_start": _on_login_start,
        "auth_start": _on_auth_start,
        "challenge": _on_challenge,
        "auth_timeout": _on_auth_timeout,
        "auto_retry": _on_auto_retry,
        "completed": _on_completed,
        "rejected_credentials": _on_rejected_credentials,
        "twofa_ended": _on_twofa_ended,
        "rejected_other": _on_rejected_other,
        "throttle": _on_throttle,
        "logged_in": _on_logged_in,
    }
)

# --- evaluation at `now` ---------------------------------------------------------------

type _Public = tuple[LoginPhase, datetime | None, str | None]


def _eval_twofa_ended(fold: _Fold, now: datetime, since: datetime) -> _Public:
    had = fold.had_challenge
    if now - since <= IDLE_AFTER:
        retry = "twofa_ended_retry" if had else "ended_before_challenge_retry"
        return LoginPhase.LOGGING_IN, since, DETAILS[retry]
    idle = "twofa_ended" if had else "ended_before_challenge"
    return LoginPhase.LOGIN_IDLE, since, DETAILS[idle]


def _eval_throttled(fold: _Fold, now: datetime, since: datetime) -> _Public:
    if fold.retry_at is not None and now > fold.retry_at + IDLE_AFTER:
        return LoginPhase.LOGIN_IDLE, fold.retry_at, DETAILS["throttle_over"]
    wording = "throttled_2fa" if fold.ib_key or fold.challenges else "throttled"
    return LoginPhase.THROTTLED, since, DETAILS[wording]


def _eval_restarting(fold: _Fold, now: datetime, since: datetime) -> _Public:
    if now - since > IDLE_AFTER:
        return LoginPhase.LOGIN_IDLE, since, DETAILS["never_attempted"]
    return LoginPhase.RESTARTING, since, DETAILS["warm"] if fold.warm else None


def _eval_logging_in(fold: _Fold, now: datetime, since: datetime) -> _Public:
    last = fold.last_lifecycle_at or since
    if now - last > STALL_AFTER:
        return LoginPhase.LOGIN_IDLE, last, DETAILS["stalled"]
    return LoginPhase.LOGGING_IN, since, fold.detail


def _eval_awaiting_2fa(fold: _Fold, _now: datetime, since: datetime) -> _Public:
    return LoginPhase.AWAITING_2FA, since, DETAILS["ib_key_push" if fold.ib_key else "challenge"]


def _eval_login_rejected(fold: _Fold, _now: datetime, since: datetime) -> _Public:
    return LoginPhase.LOGIN_REJECTED, since, DETAILS[fold.reject_reason or "unrecognized"]


def _eval_logged_in(fold: _Fold, now: datetime, since: datetime) -> _Public:
    detail: str | None = None
    if fold.token_protocol == 5 and fold.completed_at is not None:
        detail = DETAILS["second_factor"].format(t=_when(fold.completed_at, now))
    elif fold.warm and fold.pw_asked is False:
        detail = DETAILS["saved_session"]
    elif fold.pw_asked is not None:
        detail = DETAILS["password"]
    return LoginPhase.LOGGED_IN, since, detail


_EVALUATE: Final[Mapping[str, Callable[[_Fold, datetime, datetime], _Public]]] = MappingProxyType(
    {
        _TWOFA_ENDED: _eval_twofa_ended,
        LoginPhase.THROTTLED: _eval_throttled,
        LoginPhase.RESTARTING: _eval_restarting,
        LoginPhase.LOGGING_IN: _eval_logging_in,
        LoginPhase.AWAITING_2FA: _eval_awaiting_2fa,
        LoginPhase.LOGIN_REJECTED: _eval_login_rejected,
        LoginPhase.LOGGED_IN: _eval_logged_in,
    }
)


type _File = tuple[_Parsed, datetime | None]  # a parsed file and its mtime (UTC)


def _fold(files: Sequence[_File]) -> _Fold:
    """Resolve the offsets and fold the events of the files (oldest first)."""
    events = [event for parsed, _mtime in files for event in parsed.events]
    fallback = timedelta(0)  # without any anchor: the newest file's last line vs its mtime
    for parsed, mtime in reversed(files):
        if parsed.last_line is not None:
            if mtime is not None:
                estimate = _round_offset(parsed.last_line - mtime.replace(tzinfo=None))
                fallback = timedelta(0) if estimate is None else estimate
            break
    offsets = _resolve_offsets(events, fallback)
    first_line = next((p.first_line for p, _mtime in files if p.first_line is not None), None)
    oldest = None
    if first_line is not None:
        oldest = (first_line - (offsets[0] if offsets else fallback)).replace(tzinfo=UTC)
    fold = _Fold(counted_since=oldest)
    for event, offset in zip(events, offsets, strict=True):
        if event.kind != _ANCHOR:
            fold.feed(event.kind, (event.local - offset).replace(tzinfo=UTC), event.groups)
    return fold


def _evaluate(files: Sequence[_File], *, now: datetime) -> LoginState:
    """The login state at ``now``: the fold, then the time rules (see the module doc)."""
    now = _aware(now)
    files = [(parsed, None if mtime is None else _aware(mtime)) for parsed, mtime in files]
    log_mtime = files[-1][1] if files else None
    fold = _fold(files)
    if fold.phase is None or fold.since is None:
        noise = fold.idle_noise_at
        if noise is not None and now - noise <= PRELOGIN_SILENCE:
            oldest = fold.counted_since or noise
            return LoginState(
                phase=LoginPhase.LOGIN_IDLE,
                since=oldest,
                detail=DETAILS["idle_since"].format(t=_when(oldest, now)),
                log_updated_at=log_mtime,
            )
        return _unknown(DETAILS["no_start"], log_updated_at=log_mtime)

    phase, since, detail = _EVALUATE[fold.phase](fold, now, fold.since)
    if phase != LoginPhase.LOGGED_IN and log_mtime is not None:
        limit = TWOFA_SILENCE if phase == LoginPhase.AWAITING_2FA else PRELOGIN_SILENCE
        if now - log_mtime > limit:
            phase, since = LoginPhase.UNKNOWN, log_mtime
            detail = DETAILS["silent"].format(t=_when(log_mtime, now))
    return LoginState(
        phase=phase,
        since=since,
        detail=detail,
        retry_at=fold.retry_at if phase == LoginPhase.THROTTLED else None,
        login_attempts=fold.attempts,
        twofa_challenges=fold.challenges,
        counted_since=fold.counted_since,
        counts_complete=fold.counts_complete,
        log_updated_at=log_mtime,
    )


def _aware(value: datetime) -> datetime:
    """UTC; a naive value is taken as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _unknown(detail: str, *, log_updated_at: datetime | None = None) -> LoginState:
    return LoginState(phase=LoginPhase.UNKNOWN, detail=detail, log_updated_at=log_updated_at)


def _reason(exc: OSError) -> str:
    """An OS error without its path: the strerror, else the class name."""
    return exc.strerror or type(exc).__name__


def login_state_from_lines(
    lines: Iterable[str], *, now: datetime, log_mtime: datetime | None
) -> LoginState:
    """The login state that launcher.log lines (oldest first) show at ``now``.

    ``log_mtime`` is the file's modification time: it drives the staleness rule and, when
    no line carries UTC, the offset. Naive datetimes are taken as UTC.
    """
    return _evaluate([(_parse(lines), log_mtime)], now=now)


def is_silent(state: LoginState) -> bool:
    """Whether ``state`` is the staleness rule's ``unknown``: a pre-login log gone silent.

    Only that rule gives an ``unknown`` a ``since`` (the log's last write); an unreadable
    directory or file, or a log without a gateway start, has none.
    """
    return state.phase == LoginPhase.UNKNOWN and state.since is not None


# --- hints -----------------------------------------------------------------------------


def _when(moment: datetime, now: datetime) -> str:
    """``HH:MM:SS UTC``, with the date when it isn't today (UTC)."""
    moment = _aware(moment)
    if moment.date() == _aware(now).date():
        return f"{moment:%H:%M:%S} UTC"
    return f"{moment:%Y-%m-%d %H:%M:%S} UTC"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _since(state: LoginState, now: datetime) -> str:
    return _when(state.since, now) if state.since is not None else "an unknown time"


def _describe_awaiting_2fa(state: LoginState, now: datetime) -> str:
    sent = (
        "an IB Key push to IBKR Mobile"
        if state.detail == DETAILS["ib_key_push"]
        else "a second-factor challenge"
    )
    count = ""
    if state.twofa_challenges:
        challenges = _plural(state.twofa_challenges, "challenge")
        count = (
            f" ({challenges} since the last successful login)"
            if state.counts_complete
            else f" (at least {challenges} in this login sequence)"
        )
    return (
        f"The gateway is waiting for two-factor approval: IBKR sent {sent} at "
        f"{_since(state, now)}{count}. The account holder must approve it; an unanswered "
        "challenge ends after about 4 to 15 minutes, and the gateway may then send a new one."
    )


# The gateway's login automation (IBC) logs in again after a pause or an unanswered
# challenge only when its relogin is on; this server can't see that setting.
_RELOGIN: Final = (
    "only if its login automation is set to retry (ib-gateway-docker: "
    "RELOGIN_AFTER_TWOFA_TIMEOUT=yes)"
)
_THEN_IDLE: Final = (
    "the phase turns login_idle and the operator needs to restart the gateway container"
)


def _describe_throttled(state: LoginState, now: datetime) -> str:
    retry = _when(state.retry_at, now) if state.retry_at is not None else "a set time"
    challenge = (
        "; that login will send a new second-factor challenge, so the account holder should "
        "be ready to approve it"
        if state.detail == DETAILS["throttled_2fa"]
        else ""
    )
    return (
        f"The gateway is pausing logins after repeated failures until {retry}; nothing needs "
        f"to be done before then. After the pause it logs in again {_RELOGIN}{challenge}. If "
        f"nothing retries, {_THEN_IDLE}."
    )


def _describe_login_rejected(state: LoginState, now: datetime) -> str:
    since = _since(state, now)
    if state.detail == DETAILS["saved_session_refused"]:
        return (
            f"IBKR rejected the gateway's login at {since}: the daily auto-restart reused the "
            "saved session, IBKR refused it and asked for the password, which an auto-restart "
            "cannot enter. The gateway will not retry by itself; the operator needs to restart "
            "the gateway container (a cold restart). Approving a 2FA prompt won't help."
        )
    if state.detail == DETAILS["credentials"]:
        return (
            f"IBKR rejected the gateway's username or password at {since}. The gateway will "
            "not retry by itself; the operator needs to check the credentials and restart the "
            "gateway container."
        )
    return (
        f"IBKR rejected the gateway's login at {since} for a reason this server doesn't "
        "recognize. The gateway will not retry by itself; the operator should look at the "
        "gateway (its screen over VNC or its container output)."
    )


def _describe_login_idle(state: LoginState, now: datetime) -> str:
    if state.detail == DETAILS["never_attempted"]:
        return (
            f"The gateway started at {_since(state, now)} but its login automation never "
            "began a login. The operator should check the gateway container's output and "
            "configuration (credentials, TRADING_MODE, TWS_SETTINGS_PATH); restarting alone "
            "may not fix it."
        )
    return (
        "The gateway is running but not logged in, and nothing is retrying: "
        f"{state.detail or 'no login is in progress'}. The operator needs to restart the "
        "gateway container."
    )


def _describe_restarting(state: LoginState, now: datetime) -> str:
    kind = " (daily auto-restart)" if state.detail == DETAILS["warm"] else ""
    return (
        f"The gateway started at {_since(state, now)}{kind} and is about to log in; this "
        "normally takes under a minute."
    )


def _describe_login_ended(state: LoginState, now: datetime) -> str:
    what = (
        "The second-factor challenge ended unanswered"
        if state.detail == DETAILS["twofa_ended_retry"]
        else "IBKR ended the gateway's login before any second-factor challenge"
    )
    return (
        f"{what} at {_since(state, now)}. The gateway logs in again within seconds "
        f"{_RELOGIN}; check again in a minute. If nothing retries, {_THEN_IDLE}."
    )


def _describe_logging_in(state: LoginState, now: datetime) -> str:
    if state.detail in (DETAILS["twofa_ended_retry"], DETAILS["ended_before_challenge_retry"]):
        return _describe_login_ended(state, now)
    attempts = state.login_attempts or 0
    if attempts < 3:
        why = f" ({state.detail})" if state.detail else ""
        return f"The gateway is logging in to IBKR{why}; check again in a minute."
    start = _when(state.counted_since, now) if state.counted_since else "the oldest line read"
    counted = (
        f"{_plural(attempts, 'attempt')} since the last successful login at {start}"
        if state.counts_complete
        else f"at least {_plural(attempts, 'attempt')} since {start}"
    )
    return f"IBKR has not completed the gateway's login: {counted}; the gateway keeps retrying."


def _describe_logged_in(state: LoginState, now: datetime) -> str:
    since = _since(state, now)
    if state.since is not None and _aware(now) - state.since < _JUST_LOGGED_IN:
        return f"The gateway logged in at {since} and is starting its API; check again in a minute."
    return (
        f"The gateway's launcher.log shows a successful login at {since}, but its API refuses "
        "or doesn't answer connections. That log records only logins, so what happened since "
        "isn't visible there: the gateway may have stopped (a stopped gateway leaves this log "
        "unchanged) or hung, its session may have ended, its API may be disabled, IB_PORT may "
        "be the wrong port, or IB_GATEWAY_SETTINGS_DIR may belong to another gateway."
    )


def _describe_unknown(state: LoginState, _now: datetime) -> str:
    if not state.detail:
        return "The gateway's login phase is unknown."
    return f"{state.detail[:1].upper()}{state.detail[1:]}."


_DESCRIBE: Final[Mapping[LoginPhase, Callable[[LoginState, datetime], str]]] = MappingProxyType(
    {
        LoginPhase.AWAITING_2FA: _describe_awaiting_2fa,
        LoginPhase.THROTTLED: _describe_throttled,
        LoginPhase.LOGIN_REJECTED: _describe_login_rejected,
        LoginPhase.LOGIN_IDLE: _describe_login_idle,
        LoginPhase.RESTARTING: _describe_restarting,
        LoginPhase.LOGGING_IN: _describe_logging_in,
        LoginPhase.LOGGED_IN: _describe_logged_in,
        LoginPhase.UNKNOWN: _describe_unknown,
    }
)


def describe(state: LoginState, now: datetime) -> str:
    """The ``get_health`` hint for a login state: what it means and who can act.

    Addressed to whoever relays it, with remedies for "the operator". It says only what the
    state holds: the phase, its times and its counts. Times read ``HH:MM:SS UTC``, with the
    date when it isn't today.
    """
    return _DESCRIBE[state.phase](state, now)


# --- reading the directory -------------------------------------------------------------

type _CacheKey = tuple[int, int, int, int, int]


class _BusyError(Exception):
    """An earlier call on a worker is still running after its caller gave up."""


class _Worker[T]:
    """Runs one blocking call at a time, each on a fresh daemon thread.

    A caller that arrives while a call runs shares its result, unless an earlier caller
    already gave up on it (a hung file system): then it is refused at once, so waits don't
    stack up behind a thread that may never return.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._lock = threading.Lock()
        self._future: concurrent.futures.Future[T] | None = None
        self._overdue: concurrent.futures.Future[T] | None = None  # a caller gave up on it

    def _submit(self, call: Callable[[], T]) -> concurrent.futures.Future[T]:
        with self._lock:
            if self._future is not None and not self._future.done():
                if self._future is self._overdue:
                    raise _BusyError
                return self._future
            future: concurrent.futures.Future[T] = concurrent.futures.Future()
            future.set_running_or_notify_cancel()  # a waiter's timeout can't cancel it
            self._future = future
        thread = threading.Thread(
            target=_complete, args=(future, call), name=self._name, daemon=True
        )
        try:
            thread.start()
        except Exception as exc:
            future.set_exception(exc)
            raise
        return future

    async def run(self, call: Callable[[], T], timeout: float) -> T:
        """``call()`` on a daemon thread; :class:`_BusyError` or :class:`TimeoutError`."""
        future = self._submit(call)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except TimeoutError:
            with self._lock:
                self._overdue = future
            raise


def _complete[T](future: concurrent.futures.Future[T], call: Callable[[], T]) -> None:
    try:
        result = call()
    except Exception as exc:  # handed to the waiting coroutine
        future.set_exception(exc)
    else:
        future.set_result(result)


class GatewayLog:
    """The gateway's login phase, read from launcher.log in its settings directory.

    Only files named ``launcher.log`` and ``launcher.YYYYMMDD.log`` are opened: never
    through a symlink, never unless they are regular files, never more than
    :data:`MAX_FILES` of them or :data:`MAX_TOTAL_BYTES` in all. Nothing else in the
    directory is opened. Parsed files are cached by name, size and mtime, so a read after
    the first only parses what changed.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[_CacheKey, _Parsed]] = {}
        self._reads: _Worker[LoginState] = _Worker("ib-gateway-mcp-gateway-log")
        self._checks: _Worker[list[str]] = _Worker("ib-gateway-mcp-gateway-log-check")

    def read(self, now: datetime | None = None) -> LoginState:
        """The login state now (or at ``now``). Blocking; never raises; one read at a time."""
        with self._lock:
            try:
                return self._read(now)
            except Exception as exc:
                logger.warning(
                    "Reading the gateway's launcher.log failed (%s).", type(exc).__name__
                )
                return _unknown(DETAILS["failed"])

    async def read_async(self, timeout: float) -> LoginState:
        """:meth:`read` on a daemon thread, waiting at most ``timeout`` seconds.

        A call made while a read runs shares that read. While a read is stuck after its
        caller gave up (a hung file system), returns ``unknown`` at once instead.
        """
        try:
            return await self._reads.run(self.read, timeout)
        except _BusyError:
            return _unknown(DETAILS["busy"])
        except TimeoutError:
            logger.warning("Reading the gateway's launcher.log timed out after %g s.", timeout)
            return _unknown(DETAILS["timeout"].format(timeout=f"{timeout:g}"))
        except Exception as exc:
            logger.warning("Reading the gateway's launcher.log failed (%s).", type(exc).__name__)
            return _unknown(DETAILS["failed"])

    async def check_async(self, timeout: float) -> list[str]:
        """Start-up problems with the directory, one sentence each; empty when it looks right.

        Reports an unreadable directory, a directory without launcher.log, and a directory
        this process could write to (it should be mounted read-only). Runs on a daemon
        thread like :meth:`read_async`. Never raises: a failure, a timeout or a stuck earlier
        check becomes a problem sentence.
        """
        where = f"the gateway settings directory {self.directory}"
        try:
            return await self._checks.run(self._check, timeout)
        except _BusyError:
            return [f"an earlier check of {where} has not finished (slow or hung file system)"]
        except TimeoutError:
            return [f"checking {where} timed out after {timeout:g} s (slow or hung file system)"]
        except Exception as exc:
            return [f"checking {where} failed ({type(exc).__name__})"]

    def _check(self) -> list[str]:
        where = f"the gateway settings directory {self.directory}"
        try:
            names = os.listdir(self.directory)
        except OSError as exc:
            return [f"cannot read {where}: {_reason(exc)}"]
        problems: list[str] = []
        if not any(_FILE_NAME.match(name) for name in names):
            problems.append(
                f"{where} has no launcher.log: check that it is the gateway's "
                "TWS_SETTINGS_PATH (with TRADING_MODE=both, the _live or _paper one for "
                "IB_PORT); the file appears when the gateway starts"
            )
        if os.access(self.directory, os.W_OK):
            problems.append(f"{where} is writable by this server; mount it read-only")
        return problems

    def _read(self, now: datetime | None) -> LoginState:
        try:
            names = os.listdir(self.directory)
        except OSError as exc:
            self._cache = {}
            return _unknown(DETAILS["dir_unreadable"].format(reason=_reason(exc)))
        # Newest first: launcher.log, then launcher.YYYYMMDD.log by the date in the name.
        matches = sorted(
            filter(None, map(_FILE_NAME.match, names)),
            key=lambda match: match.group(1) or "99999999",
            reverse=True,
        )
        candidates = [match.string for match in matches[:MAX_FILES]]
        cache: dict[str, tuple[_CacheKey, _Parsed]] = {}
        newest_first: list[tuple[_Parsed, datetime]] = []
        budget = MAX_TOTAL_BYTES
        try:
            for name in candidates:
                if budget <= 0:
                    break
                try:
                    got = self._read_file(name, budget)
                except OSError as exc:
                    if newest_first:
                        break  # keep the newer files; the counts become a lower bound
                    return _unknown(
                        DETAILS["file_unreadable"].format(name=name, reason=_reason(exc))
                    )
                if got is None:  # not a regular file, or gone since the listing
                    if newest_first:
                        break  # older files would be folded across the gap
                    continue
                key, parsed, mtime, taken, cut = got
                cache[name] = (key, parsed)
                newest_first.append((parsed, mtime))
                budget -= taken
                if cut:
                    break  # an older file would not continue where this one's tail begins
        finally:
            self._cache = cache
        if not newest_first:
            return _unknown(DETAILS["no_regular_file" if candidates else "no_file"])
        return _evaluate(newest_first[::-1], now=now or datetime.now(UTC))

    def _read_file(
        self, name: str, budget: int
    ) -> tuple[_CacheKey, _Parsed, datetime, int, bool] | None:
        """One file's events (cached), its mtime, the bytes taken and whether its head was cut.

        None when the file is skipped. Only newline-terminated lines are parsed.
        """
        try:
            fd = os.open(self.directory / name, _OPEN_FLAGS)
        except OSError as exc:
            if exc.errno in _SKIPPED_ERRNOS:
                return None
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None
            take = min(info.st_size, MAX_FILE_BYTES, budget)
            start = info.st_size - take
            key: _CacheKey = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, start)
            mtime = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
                microseconds=info.st_mtime_ns // 1000
            )
            cached = self._cache.get(name)
            if cached is not None and cached[0] == key:
                return key, cached[1], mtime, take, start > 0
            # From a cut, also read the byte before it: the first line is whole only when
            # that byte ends the line before.
            data = _read_range(fd, start - 1, take + 1) if start else _read_range(fd, 0, take)
        finally:
            os.close(fd)
        # A last line without its newline is still being written: it is parsed on a later
        # read, once the file (and so the cache key) has grown. Cutting the bytes, not the
        # text, also keeps a multi-byte character cut at the end from decoding as U+FFFD.
        data = data[: data.rfind(b"\n") + 1]
        text = data.decode("utf-8", errors="replace")
        if start:
            _partial, _newline, text = text.partition("\n")
        return key, _parse(text.split("\n")[:-1]), mtime, take, start > 0


def _read_range(fd: int, start: int, size: int) -> bytes:
    os.lseek(fd, start, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, min(remaining, _READ_CHUNK))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
