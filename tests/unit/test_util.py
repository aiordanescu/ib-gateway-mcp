"""Shared conversions and the ContractSpec model."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta, timezone
from typing import get_args

import pytest
from ib_async import Bag, Option, Stock
from ib_async.util import UNSET_DOUBLE, UNSET_INTEGER
from pydantic import ValidationError

from ib_gateway_mcp._util import (
    clamp_limit,
    clean_float,
    clean_int,
    clean_str,
    contract_from_spec,
    contract_to_out,
    ensure_utc,
    greeks_from_computation,
    quote_from_ticker,
    subscription_out,
    truncate,
    utc_now,
)
from ib_gateway_mcp.models.common import (
    MARKET_DATA_TYPE_CODES,
    MARKET_DATA_TYPE_NAMES,
    BarSize,
    ComboLegSpec,
    ContractSpec,
    QuoteOut,
    RealtimeWhatToShow,
    SecType,
    SubscriptionOut,
    WhatToShow,
)
from ib_gateway_mcp.subscriptions import SubscriptionInfo
from tests.fakes import FIXED_TIME, option, option_computation, stock, ticker


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.5, 1.5),
        (0, 0.0),
        ("2.25", 2.25),
        (math.nan, None),
        (math.inf, None),
        (-math.inf, None),
        (UNSET_DOUBLE, None),
        (None, None),
        ("", None),
        ("n/a", None),
    ],
)
def test_clean_float(value: float | str | None, expected: float | None) -> None:
    assert clean_float(value) == expected


def test_clean_int() -> None:
    assert clean_int(7) == 7
    assert clean_int("12") == 12
    assert clean_int(UNSET_INTEGER) is None
    assert clean_int(math.nan) is None


def test_clean_str() -> None:
    assert clean_str("  x ") == "x"
    assert clean_str("   ") is None
    assert clean_str(None) is None


def test_ensure_utc() -> None:
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert ensure_utc(naive) == naive.replace(tzinfo=UTC)
    eastern = datetime(2026, 1, 2, 10, 0, tzinfo=timezone(timedelta(hours=-5)))
    converted = ensure_utc(eastern)
    assert converted.tzinfo is UTC
    assert converted.hour == 15
    assert ensure_utc(date(2026, 1, 2)) == datetime(2026, 1, 2, tzinfo=UTC)
    assert ensure_utc(None) is None
    assert utc_now().tzinfo is UTC


def test_clamp_limit_and_truncate() -> None:
    assert clamp_limit(None, default=50, maximum=500) == 50
    assert clamp_limit(10_000, default=50, maximum=500) == 500
    assert clamp_limit(0, default=50, maximum=500) == 1
    assert truncate([1, 2, 3], 2) == ([1, 2], True)
    assert truncate([1, 2], 2) == ([1, 2], False)


def test_contract_from_spec_builds_a_stock() -> None:
    contract = contract_from_spec(ContractSpec(symbol="AAPL", primary_exchange="nasdaq"))
    assert isinstance(contract, Stock)
    assert (contract.symbol, contract.exchange, contract.currency) == ("AAPL", "SMART", "USD")
    assert contract.primaryExchange == "NASDAQ"
    assert contract.conId == 0


def test_contract_from_spec_builds_an_option() -> None:
    spec = ContractSpec(
        symbol="SPY",
        sec_type="OPT",
        last_trade_date_or_contract_month="20261218",
        strike=500,
        right="call",  # type: ignore[arg-type]  # normalized to "C"
        multiplier="100",
    )
    contract = contract_from_spec(spec)
    assert isinstance(contract, Option)
    assert (contract.right, contract.strike, contract.multiplier) == ("C", 500, "100")


def test_contract_from_spec_builds_a_combo() -> None:
    spec = ContractSpec(
        symbol="SPY",
        sec_type="BAG",
        combo_legs=[
            ComboLegSpec(con_id=1, action="BUY"),
            ComboLegSpec(con_id=2, ratio=2, action="SELL"),
        ],
    )
    contract = contract_from_spec(spec)
    assert isinstance(contract, Bag)
    assert [(leg.conId, leg.ratio, leg.action) for leg in contract.comboLegs] == [
        (1, 1, "BUY"),
        (2, 2, "SELL"),
    ]


@pytest.mark.parametrize("sec_type", get_args(SecType))
def test_contract_from_spec_keeps_every_sec_type(sec_type: str) -> None:
    # ib_async's Contract.create maps IOPT onto Warrant, which forces WAR.
    legs = (
        [ComboLegSpec(con_id=1, action="BUY"), ComboLegSpec(con_id=2, action="SELL")]
        if sec_type == "BAG"
        else []
    )
    spec = ContractSpec(symbol="X", sec_type=sec_type, combo_legs=legs)  # type: ignore[arg-type]
    assert contract_from_spec(spec).secType == sec_type


def test_contract_to_out() -> None:
    out = contract_to_out(stock(), description="Apple")
    assert out.con_id == 265598
    assert out.symbol == "AAPL"
    assert out.primary_exchange == "NASDAQ"
    assert out.strike is None
    assert out.right is None
    assert out.description == "Apple"

    opt = contract_to_out(option(strike=200.0, right="P"))
    assert (opt.sec_type, opt.strike, opt.right, opt.multiplier) == ("OPT", 200.0, "P", "100")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "at least one of"),
        ({"symbol": "SPY", "sec_type": "BAG"}, "at least two combo_legs"),
        (
            {"symbol": "SPY", "combo_legs": [{"con_id": 1, "action": "BUY"}]},
            "only valid with sec_type BAG",
        ),
        ({"sec_id": "US0378331005"}, "go together"),
        ({"sec_id_type": "ISIN"}, "go together"),
        ({"sec_id_type": "SEDOL", "sec_id": "2046251"}, "sec_id_type"),
        ({"symbol": "AAPL", "sec_id_type": "ISIN", "sec_id": "  "}, "go together"),
        ({"symbol": "AAPL", "unexpected": 1}, "Extra inputs"),
    ],
)
def test_contract_spec_validation(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ContractSpec.model_validate(kwargs)


def test_contract_spec_accepts_identifiers_without_symbol() -> None:
    assert ContractSpec(con_id=265598).con_id == 265598
    assert ContractSpec(sec_id_type="ISIN", sec_id="US0378331005").sec_id == "US0378331005"
    assert ContractSpec(sec_type="BOND", issuer_id="e1234567").issuer_id == "e1234567"


def test_contract_spec_normalizes_identifiers() -> None:
    spec = ContractSpec.model_validate({"sec_id_type": "figi", "sec_id": " BBG000B9XRY4 "})
    assert (spec.sec_id_type, spec.sec_id) == ("FIGI", "BBG000B9XRY4")
    assert ContractSpec(symbol="AAPL", issuer_id="  ").issuer_id is None


def test_contract_from_spec_maps_lookup_fields() -> None:
    contract = contract_from_spec(
        ContractSpec(
            sec_type="BOND",
            sec_id_type="CUSIP",
            sec_id="037833100",
            issuer_id="e1234567",
            include_expired=True,
        )
    )
    assert (contract.secIdType, contract.secId, contract.issuerId) == (
        "CUSIP",
        "037833100",
        "e1234567",
    )
    assert contract.includeExpired is True
    assert contract.symbol == ""

    plain = contract_from_spec(ContractSpec(symbol="ES", sec_type="FUT", exchange="CME"))
    assert (plain.secIdType, plain.secId, plain.issuerId, plain.includeExpired) == (
        "",
        "",
        "",
        False,
    )


# --- shared enums -------------------------------------------------------------------------


def test_bar_sizes_use_ib_spelling() -> None:
    sizes = get_args(BarSize)
    assert sizes[0] == "1 secs"
    assert sizes[-1] == "1 month"
    assert {"1 min", "2 mins", "1 hour", "8 hours", "1 day", "1 week"} <= set(sizes)
    assert len(sizes) == len(set(sizes)) == 21


def test_what_to_show_values() -> None:
    assert set(get_args(RealtimeWhatToShow)) <= set(get_args(WhatToShow))
    assert {"TRADES", "BID_ASK", "ADJUSTED_LAST", "OPTION_IMPLIED_VOLATILITY"} <= set(
        get_args(WhatToShow)
    )


def test_market_data_type_names_round_trip() -> None:
    assert MARKET_DATA_TYPE_CODES["delayed"] == 3
    assert {MARKET_DATA_TYPE_NAMES[code] for code in (1, 2, 3, 4)} == set(MARKET_DATA_TYPE_CODES)


# --- quotes and greeks --------------------------------------------------------------------


def test_quote_from_ticker() -> None:
    out = contract_to_out(stock())
    quote = quote_from_ticker(
        ticker(open=99.0, high=101.0, low=98.5, volume=12345.0, halted=0.0, bboExchange="9c0001"),
        out,
    )
    assert quote.contract == out
    assert (quote.bid, quote.ask, quote.last) == (99.5, 100.5, 100.0)
    assert (quote.bid_size, quote.ask_size, quote.last_size) == (100.0, 200.0, 10.0)
    assert (quote.open, quote.high, quote.low, quote.close) == (99.0, 101.0, 98.5, 98.0)
    assert quote.volume == 12345.0
    assert quote.halted is False
    assert quote.market_data_type == "live"
    assert quote.bbo_exchange == "9c0001"
    assert quote.time == FIXED_TIME
    assert quote.greeks is None
    # JSON-safe: no NaN anywhere.
    assert "NaN" not in quote.model_dump_json()


def test_quote_from_ticker_maps_missing_values_to_none() -> None:
    quote = quote_from_ticker(
        ticker(
            bid=-1.0,
            bidSize=0.0,
            ask=-1.0,
            askSize=0.0,
            last=math.nan,
            lastSize=math.nan,
            close=math.nan,
            time=None,
            marketDataType=3,
        ),
        contract_to_out(stock()),
    )
    assert (quote.bid, quote.ask, quote.last, quote.close) == (None, None, None, None)
    assert (quote.bid_size, quote.ask_size, quote.last_size) == (0.0, 0.0, None)
    assert quote.volume is None
    assert quote.halted is None
    assert quote.time is None
    assert quote.market_data_type == "delayed"
    assert quote.bbo_exchange is None
    QuoteOut.model_validate_json(quote.model_dump_json())


def test_quote_keeps_a_negative_price_with_size() -> None:
    # Spreads can trade below zero; only -1 with no size means "empty side".
    quote = quote_from_ticker(ticker(bid=-1.0, bidSize=5.0), contract_to_out(stock()))
    assert quote.bid == -1.0


@pytest.mark.parametrize(
    ("halted", "delayed_halted", "expected"),
    [
        (1.0, math.nan, True),
        (2.0, math.nan, True),
        (0.0, math.nan, False),
        (-1.0, math.nan, None),
        (math.nan, 1.0, True),
        (math.nan, math.nan, None),
    ],
)
def test_quote_halted(halted: float, delayed_halted: float, expected: bool | None) -> None:
    quote = quote_from_ticker(
        ticker(halted=halted, delayedHalted=delayed_halted), contract_to_out(stock())
    )
    assert quote.halted is expected


def test_quote_greeks_prefer_model_greeks() -> None:
    model = option_computation(delta=0.5)
    last = option_computation(delta=0.4)
    out = contract_to_out(option())
    both = quote_from_ticker(ticker(option(), modelGreeks=model, lastGreeks=last), out)
    assert both.greeks is not None
    assert both.greeks.delta == 0.5
    fallback = quote_from_ticker(ticker(option(), lastGreeks=last), out)
    assert fallback.greeks is not None
    assert fallback.greeks.delta == 0.4


def test_greeks_from_computation() -> None:
    greeks = greeks_from_computation(option_computation())
    assert greeks is not None
    assert greeks.model_dump() == {
        "implied_vol": 0.25,
        "delta": 0.52,
        "gamma": 0.015,
        "vega": 0.45,
        "theta": -0.06,
        "opt_price": 12.5,
        "pv_dividend": 0.8,
        "und_price": 200.0,
    }


def test_greeks_from_computation_drops_not_computed_markers() -> None:
    # ib_async maps most markers to None but passes vega/theta -2 through, and NaN happens.
    greeks = greeks_from_computation(
        option_computation(
            impliedVol=None, delta=math.nan, vega=-2.0, theta=-2.0, optPrice=-1.0, undPrice=-1.0
        )
    )
    assert greeks is not None
    assert (greeks.implied_vol, greeks.delta, greeks.vega, greeks.theta) == (None,) * 4
    assert (greeks.opt_price, greeks.und_price) == (None, None)
    assert greeks.gamma == 0.015


def test_greeks_from_computation_without_values() -> None:
    assert greeks_from_computation(None) is None
    empty = option_computation(
        impliedVol=None,
        delta=None,
        optPrice=None,
        pvDividend=None,
        gamma=None,
        vega=-2.0,
        theta=-2.0,
        undPrice=None,
    )
    assert greeks_from_computation(empty) is None


# --- subscriptions ------------------------------------------------------------------------


def test_subscription_out() -> None:
    contract = contract_to_out(stock())
    info = SubscriptionInfo(
        id="quotes-1",
        kind="quotes",
        key="265598",
        created_at=FIXED_TIME,
        meta={"contract": contract.model_dump(mode="json")},
    )
    out = subscription_out(info, idle_ttl_s=900.0, deduplicated=True)
    assert out == SubscriptionOut(
        subscription_id="quotes-1",
        kind="quotes",
        key="265598",
        contract=contract,
        created_at=FIXED_TIME,
        idle_ttl_s=900.0,
        deduplicated=True,
    )
    bare = SubscriptionInfo(
        id="news_bulletins-2", kind="news_bulletins", key="all", created_at=FIXED_TIME
    )
    assert subscription_out(bare, idle_ttl_s=60).contract is None
