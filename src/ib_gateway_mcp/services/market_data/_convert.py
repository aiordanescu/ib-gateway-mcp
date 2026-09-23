"""Converters from ib_async tickers, ticks and bars to the market data models."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from ib_async import (
    IB,
    BarData,
    Contract,
    RealTimeBar,
    TickByTickAllLast,
    TickByTickBidAsk,
    TickByTickMidPoint,
    Ticker,
)

from ib_gateway_mcp._ib_compat import ticker_request_id
from ib_gateway_mcp._util import (
    clean_float,
    clean_int,
    clean_str,
    ensure_utc,
    is_informational,
    utc_now,
)
from ib_gateway_mcp.errors import (
    IbApiError,
    IbGatewayMcpError,
    InvalidRequestError,
    SubscriptionLimitError,
)
from ib_gateway_mcp.models.common import ContractSpec, QuoteOut
from ib_gateway_mcp.models.market_data import (
    GENERIC_TICK_IDS,
    BidAskTick,
    CancelledSubscription,
    DepthLevel,
    DividendsOut,
    GenericTick,
    MidPointTick,
    QuoteError,
    QuoteExtras,
    RealtimeBar,
    StreamBar,
    TickByTickType,
    TradeTick,
)
from ib_gateway_mcp.services.base import describe_spec
from ib_gateway_mcp.services.market_data._constants import (
    _HINTS,
    _LIMIT_CODES,
    _NOTICE_CODES,
    _TIME_KEYS,
)

if TYPE_CHECKING:
    from ib_gateway_mcp.subscriptions import SubscriptionInfo


# --- helpers ------------------------------------------------------------------------------


def _is_notice(code: int) -> bool:
    return code in _NOTICE_CODES or is_informational(code)


def _with_hint(exc: IbApiError, hints: Mapping[int, str] = _HINTS) -> IbGatewayMcpError:
    """``exc`` with the hint for its code, as a limit error where that fits."""
    hint = hints.get(exc.error_code)
    if hint is None:
        return exc
    hinted = exc.with_hint(hint)
    if exc.error_code in _LIMIT_CODES:
        return SubscriptionLimitError(str(hinted))
    return hinted


def _describe(contract: Contract) -> str:
    name = contract.localSymbol or contract.symbol or "combo"
    if contract.conId:
        return f"{name} {contract.secType} (con_id {contract.conId})"
    return f"{name} {contract.secType}"


def _contract_key(contract: Contract) -> str:
    """The registry key for a stream on ``contract``: its conId (combos: their legs)."""
    if contract.secType == "BAG":
        legs = ",".join(
            f"{leg.conId}x{leg.ratio}{leg.action}@{leg.exchange}"
            for leg in sorted(contract.comboLegs or [], key=lambda leg: leg.conId)
        )
        return f"BAG:{contract.symbol}:{contract.exchange}:{legs}"
    return str(contract.conId)


def _ticker_req_id(ib: IB, ticker: Ticker, request_key: str) -> int | None:
    """The request id ib_async assigned to ``ticker``'s ``request_key`` stream, if known."""
    return ticker_request_id(ib.wrapper, ticker, request_key)


def _update_event(source: object) -> Any:
    """The eventkit ``updateEvent`` of a Ticker or bar list (untyped in ib_async)."""
    return cast("Any", source).updateEvent


def _sorted_ticks(ticks: Iterable[GenericTick]) -> list[GenericTick]:
    return sorted(set(ticks), key=lambda name: GENERIC_TICK_IDS[name])


def _checked_ticks(ticks: Iterable[str]) -> list[GenericTick]:
    """``ticks`` sorted by id, deduplicated; unknown names are an invalid request."""
    names = list(ticks) if not isinstance(ticks, str) else [ticks]
    unknown = sorted({name for name in names if name not in GENERIC_TICK_IDS})
    if unknown:
        raise InvalidRequestError(
            f"Unknown generic tick(s) {', '.join(map(repr, unknown))}; valid names: "
            f"{', '.join(GENERIC_TICK_IDS)}."
        )
    return _sorted_ticks(cast("list[GenericTick]", names))


def _generic_tick_list(ticks: Iterable[GenericTick]) -> str:
    return ",".join(str(GENERIC_TICK_IDS[name]) for name in _sorted_ticks(ticks))


def _nonnegative(value: float | int | None) -> float | None:
    """A size, volume or count; IBKR sends -1 when it has none."""
    number = clean_float(value)
    return None if number is None or number < 0 else number


def _tick_price(price: float | None, size: float | None) -> float | None:
    """A trade or quote price; None for IBKR's empty side (no size, price 0 or -1)."""
    value = clean_float(price)
    if value is None:
        return None
    if value <= 0 and not clean_float(size):
        return None
    return value


def _item_time(item: Mapping[str, Any]) -> datetime | None:
    for key in _TIME_KEYS:
        value = item.get(key)
        if isinstance(value, str):
            try:
                return ensure_utc(datetime.fromisoformat(value))
            except ValueError:
                return None
    return None


def _window(data: dict[str, Any], *, limit: int, since: datetime | None) -> bool:
    """Cut every time series in ``data`` to items after ``since``, newest ``limit`` last.

    A time series is a list of objects with a ``time`` (or ``date``) field: ticks, bars,
    headlines. Other lists (order book levels, scanner rows) are left alone. Returns
    whether anything was cut by ``limit``.
    """
    truncated = False
    for name, value in list(data.items()):
        if not (isinstance(value, list) and value and all(isinstance(i, dict) for i in value)):
            continue
        if not any(key in value[0] for key in _TIME_KEYS):
            continue
        items: list[dict[str, Any]] = value
        if since is not None:
            items = [item for item in items if (t := _item_time(item)) is None or t > since]
        if len(items) > limit:
            items = items[-limit:]
            truncated = True
        data[name] = items
    return truncated


def _json_safe(value: object) -> bool:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list | tuple):
        return all(_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _json_safe(v) for k, v in value.items())
    return False


def _params(meta: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in meta.items() if k != "contract" and _json_safe(v)}


def _cancelled(info: SubscriptionInfo) -> CancelledSubscription:
    return CancelledSubscription(subscription_id=info.id, kind=info.kind, key=info.key)


def _quote_error(spec: ContractSpec, exc: IbGatewayMcpError) -> QuoteError:
    return QuoteError(
        contract=describe_spec(spec),
        code=exc.code,
        ib_error_code=exc.error_code if isinstance(exc, IbApiError) else None,
        message=str(exc),
    )


def _extras(ticker: Ticker) -> QuoteExtras:
    ratios = ticker.fundamentalRatios
    dividends = ticker.dividends
    next_date = dividends.nextDate if dividends is not None else None
    if isinstance(next_date, datetime):
        next_date = next_date.date()
    return QuoteExtras(
        put_volume=_nonnegative(ticker.putVolume),
        call_volume=_nonnegative(ticker.callVolume),
        put_open_interest=_nonnegative(ticker.putOpenInterest),
        call_open_interest=_nonnegative(ticker.callOpenInterest),
        historical_volatility=clean_float(ticker.histVolatility),
        avg_option_volume=_nonnegative(ticker.avOptionVolume),
        implied_volatility=clean_float(ticker.impliedVolatility),
        index_future_premium=clean_float(ticker.indexFuturePremium),
        low_13_week=clean_float(ticker.low13week),
        high_13_week=clean_float(ticker.high13week),
        low_26_week=clean_float(ticker.low26week),
        high_26_week=clean_float(ticker.high26week),
        low_52_week=clean_float(ticker.low52week),
        high_52_week=clean_float(ticker.high52week),
        avg_volume=_nonnegative(ticker.avVolume),
        mark_price=clean_float(ticker.markPrice),
        auction_volume=_nonnegative(ticker.auctionVolume),
        auction_price=clean_float(ticker.auctionPrice),
        auction_imbalance=clean_float(ticker.auctionImbalance),
        rt_volume=_nonnegative(ticker.rtVolume),
        rt_time=ensure_utc(ticker.rtTime),
        vwap=clean_float(ticker.vwap),
        shortable=clean_float(ticker.shortable),
        shortable_shares=_nonnegative(ticker.shortableShares),
        fundamental_ratios=(
            {
                name: clean_float(value) if isinstance(value, int | float) else clean_str(value)
                for name, value in vars(ratios).items()
                if isinstance(value, int | float | str)
            }
            if ratios is not None
            else None
        ),
        trade_count=_nonnegative(ticker.tradeCount),
        trade_rate=_nonnegative(ticker.tradeRate),
        volume_rate=_nonnegative(ticker.volumeRate),
        rt_trade_volume=_nonnegative(ticker.rtTradeVolume),
        rt_historical_volatility=clean_float(ticker.rtHistVolatility),
        dividends=(
            DividendsOut(
                past_12_months=clean_float(dividends.past12Months),
                next_12_months=clean_float(dividends.next12Months),
                next_date=next_date,
                next_amount=clean_float(dividends.nextAmount),
            )
            if dividends is not None
            else None
        ),
        futures_open_interest=_nonnegative(ticker.futuresOpenInterest),
        last_rth_trade=clean_float(ticker.lastRthTrade),
        bond_factor_multiplier=clean_float(ticker.bondFactorMultiplier),
        etf_nav_bid=clean_float(ticker.etfNavBid),
        etf_nav_ask=clean_float(ticker.etfNavAsk),
        etf_nav_last=clean_float(ticker.etfNavLast),
        etf_nav_close=clean_float(ticker.etfNavClose),
        etf_nav_prior_close=clean_float(ticker.etfNavPriorClose),
        etf_nav_high=clean_float(ticker.etfNavHigh),
        etf_nav_low=clean_float(ticker.etfNavLow),
        etf_nav_frozen_last=clean_float(ticker.etfFrozenNavLast),
        estimated_ipo_midpoint=clean_float(ticker.estimatedIpoMidpoint),
        final_ipo_last=clean_float(ticker.finalIpoLast),
        volume_3_min=_nonnegative(ticker.volumeRate3Min),
        volume_5_min=_nonnegative(ticker.volumeRate5Min),
        volume_10_min=_nonnegative(ticker.volumeRate10Min),
    )


def _levels(by_position: Mapping[int, Any], ordered: Sequence[Any]) -> list[DepthLevel]:
    items = sorted(by_position.items()) if by_position else list(enumerate(ordered))
    return [
        DepthLevel(
            position=position,
            price=clean_float(level.price),
            size=_nonnegative(level.size),
            market_maker=clean_str(level.marketMaker),
        )
        for position, level in items
    ]


def _tick_time(value: datetime | None) -> datetime:
    return ensure_utc(value) if value is not None else utc_now()


def _trade_tick(tick: TickByTickAllLast) -> TradeTick:
    attrib = tick.tickAttribLast
    return TradeTick(
        time=_tick_time(tick.time),
        price=_tick_price(tick.price, tick.size),
        size=_nonnegative(tick.size),
        exchange=clean_str(tick.exchange),
        special_conditions=clean_str(tick.specialConditions),
        past_limit=bool(getattr(attrib, "pastLimit", False)),
        unreported=bool(getattr(attrib, "unreported", False)),
    )


def _bid_ask_tick(tick: TickByTickBidAsk) -> BidAskTick:
    attrib = tick.tickAttribBidAsk
    return BidAskTick(
        time=_tick_time(tick.time),
        bid=_tick_price(tick.bidPrice, tick.bidSize),
        ask=_tick_price(tick.askPrice, tick.askSize),
        bid_size=_nonnegative(tick.bidSize),
        ask_size=_nonnegative(tick.askSize),
        bid_past_low=bool(getattr(attrib, "bidPastLow", False)),
        ask_past_high=bool(getattr(attrib, "askPastHigh", False)),
    )


_LAST_TICK_TYPES: Mapping[str, int] = MappingProxyType({"Last": 1, "AllLast": 2})
"""The ``tickType`` ib_async reports on a ``TickByTickAllLast`` for each request type."""


def _convert_tick(
    tick: object, tick_type: TickByTickType
) -> TradeTick | BidAskTick | MidPointTick | None:
    """Convert one ib_async tick-by-tick tick, or None if it belongs to another tick type.

    Every tick-by-tick stream of a contract lands in the same ``Ticker.tickByTicks``.
    """
    if isinstance(tick, TickByTickAllLast) and tick.tickType == _LAST_TICK_TYPES.get(tick_type):
        return _trade_tick(tick)
    if isinstance(tick, TickByTickBidAsk) and tick_type == "BidAsk":
        return _bid_ask_tick(tick)
    if isinstance(tick, TickByTickMidPoint) and tick_type == "MidPoint":
        return MidPointTick(time=_tick_time(tick.time), mid_point=clean_float(tick.midPoint))
    return None


def _realtime_bar(bar: RealTimeBar) -> RealtimeBar:
    return RealtimeBar(
        time=_tick_time(bar.time),
        open=clean_float(bar.open_),
        high=clean_float(bar.high),
        low=clean_float(bar.low),
        close=clean_float(bar.close),
        volume=_nonnegative(bar.volume),
        wap=_nonnegative(bar.wap),
        count=clean_int(_nonnegative(bar.count)),
    )


def _stream_bar(bar: BarData) -> StreamBar:
    return StreamBar(
        time=ensure_utc(bar.date),
        open=clean_float(bar.open),
        high=clean_float(bar.high),
        low=clean_float(bar.low),
        close=clean_float(bar.close),
        volume=_nonnegative(bar.volume),
        average=_nonnegative(bar.average),
        bar_count=clean_int(_nonnegative(bar.barCount)),
    )


def _quote_notices(quotes: Sequence[QuoteOut], empty: Sequence[str], fees: int) -> list[str]:
    notices: list[str] = []
    if fees:
        notices.append(
            f"Requested {fees} regulatory snapshot(s); IBKR bills about USD 0.01 each for US "
            "stocks and ETFs without a live subscription."
        )
    if empty:
        notices.append(
            f"No prices for {', '.join(empty)}: the market may be closed with no previous "
            "close, or the login lacks market data permissions (try set_market_data_type "
            "with 'delayed')."
        )
    if any(q.market_data_type in ("delayed", "delayed_frozen") for q in quotes):
        notices.append("Quotes marked delayed are 15-20 minutes old.")
    if any(q.market_data_type in ("frozen", "delayed_frozen") for q in quotes):
        notices.append("Quotes marked frozen are the last values before the close.")
    return notices
