"""OrderPolicy and OrderSummary: allowlists, quantity and notional limits."""

from __future__ import annotations

import sys
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from ib_gateway_mcp.errors import OrderLimitError, SafetyError
from ib_gateway_mcp.safety import (
    LegSummary,
    NotionalEstimate,
    OrderPolicy,
    OrderSummary,
    estimate_notional,
)

UNSET_DOUBLE = sys.float_info.max


def order(**overrides: Any) -> OrderSummary:
    fields: dict[str, Any] = {
        "symbol": "AAPL",
        "sec_type": "STK",
        "action": "BUY",
        "quantity": 10,
        "order_type": "LMT",
        "limit_price": 100.0,
    }
    fields.update(overrides)
    return OrderSummary(**fields)


def leg(symbol: str, **overrides: Any) -> LegSummary:
    fields: dict[str, Any] = {"symbol": symbol, "sec_type": "OPT", "action": "BUY"}
    fields.update(overrides)
    return LegSummary(**fields)


def spread(**overrides: Any) -> OrderSummary:
    """A two-leg SPY option vertical, quoted as a net debit."""
    fields: dict[str, Any] = {
        "symbol": "SPY",
        "sec_type": "BAG",
        "action": "BUY",
        "quantity": 2,
        "order_type": "LMT",
        "limit_price": 1.5,
        "legs": (
            leg("SPY", action="BUY", multiplier=100),
            leg("SPY", action="SELL", multiplier=100),
        ),
    }
    fields.update(overrides)
    return OrderSummary(**fields)


def refusal(policy: OrderPolicy, summary: OrderSummary) -> str:
    with pytest.raises(OrderLimitError) as excinfo:
        policy.check(summary)
    return str(excinfo.value)


# -- no limits --------------------------------------------------------------


def test_no_limits_allows_anything() -> None:
    policy = OrderPolicy()
    policy.check(
        order(symbol="ANY", sec_type="FUT", quantity=1e9, order_type="MKT", limit_price=None)
    )
    policy.check(spread())
    assert policy.describe() == "no order limits configured"


# -- symbol allowlist -------------------------------------------------------


def test_symbol_allowlist_is_case_insensitive() -> None:
    policy = OrderPolicy(allowed_symbols=["aapl", " spy "])
    policy.check(order(symbol="AAPL"))
    policy.check(order(symbol="aapl"))
    policy.check(order(symbol="Spy"))


def test_symbol_not_allowed() -> None:
    policy = OrderPolicy(allowed_symbols=["AAPL", "SPY"])
    message = refusal(policy, order(symbol="msft"))
    assert "symbol msft is not allowed (allowed symbols: AAPL, SPY)" in message
    assert message.startswith("Order refused by the server's order limits: ")


def test_symbol_allowlist_accepts_comma_separated_string() -> None:
    policy = OrderPolicy(allowed_symbols="AAPL, spy,,")
    assert policy.allowed_symbols == frozenset({"AAPL", "SPY"})


def test_long_allowlists_are_truncated_in_messages() -> None:
    symbols = [f"S{i:02d}" for i in range(30)]
    message = refusal(OrderPolicy(allowed_symbols=symbols), order(symbol="NOPE"))
    assert "S19, ... (30 total)" in message
    assert "S20" not in message


def test_combo_checks_every_leg_symbol_not_the_combo_symbol() -> None:
    policy = OrderPolicy(allowed_symbols=["AAPL", "MSFT"])
    pair = spread(
        symbol="AAPL,MSFT",
        legs=(leg("AAPL", sec_type="STK"), leg("MSFT", sec_type="STK", action="SELL")),
    )
    policy.check(pair)

    bad = spread(symbol="AAPL", legs=(leg("AAPL"), leg("TSLA"), leg("GME")))
    message = refusal(policy, bad)
    assert "combo leg symbol TSLA is not allowed" in message
    assert "combo leg symbol GME is not allowed" in message
    assert "leg symbol AAPL" not in message


# -- sec type allowlist -----------------------------------------------------


def test_sec_type_allowlist() -> None:
    policy = OrderPolicy(allowed_sec_types=["stk", "OPT"])
    policy.check(order(sec_type="STK"))
    policy.check(order(sec_type="opt", multiplier=100, limit_price=1.0))
    message = refusal(policy, order(sec_type="FUT"))
    assert "security type FUT is not allowed (allowed security types: OPT, STK)" in message


def test_combo_requires_bag_in_the_allowlist() -> None:
    message = refusal(OrderPolicy(allowed_sec_types=["OPT"]), spread())
    assert "combo orders (BAG) are not allowed" in message


def test_combo_checks_every_leg_sec_type() -> None:
    policy = OrderPolicy(allowed_sec_types=["BAG", "OPT"])
    policy.check(spread())

    mixed = spread(legs=(leg("SPY"), leg("ES", sec_type="FUT")))
    message = refusal(policy, mixed)
    assert "combo leg ES has security type FUT, which is not allowed" in message


# -- max quantity -----------------------------------------------------------


def test_max_quantity_boundary() -> None:
    policy = OrderPolicy(max_quantity=100)
    policy.check(order(quantity=100))
    message = refusal(policy, order(quantity=100.5))
    assert "quantity 100.5 exceeds the maximum of 100" in message


def test_max_quantity_uses_absolute_value() -> None:
    message = refusal(OrderPolicy(max_quantity=10), order(quantity=-11))
    assert "quantity 11 exceeds the maximum of 10" in message


def test_max_quantity_applies_to_leg_ratios() -> None:
    policy = OrderPolicy(max_quantity=10)
    ratio_spread = spread(quantity=5, legs=(leg("SPY"), leg("SPY", action="SELL", ratio=3)))
    message = refusal(policy, ratio_spread)
    assert "combo leg SPY quantity 15 (ratio 3 x 5) exceeds the maximum of 10" in message
    assert "quantity 5 exceeds" not in message


def test_ratio_one_legs_do_not_duplicate_the_combo_message() -> None:
    message = refusal(OrderPolicy(max_quantity=10), spread(quantity=11))
    assert message.count("exceeds the maximum") == 1


# -- max notional -----------------------------------------------------------


def test_notional_boundary_uses_limit_price() -> None:
    policy = OrderPolicy(max_notional=1_000)
    policy.check(order(quantity=10, limit_price=100))
    message = refusal(policy, order(quantity=10, limit_price=100.01))
    assert (
        "notional 1,000.10 USD (10 x 100.01 limit price x 1 multiplier) "
        "exceeds the maximum of 1,000 USD"
    ) in message


def test_notional_is_exact_decimal_arithmetic() -> None:
    # 3 * 33.33 is 99.99000000000001 in binary floating point.
    OrderPolicy(max_notional=99.99).check(order(quantity=3, limit_price=33.33))


def test_notional_includes_multiplier() -> None:
    option = order(sec_type="OPT", quantity=10, limit_price=2.5, multiplier="100")
    OrderPolicy(max_notional=2_500).check(option)
    message = refusal(OrderPolicy(max_notional=2_499), option)
    assert "notional 2,500.00 USD (10 x 2.5 limit price x 100 multiplier)" in message


def test_notional_reports_the_order_currency() -> None:
    message = refusal(OrderPolicy(max_notional=100), order(currency="eur"))
    assert "USD" not in message
    assert "exceeds the maximum of 100 EUR" in message


def test_stop_orders_use_the_stop_price() -> None:
    stop = order(order_type="STP", limit_price=None, aux_price=200, reference_price=50)
    message = refusal(OrderPolicy(max_notional=1_000), stop)
    assert "10 x 200 stop/trigger price" in message


def test_stop_limit_prefers_the_limit_price() -> None:
    stop_limit = order(order_type="STP LMT", limit_price=90, aux_price=200)
    estimate = estimate_notional(stop_limit)
    assert estimate == NotionalEstimate(Decimal("900.0"), "10 x 90 limit price x 1 multiplier")


@pytest.mark.parametrize("order_type", ["TRAIL", "REL", "PEG MID", "TRAIL LIMIT"])
def test_offset_aux_prices_are_never_used_as_prices(order_type: str) -> None:
    # A trailing amount of 1.0 must not make a 10-share order look like $10.
    trailing = order(order_type=order_type, limit_price=None, aux_price=1.0)
    message = refusal(OrderPolicy(max_notional=1_000), trailing)
    assert f"nothing bounds the fill price of this BUY {order_type} order" in message

    with_reference = trailing.model_copy(update={"reference_price": 150.0})
    message = refusal(OrderPolicy(max_notional=1_000), with_reference)
    assert "10 x 150 reference price" in message


def test_market_order_uses_reference_price() -> None:
    market = order(order_type="MKT", limit_price=None, reference_price=99.5)
    OrderPolicy(max_notional=995).check(market)
    assert "10 x 99.5 reference price" in refusal(OrderPolicy(max_notional=994), market)


def test_market_order_without_price_is_refused_when_notional_is_capped() -> None:
    market = order(order_type="MKT", limit_price=None)
    message = refusal(OrderPolicy(max_notional=1_000), market)
    assert "a maximum notional of 1,000 USD is set" in message
    assert "notional cannot be checked; make market data" in message
    # Without a notional cap the same order passes.
    OrderPolicy(max_quantity=100).check(market)


@pytest.mark.parametrize("unset", [None, float("nan"), float("inf"), UNSET_DOUBLE, ""])
def test_unset_prices_are_normalized_to_none(unset: Any) -> None:
    summary = order(order_type="MKT", limit_price=unset, aux_price=unset, reference_price=unset)
    assert summary.limit_price is None
    assert summary.aux_price is None
    assert summary.reference_price is None
    assert estimate_notional(summary) is None


@pytest.mark.parametrize("unset", [None, "", float("nan"), UNSET_DOUBLE])
def test_unset_multiplier_counts_as_one(unset: Any) -> None:
    summary = order(multiplier=unset)
    assert summary.multiplier is None
    estimate = estimate_notional(summary)
    assert estimate is not None
    assert estimate.value == Decimal("1000.0")


def test_decimal_inputs_are_accepted() -> None:
    summary = order(quantity=Decimal("3"), limit_price=Decimal("33.33"))
    OrderPolicy(max_notional=Decimal("99.99")).check(summary)


# -- combo notional ---------------------------------------------------------


def test_combo_notional_sums_leg_prices_when_all_known() -> None:
    pair = spread(
        quantity=10,
        limit_price=None,
        legs=(
            leg("AAPL", sec_type="STK", price=100),
            leg("MSFT", sec_type="STK", action="SELL", ratio=2, price=50),
        ),
    )
    estimate = estimate_notional(pair)
    assert estimate is not None
    assert estimate.value == Decimal("2000.0")  # 10*100 + 20*50, gross
    OrderPolicy(max_notional=2_000).check(pair)
    assert "notional 2,000.00 USD (sum over legs" in refusal(OrderPolicy(max_notional=1_999), pair)


def test_combo_leg_prices_use_leg_multipliers() -> None:
    vertical = spread(
        quantity=2,
        legs=(
            leg("SPY", multiplier=100, price=5.0),
            leg("SPY", action="SELL", multiplier=100, price=3.5),
        ),
    )
    estimate = estimate_notional(vertical)
    assert estimate is not None
    assert estimate.value == Decimal("1700.0")  # 2*100*(5.0 + 3.5)


def test_combo_with_an_unpriced_leg_has_no_notional() -> None:
    """The net price bounds neither leg, so it never stands in for the gross notional."""
    credit = spread(
        quantity=10,
        limit_price=-1.5,
        legs=(leg("SPY", multiplier=100, price=5.0), leg("SPY", action="SELL", multiplier=100)),
    )
    assert estimate_notional(credit) is None
    message = refusal(OrderPolicy(max_notional=1_000_000), credit)
    assert "combo leg SPY (OPT) has no reference price" in message
    assert "net price bounds neither leg" in message
    OrderPolicy().check(credit)  # no notional limit: nothing to check


def test_a_cheap_net_buy_limit_cannot_hide_a_big_pair() -> None:
    """Review finding: BUY 10,000 F / SELL 10,000 NVDA at a 0.01 debit, NVDA unpriced."""
    pair = spread(
        quantity=10_000,
        limit_price=0.01,
        legs=(
            leg("F", sec_type="STK", price=12.0),
            leg("NVDA", sec_type="STK", action="SELL"),
        ),
    )
    message = refusal(OrderPolicy(max_notional=10_000), pair)
    assert "combo leg NVDA (STK) has no reference price" in message
    assert "combo leg F" not in message
    priced = spread(
        quantity=10_000,
        limit_price=0.01,
        legs=(
            leg("F", sec_type="STK", price=12.0),
            leg("NVDA", sec_type="STK", action="SELL", price=148.0),
        ),
    )
    assert "notional 1,600,000.00 USD" in refusal(OrderPolicy(max_notional=10_000), priced)


def test_combo_multiplier_field_does_not_replace_leg_prices() -> None:
    assert estimate_notional(spread(quantity=1, limit_price=2, multiplier=50)) is None


def test_combo_without_any_price_is_refused() -> None:
    message = refusal(OrderPolicy(max_notional=10_000), spread(order_type="MKT", limit_price=None))
    assert message.count("combo leg SPY (OPT) has no reference price") == 1


# -- prices the fill can exceed ---------------------------------------------
# The model chooses the order fields, so only a price that caps the fill may stand
# for the order's notional (review finding: the rail could be sidestepped).


def test_a_sell_limit_is_a_floor_not_a_cap() -> None:
    dump = order(action="SELL", quantity=10_000, limit_price=0.01, reference_price=200)
    message = refusal(OrderPolicy(max_notional=10_000), dump)
    assert "notional 2,000,000.00 USD (10,000 x 200 reference price x 1 multiplier)" in message


def test_a_sell_limit_needs_a_reference_price_when_notional_is_capped() -> None:
    message = refusal(OrderPolicy(max_notional=10_000), order(action="SELL", limit_price=1.0))
    assert "nothing bounds the fill price of this SELL LMT order" in message


def test_a_sell_limit_above_the_market_counts_at_its_limit() -> None:
    high = order(action="SELL", quantity=10, limit_price=250, reference_price=200)
    estimate = estimate_notional(high)
    assert estimate == NotionalEstimate(Decimal("2500.0"), "10 x 250 limit price x 1 multiplier")


def test_a_buy_stop_below_the_market_counts_at_the_market() -> None:
    stop = order(order_type="STP", limit_price=None, aux_price=0.01, quantity=10_000)
    message = refusal(
        OrderPolicy(max_notional=10_000), stop.model_copy(update={"reference_price": 200})
    )
    assert "10,000 x 200 reference price" in message
    assert "nothing bounds" in refusal(OrderPolicy(max_notional=10_000), stop)


def test_a_stray_limit_price_on_a_market_order_is_not_trusted() -> None:
    market = order(order_type="MKT", limit_price=0.01, quantity=10_000, reference_price=200)
    assert "10,000 x 200 reference price" in refusal(OrderPolicy(max_notional=10_000), market)


def test_a_buy_limit_far_below_the_market_is_bounded_by_its_limit() -> None:
    # It cannot fill above 0.01, so 100 is the most it can cost.
    cheap = order(quantity=10_000, limit_price=0.01, reference_price=200)
    OrderPolicy(max_notional=100).check(cheap)


@pytest.mark.parametrize("sec_type", ["OPT", "FOP", "FUT", "CONTFUT", "WAR", "IOPT"])
@pytest.mark.parametrize("multiplier", [None, ""])
def test_derivatives_without_a_multiplier_are_refused(sec_type: str, multiplier: Any) -> None:
    contract = order(
        symbol="ES", sec_type=sec_type, quantity=1, limit_price=5000, multiplier=multiplier
    )
    message = refusal(OrderPolicy(max_notional=10_000), contract)
    assert f"the multiplier of this {sec_type} contract (ES) is unknown" in message
    # Without a notional cap there is nothing to compute, so the order passes.
    OrderPolicy(max_quantity=10).check(contract)


def test_combo_legs_without_a_multiplier_are_refused() -> None:
    vertical = spread(legs=(leg("SPY", multiplier=100), leg("SPY", action="SELL")))
    message = refusal(OrderPolicy(max_notional=1_000_000), vertical)
    assert "the multiplier of combo leg SPY (OPT) is unknown" in message


# -- several violations -----------------------------------------------------


def test_all_violations_are_reported_together() -> None:
    policy = OrderPolicy(
        max_notional=1_000, max_quantity=5, allowed_symbols=["AAPL"], allowed_sec_types=["STK"]
    )
    message = refusal(policy, order(symbol="TSLA", sec_type="CFD", quantity=50, limit_price=30))
    assert "symbol TSLA is not allowed" in message
    assert "security type CFD is not allowed" in message
    assert "quantity 50 exceeds the maximum of 5" in message
    assert "notional 1,500.00 USD" in message
    assert message.endswith(".")


# -- construction -----------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "abc", "-5"])
@pytest.mark.parametrize("name", ["max_notional", "max_quantity"])
def test_invalid_limits(name: str, bad: Any) -> None:
    with pytest.raises(ValueError, match=name):
        OrderPolicy(**{name: bad})


def test_limits_accept_decimal_float_int_and_str() -> None:
    policy = OrderPolicy(max_notional="1000.50", max_quantity=Decimal("25"))
    assert policy.max_notional == Decimal("1000.50")
    assert policy.max_quantity == Decimal("25")
    assert OrderPolicy(max_notional=0.1).max_notional == Decimal("0.1")


def test_describe_lists_active_limits() -> None:
    policy = OrderPolicy(
        max_notional=25_000,
        max_quantity=100,
        allowed_symbols=["spy", "aapl"],
        allowed_sec_types=["STK"],
    )
    assert policy.describe() == (
        "max notional 25,000 per order (order currency); max quantity 100; "
        "symbols AAPL, SPY; security types STK"
    )


def test_from_settings() -> None:
    settings = SimpleNamespace(
        max_notional=5_000.0,
        max_quantity=None,
        allowed_symbols=["AAPL"],
        allowed_sec_types=[],
    )
    policy = OrderPolicy.from_settings(settings)
    assert policy.max_notional == Decimal("5000.0")
    assert policy.max_quantity is None
    assert policy.allowed_symbols == frozenset({"AAPL"})
    assert policy.allowed_sec_types == frozenset()


# -- models -----------------------------------------------------------------


def test_summary_normalizes_codes() -> None:
    summary = order(
        symbol=" aapl ", sec_type="stk", action="buy", order_type="stp lmt", currency="usd"
    )
    assert (summary.symbol, summary.sec_type, summary.action) == ("aapl", "STK", "BUY")
    assert (summary.order_type, summary.currency) == ("STP LMT", "USD")


def test_summary_is_frozen_and_strict() -> None:
    summary = order()
    with pytest.raises(ValidationError):
        summary.quantity = 1_000  # type: ignore[misc]
    with pytest.raises(ValidationError):
        order(unexpected=True)
    with pytest.raises(ValidationError):
        order(quantity=float("inf"))
    with pytest.raises(ValidationError):
        order(symbol="  ")
    with pytest.raises(ValidationError):
        order(multiplier=-100)


def test_leg_ratio_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        leg("SPY", ratio=0)


def test_summary_round_trips_through_json() -> None:
    summary = spread()
    assert OrderSummary.model_validate_json(summary.model_dump_json()) == summary


def test_order_limit_error_is_a_safety_error() -> None:
    assert issubclass(OrderLimitError, SafetyError)


@pytest.mark.parametrize("sec_type", ["BOND", "EVENT"])
def test_unboundable_sec_types_are_refused_under_a_notional_limit(sec_type: str) -> None:
    """IBKR quotes bonds in percent of face value: 100 x 99.5 is about 99,500 USD, not 9,950."""
    summary = OrderSummary(
        symbol="X",
        sec_type=sec_type,
        action="BUY",
        quantity=100,
        order_type="LMT",
        limit_price=99.5,
    )
    with pytest.raises(OrderLimitError, match="cannot be bounded by quantity x price"):
        OrderPolicy(max_notional=10_000).check(summary)
    OrderPolicy().check(summary)  # no notional limit, nothing to bound
    leg = LegSummary(symbol="X", sec_type=sec_type, action="BUY", price=99.5)
    combo = OrderSummary(
        symbol="X", sec_type="BAG", action="BUY", quantity=1, order_type="LMT", legs=(leg,)
    )
    with pytest.raises(OrderLimitError, match="combo leg X"):
        OrderPolicy(max_notional=10_000).check(combo)


def test_allowed_currencies() -> None:
    policy = OrderPolicy(allowed_currencies="usd, eur")
    assert policy.allowed_currencies == frozenset({"USD", "EUR"})
    assert "currencies EUR, USD" in policy.describe()
    gbp = OrderSummary(
        symbol="VOD", action="BUY", quantity=1, order_type="LMT", limit_price=1.0, currency="GBP"
    )
    with pytest.raises(OrderLimitError, match="currency GBP is not allowed"):
        policy.check(gbp)
    policy.check(gbp.model_copy(update={"currency": "EUR"}))
