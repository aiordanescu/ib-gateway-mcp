"""Small conversions shared by the services.

ib_async reports "no value" in several ways: ``nan`` for missing prices, ``UNSET_DOUBLE``
(``sys.float_info.max``) and ``UNSET_INTEGER`` (``2**31 - 1``) for unset order fields,
and empty strings for absent text. JSON can carry none of these meaningfully, so the
helpers here turn all of them into ``None``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, overload

from ib_async import ComboLeg, Contract, OptionComputation, Ticker
from ib_async.util import UNSET_DOUBLE, UNSET_INTEGER

from ib_gateway_mcp.models.common import (
    MARKET_DATA_TYPE_NAMES,
    ComboLegOut,
    ContractOut,
    ContractSpec,
    OptionGreeks,
    QuoteOut,
    SubscriptionOut,
)

if TYPE_CHECKING:
    from ib_gateway_mcp.subscriptions import SubscriptionInfo

__all__ = [
    "best_effort",
    "clamp_limit",
    "clean_float",
    "clean_int",
    "clean_str",
    "contract_from_spec",
    "contract_to_out",
    "ensure_utc",
    "exact_decimal",
    "fop_exchange",
    "greeks_from_computation",
    "is_informational",
    "option_underlying_sec_type",
    "quote_from_ticker",
    "subscription_out",
    "truncate",
    "utc_now",
]

logger = logging.getLogger(__name__)

_EMPTY_PRICE = -1.0
"""IBKR's price for an empty book side (sent with size 0); see ``IBDefaults.emptyPrice``."""
_NOT_COMPUTED_PRICE = -1.0
"""IBKR's marker for an uncomputed implied volatility, option, dividend or underlying price."""
_NOT_COMPUTED_GREEK = -2.0
"""IBKR's marker for an uncomputed delta, gamma, vega or theta."""


def best_effort(action: Callable[[], object], what: str) -> None:
    """Run a cleanup call (a cancel) whose failure must not mask the real outcome.

    Failures are logged at debug level (``"Could not <what>"``), never raised.
    """
    try:
        action()
    except Exception:
        logger.debug("Could not %s", what, exc_info=True)


def is_informational(code: int) -> bool:
    """IBKR's 2100-2199 codes: connection and data-farm status notices, not failures."""
    return 2100 <= code < 2200


def exact_decimal(value: float | Decimal) -> Decimal:
    """The exact decimal of a float's shortest repr (0.1 -> ``Decimal('0.1')``)."""
    return value if isinstance(value, Decimal) else Decimal(repr(float(value)))


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def clean_float(value: float | int | str | None) -> float | None:
    """Return ``value`` as a float, or None for NaN, infinities, IBKR's unset marker and blanks."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number == UNSET_DOUBLE:
        return None
    return number


def clean_int(value: int | float | str | None) -> int | None:
    """Return ``value`` as an int, or None for IBKR's unset markers, NaN and blanks."""
    number = clean_float(value)
    if number is None or number == UNSET_INTEGER:
        return None
    return int(number)


def clean_str(value: str | None) -> str | None:
    """Return a stripped string, or None when it is empty."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@overload
def ensure_utc(value: datetime | date) -> datetime: ...
@overload
def ensure_utc(value: None) -> None: ...
def ensure_utc(value: datetime | date | None) -> datetime | None:
    """Make a datetime timezone-aware in UTC.

    Naive datetimes are assumed to already be UTC; plain dates become midnight UTC.
    """
    if value is None:
        return None
    if not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def clamp_limit(limit: int | None, *, default: int, maximum: int) -> int:
    """Resolve a caller's ``limit``: ``default`` when None, capped at ``maximum``, at least 1."""
    if limit is None:
        return default
    return max(1, min(limit, maximum))


def truncate[T](items: Sequence[T], limit: int) -> tuple[list[T], bool]:
    """Return the first ``limit`` items and whether anything was cut."""
    return list(items[:limit]), len(items) > limit


def contract_from_spec(spec: ContractSpec) -> Contract:
    """Build an ib_async :class:`~ib_async.Contract` from a :class:`ContractSpec`.

    The result is not qualified; services call ``BaseService.qualify`` when they need
    IBKR's canonical contract.
    """
    fields: dict[str, Any] = {
        "secType": spec.sec_type,
        "conId": spec.con_id or 0,
        "symbol": spec.symbol or "",
        "lastTradeDateOrContractMonth": spec.last_trade_date_or_contract_month or "",
        "strike": spec.strike or 0.0,
        "right": spec.right or "",
        "multiplier": spec.multiplier or "",
        "exchange": spec.exchange,
        "primaryExchange": spec.primary_exchange or "",
        "currency": spec.currency,
        "localSymbol": spec.local_symbol or "",
        "tradingClass": spec.trading_class or "",
        "includeExpired": spec.include_expired,
        "secIdType": spec.sec_id_type or "",
        "secId": spec.sec_id or "",
        "issuerId": spec.issuer_id or "",
    }
    if spec.combo_legs:
        fields["comboLegs"] = [
            ComboLeg(conId=leg.con_id, ratio=leg.ratio, action=leg.action, exchange=leg.exchange)
            for leg in spec.combo_legs
        ]
    contract = Contract.create(**fields)
    if contract.secType != spec.sec_type:
        # Contract.create maps some types onto a class with a fixed secType (IOPT becomes
        # a Warrant, i.e. WAR); keep what the caller asked for.
        contract = Contract(**fields)
    return contract


def option_underlying_sec_type(sec_type: str) -> str:
    """The security type IBKR lists options on for an underlying of ``sec_type``.

    A continuous future (CONTFUT) stands for its front-month future, which is what
    IBKR lists futures options (FOP) on.
    """
    return "FUT" if sec_type == "CONTFUT" else sec_type


def fop_exchange(contract: Contract) -> str:
    """``reqSecDefOptParams``' ``futFopExchange`` for an underlying: its exchange for a
    future (or continuous future), empty otherwise."""
    return contract.exchange if option_underlying_sec_type(contract.secType) == "FUT" else ""


def contract_to_out(contract: Contract, *, description: str | None = None) -> ContractOut:
    """Convert an ib_async contract into the public :class:`ContractOut` model."""
    return ContractOut(
        con_id=contract.conId or None,
        symbol=contract.symbol,
        sec_type=contract.secType,
        exchange=clean_str(contract.exchange),
        primary_exchange=clean_str(contract.primaryExchange),
        currency=clean_str(contract.currency),
        local_symbol=clean_str(contract.localSymbol),
        trading_class=clean_str(contract.tradingClass),
        last_trade_date_or_contract_month=clean_str(contract.lastTradeDateOrContractMonth),
        strike=clean_float(contract.strike) or None,
        right=clean_str(contract.right),
        multiplier=clean_str(contract.multiplier),
        description=clean_str(description) or clean_str(contract.description),
        combo_legs=[
            ComboLegOut(con_id=leg.conId, ratio=leg.ratio, action=leg.action, exchange=leg.exchange)
            for leg in contract.comboLegs or []
        ],
    )


def _book_price(price: float | None, size: float | None) -> float | None:
    """A bid, ask or last price, or None for IBKR's empty-side marker (-1 with no size)."""
    value = clean_float(price)
    if value == _EMPTY_PRICE and not clean_float(size):
        return None
    return value


def _halted(ticker: Ticker) -> bool | None:
    """IBKR's halted tick: -1 unknown, 0 trading, 1 general halt, 2 volatility halt."""
    value = clean_float(ticker.halted)
    if value is None:
        value = clean_float(ticker.delayedHalted)
    if value is None or value < 0:
        return None
    return value > 0


def greeks_from_computation(computation: OptionComputation | None) -> OptionGreeks | None:
    """Convert an ib_async ``OptionComputation`` into :class:`OptionGreeks`.

    NaN and IBKR's "not computed" markers (-1 for prices and volatility, -2 for greeks)
    become None. Returns None when there is no computation or it holds no value at all.
    """
    if computation is None:
        return None

    def price(value: float | None) -> float | None:
        number = clean_float(value)
        return None if number == _NOT_COMPUTED_PRICE else number

    def greek(value: float | None) -> float | None:
        number = clean_float(value)
        return None if number == _NOT_COMPUTED_GREEK else number

    greeks = OptionGreeks(
        implied_vol=price(computation.impliedVol),
        delta=greek(computation.delta),
        gamma=greek(computation.gamma),
        vega=greek(computation.vega),
        theta=greek(computation.theta),
        opt_price=price(computation.optPrice),
        pv_dividend=price(computation.pvDividend),
        und_price=price(computation.undPrice),
    )
    if all(value is None for value in greeks.model_dump().values()):
        return None
    return greeks


def quote_from_ticker(ticker: Ticker, contract: ContractOut) -> QuoteOut:
    """Convert an ib_async ``Ticker`` into a :class:`QuoteOut` for ``contract``.

    NaN and IBKR's empty-side price (-1 with size 0) become None. Delayed ticks land in
    the same ``Ticker`` fields, so this works for every market data type. Greeks come
    from IBKR's model greeks, falling back to the greeks of the last trade.
    """
    return QuoteOut(
        contract=contract,
        bid=_book_price(ticker.bid, ticker.bidSize),
        ask=_book_price(ticker.ask, ticker.askSize),
        last=_book_price(ticker.last, ticker.lastSize),
        bid_size=clean_float(ticker.bidSize),
        ask_size=clean_float(ticker.askSize),
        last_size=clean_float(ticker.lastSize),
        open=clean_float(ticker.open),
        high=clean_float(ticker.high),
        low=clean_float(ticker.low),
        close=clean_float(ticker.close),
        volume=clean_float(ticker.volume),
        halted=_halted(ticker),
        market_data_type=MARKET_DATA_TYPE_NAMES.get(ticker.marketDataType),
        bbo_exchange=clean_str(ticker.bboExchange),
        time=ensure_utc(ticker.time),
        greeks=greeks_from_computation(ticker.modelGreeks or ticker.lastGreeks),
    )


def subscription_out(
    info: SubscriptionInfo, *, idle_ttl_s: float, deduplicated: bool = False
) -> SubscriptionOut:
    """Describe a registry entry as a :class:`SubscriptionOut`.

    The contract comes from ``info.meta["contract"]`` (a dumped :class:`ContractOut`),
    which ``BaseService._subscribe`` stores there.
    """
    contract = info.meta.get("contract")
    return SubscriptionOut(
        subscription_id=info.id,
        kind=info.kind,
        key=info.key,
        contract=ContractOut.model_validate(contract) if contract else None,
        created_at=info.created_at,
        idle_ttl_s=idle_ttl_s,
        deduplicated=deduplicated,
    )
