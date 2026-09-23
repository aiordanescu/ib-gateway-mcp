"""AuditLog: JSONL output, redaction and tolerance of I/O failures."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from ib_gateway_mcp.safety import AUDIT_LOGGER_NAME, AuditEvent, AuditLog, PreviewRecord

NOW = datetime(2026, 9, 22, 18, 30, 5, 123456, tzinfo=UTC)
TOKEN = "AbCdEf0123456789abcdefGHIJKLmnop"


def fixed_clock() -> datetime:
    return NOW


def read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def audit(path: Path) -> AuditLog:
    return AuditLog(path, clock=fixed_clock)


# -- output -----------------------------------------------------------------


def test_writes_one_json_object_per_line(audit: AuditLog, path: Path) -> None:
    audit.record(AuditEvent.PREVIEW, account="DU1234567", kind="order", quantity=10)
    audit.record("submit_result", account="DU1234567", order_id=42, status="Submitted")

    lines = read_lines(path)
    assert lines == [
        {
            "ts": "2026-09-22T18:30:05.123+00:00",
            "event": "preview",
            "account": "DU1234567",
            "kind": "order",
            "quantity": 10,
        },
        {
            "ts": "2026-09-22T18:30:05.123+00:00",
            "event": "submit_result",
            "account": "DU1234567",
            "order_id": 42,
            "status": "Submitted",
        },
    ]
    assert list(lines[0])[:2] == ["ts", "event"]
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_appends_to_an_existing_file(audit: AuditLog, path: Path) -> None:
    path.write_text('{"event": "earlier"}\n', encoding="utf-8")
    audit.record("cancel", order_id=7)
    assert [line["event"] for line in read_lines(path)] == ["earlier", "cancel"]


def test_creates_parent_directories(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "audit.jsonl"
    AuditLog(nested, clock=fixed_clock).record("cancel_all")
    assert read_lines(nested)[0]["event"] == "cancel_all"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_new_file_is_private(audit: AuditLog, path: Path) -> None:
    audit.record("preview")
    assert path.stat().st_mode & 0o777 == 0o600


def test_timestamps_are_utc(path: Path) -> None:
    plus_two = timezone(timedelta(hours=2))
    AuditLog(path, clock=lambda: datetime(2026, 9, 22, 20, 0, tzinfo=plus_two)).record("a")
    AuditLog(path, clock=lambda: datetime(2026, 9, 22, 18, 0)).record("b")
    assert [line["ts"] for line in read_lines(path)] == [
        "2026-09-22T18:00:00.000+00:00",
        "2026-09-22T18:00:00.000+00:00",
    ]


def test_default_clock_is_now_utc(path: Path) -> None:
    before = datetime.now(UTC)
    AuditLog(path).record("preview")
    ts = datetime.fromisoformat(read_lines(path)[0]["ts"])
    assert ts.tzinfo == UTC
    assert before - timedelta(seconds=1) <= ts <= datetime.now(UTC)


def test_reserved_keys_do_not_clobber_ts_or_event(audit: AuditLog, path: Path) -> None:
    audit.record("preview", ts="spoofed", event="spoofed")
    line = read_lines(path)[0]
    assert line["event"] == "preview"
    assert line["ts"] == "2026-09-22T18:30:05.123+00:00"
    assert line["field_ts"] == "spoofed"
    assert line["field_event"] == "spoofed"


class Side(Enum):
    BUY = 1


class Summary(BaseModel):
    symbol: str
    quantity: float
    password: SecretStr


@dataclass
class Leg:
    symbol: str
    api_key: str


def test_serializes_non_json_values(audit: AuditLog, path: Path) -> None:
    audit.record(
        "submit",
        price=Decimal("1.50"),
        at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        file=Path("/tmp/x"),  # noqa: S108 - only stringified
        side=Side.BUY,
        tags={"only"},
        pair=("AAPL", "MSFT"),
        missing=float("nan"),
        infinite=float("inf"),
        model=Summary(symbol="AAPL", quantity=1.5, password=SecretStr("hunter2")),
        leg=Leg(symbol="SPY", api_key="k-123"),
        event_enum=AuditEvent.CANCEL,
    )
    line = read_lines(path)[0]
    assert line["price"] == "1.50"
    assert line["at"] == "2026-01-02T03:04:05+00:00"
    assert line["file"] == "/tmp/x"  # noqa: S108
    assert line["side"] == 1
    assert line["tags"] == ["only"]
    assert line["pair"] == ["AAPL", "MSFT"]
    assert line["missing"] is None
    assert line["infinite"] is None
    assert line["model"] == {"symbol": "AAPL", "quantity": 1.5, "password": "[redacted]"}
    assert line["leg"] == {"symbol": "SPY", "api_key": "[redacted]"}
    assert line["event_enum"] == "cancel"


# -- redaction --------------------------------------------------------------


def test_preview_token_is_kept_as_a_prefix(audit: AuditLog, path: Path) -> None:
    audit.record("preview", token=TOKEN)
    audit.record("submit", preview_token=TOKEN)
    audit.record("submit", token=None)

    lines = read_lines(path)
    assert lines[0]["token"] == "AbCdEf…"
    assert lines[1]["preview_token"] == "AbCdEf…"
    assert lines[2]["token"] is None
    assert TOKEN not in path.read_text(encoding="utf-8")


def test_non_string_token_values_are_redacted(audit: AuditLog, path: Path) -> None:
    audit.record("preview", token={"id": TOKEN}, Token=12345)
    line = read_lines(path)[0]
    assert line["token"] == "[redacted]"
    assert line["Token"] == "[redacted]"


@pytest.mark.parametrize(
    "key",
    [
        "auth_token",
        "token_secret",
        "access_token",
        "TOKENS",
        "secret",
        "client_secret",
        "password",
        "IB_PASSWORD",
        "passwd",
        "Authorization",
        "credentials",
        "api_key",
        "apikey",
    ],
)
def test_sensitive_keys_are_redacted(audit: AuditLog, path: Path, key: str) -> None:
    audit.record("preview", **{key: "hunter2"})
    assert read_lines(path)[0][key] == "[redacted]"
    assert "hunter2" not in path.read_text(encoding="utf-8")


def test_redaction_applies_at_every_depth(audit: AuditLog, path: Path) -> None:
    audit.record(
        "submit",
        request={
            "headers": {"Authorization": "Bearer hunter2", "Accept": "json"},
            "attempts": [{"password": "hunter2", "n": 1}, {"nested": {"auth_token": "hunter2"}}],
            "token": TOKEN,
        },
    )
    line = read_lines(path)[0]
    assert line["request"] == {
        "headers": {"Authorization": "[redacted]", "Accept": "json"},
        "attempts": [{"password": "[redacted]", "n": 1}, {"nested": {"auth_token": "[redacted]"}}],
        "token": "AbCdEf…",
    }
    assert "hunter2" not in path.read_text(encoding="utf-8")


def test_preview_records_are_expanded_with_the_token_shortened(audit: AuditLog, path: Path) -> None:
    record = PreviewRecord(
        token=TOKEN,
        kind="order",
        account="DU1234567",
        payload={"order": {"quantity": 1}},
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
    )
    audit.record("submit", preview=record)
    preview = read_lines(path)[0]["preview"]
    assert preview["token"] == "AbCdEf…"
    assert preview["payload"] == {"order": {"quantity": 1}}
    assert preview["expires_at"] == "2026-09-22T18:32:05.123456+00:00"


def test_non_sensitive_keys_are_kept(audit: AuditLog, path: Path) -> None:
    audit.record("preview", account="DU1234567", symbol="AAPL", reason="margin")
    line = read_lines(path)[0]
    assert (line["account"], line["symbol"], line["reason"]) == ("DU1234567", "AAPL", "margin")


# -- logger -----------------------------------------------------------------


def test_logger_only_mode(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    audit = AuditLog(None, clock=fixed_clock)
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER_NAME):
        audit.record("preview", account="DU1234567", password="hunter2")

    assert audit.path is None
    assert list(tmp_path.iterdir()) == []
    [log] = [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME]
    assert log.levelno == logging.INFO
    entry = json.loads(log.getMessage())
    assert entry["event"] == "preview"
    assert entry["password"] == "[redacted]"


def test_file_mode_also_logs(audit: AuditLog, path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER_NAME):
        audit.record("cancel", order_id=1)
    assert any(json.loads(r.getMessage())["event"] == "cancel" for r in caplog.records)
    assert read_lines(path)[0]["order_id"] == 1


def test_custom_logger(path: Path, caplog: pytest.LogCaptureFixture) -> None:
    custom = logging.getLogger("tests.audit")
    with caplog.at_level(logging.INFO, logger="tests.audit"):
        AuditLog(path, logger=custom, clock=fixed_clock).record("preview")
    assert [r.name for r in caplog.records] == ["tests.audit"]


# -- failure tolerance ------------------------------------------------------


def test_unwritable_path_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    audit = AuditLog(blocker / "audit.jsonl", clock=fixed_clock)

    with caplog.at_level(logging.WARNING, logger=AUDIT_LOGGER_NAME):
        audit.record("submit", order_id=1)

    assert any("Could not write audit log" in r.getMessage() for r in caplog.records)


def test_directory_path_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=AUDIT_LOGGER_NAME):
        AuditLog(tmp_path, clock=fixed_clock).record("submit")
    assert any("Could not write audit log" in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX, non-root")
def test_read_only_file_is_logged_not_raised(
    audit: AuditLog, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path.write_text("", encoding="utf-8")
    path.chmod(0o400)
    with caplog.at_level(logging.WARNING, logger=AUDIT_LOGGER_NAME):
        audit.record("submit")
    assert any("Could not write audit log" in r.getMessage() for r in caplog.records)
    assert path.read_text(encoding="utf-8") == ""


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("boom")


def test_serialization_failure_is_logged_not_raised(
    audit: AuditLog, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=AUDIT_LOGGER_NAME):
        audit.record("submit", thing=Unprintable())
    assert any("Could not serialize audit event" in r.getMessage() for r in caplog.records)
    assert not path.exists()

    audit.record("submit", order_id=2)  # the log keeps working afterwards
    assert read_lines(path)[0]["order_id"] == 2


def test_self_referencing_values_do_not_recurse_forever(audit: AuditLog, path: Path) -> None:
    loop: dict[str, Any] = {"name": "loop"}
    loop["self"] = loop
    audit.record("submit", data=loop)

    depth, node = 0, read_lines(path)[0]["data"]
    while isinstance(node, dict):
        node, depth = node["self"], depth + 1
    assert node == "[redacted]"
    assert depth < 30


# -- construction -----------------------------------------------------------


def test_from_settings(tmp_path: Path) -> None:
    target = tmp_path / "from-settings.jsonl"
    audit = AuditLog.from_settings(SimpleNamespace(audit_log=str(target)), clock=fixed_clock)
    assert audit.path == target
    audit.record("preview")
    assert target.exists()

    assert AuditLog.from_settings(SimpleNamespace(audit_log=None)).path is None
    assert AuditLog.from_settings(SimpleNamespace(audit_log="")).path is None


def test_event_names_are_plain_strings() -> None:
    assert json.dumps(AuditEvent.SUBMIT_RESULT) == '"submit_result"'
    assert {e.value for e in AuditEvent} >= {
        "preview",
        "submit",
        "submit_result",
        "modify",
        "cancel",
        "cancel_all",
        "exercise",
        "replace_fa",
        "rejected",
    }


def test_audit_lines_survive_a_quiet_log_level(caplog: pytest.LogCaptureFixture) -> None:
    """IBKR_MCP_LOG_LEVEL=WARNING must not silence the audit trail."""
    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    previous = audit_logger.level
    audit_logger.setLevel(logging.NOTSET)
    root = logging.getLogger()
    root_level = root.level
    try:
        root.setLevel(logging.WARNING)
        audit = AuditLog(None, clock=fixed_clock)
        assert audit_logger.level == logging.INFO
        with caplog.at_level(logging.WARNING):  # the handler still sees INFO records
            caplog.handler.setLevel(logging.NOTSET)
            audit.record("submit", order_id=3)
        assert any('"order_id": 3' in r.getMessage() for r in caplog.records)
    finally:
        root.setLevel(root_level)
        audit_logger.setLevel(previous)


def test_check_writable(tmp_path: Path) -> None:
    good = AuditLog(tmp_path / "sub" / "audit.jsonl")
    good.check_writable()
    assert (tmp_path / "sub" / "audit.jsonl").exists()
    AuditLog(None).check_writable()  # nothing to check
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    with pytest.raises(OSError, match="Errno"):
        AuditLog(blocker / "audit.jsonl").check_writable()
