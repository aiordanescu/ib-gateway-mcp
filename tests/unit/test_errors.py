"""IbApiError.with_hint: the raw gateway text stays, only the message gains advice."""

from __future__ import annotations

import pytest

from ib_gateway_mcp.errors import IbApiError


@pytest.mark.parametrize(
    ("message", "hint", "expected"),
    [
        ("Scanner subscription cancelled.", "Check scan_code", "cancelled. Check scan_code."),
        ("Scanner subscription cancelled. ", "Check scan_code.", "cancelled. Check scan_code."),
        ("No data", "Try use_rth=false!", "No data. Try use_rth=false!"),
        ("No data.", None, "No data."),
    ],
)
def test_with_hint_joins_the_sentences_once(message: str, hint: str | None, expected: str) -> None:
    error = IbApiError(162, message, 7)
    hinted = error.with_hint(hint)
    assert str(hinted).endswith(expected)
    assert str(hinted).startswith("IB error 162: ")
    assert ".." not in str(hinted)


def test_with_hint_keeps_code_request_id_and_raw_message() -> None:
    error = IbApiError(354, "Requested market data is not subscribed.", 12)
    hinted = error.with_hint("Use delayed data.", context="a quote for AAPL")
    assert (hinted.error_code, hinted.req_id) == (354, 12)
    assert hinted.error_message == "Requested market data is not subscribed."
    assert hinted.hint == "Use delayed data."
    assert str(hinted) == (
        "IB error 354: Requested market data is not subscribed (a quote for AAPL). "
        "Use delayed data."
    )
    assert error.hint is None  # the original is untouched
