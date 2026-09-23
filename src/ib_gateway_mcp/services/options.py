"""IBKR's option calculators (implied volatility, theoretical price) and chain-slice quotes."""

from __future__ import annotations

import asyncio
import bisect
import functools
import math
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, NoReturn

from ib_async import Contract, OptionChain, OptionComputation, Ticker

from ib_gateway_mcp._ib_compat import end_request
from ib_gateway_mcp._util import (
    best_effort,
    clamp_limit,
    clean_float,
    contract_to_out,
    fop_exchange,
    greeks_from_computation,
    option_underlying_sec_type,
    quote_from_ticker,
    truncate,
)
from ib_gateway_mcp.errors import (
    AmbiguousContractError,
    IbApiError,
    InvalidRequestError,
    NotFoundError,
    RequestTimeoutError,
    SubscriptionLimitError,
)
from ib_gateway_mcp.models.common import (
    MARKET_DATA_TYPE_NAMES,
    ContractOut,
    ContractSpec,
    OptionGreeks,
    QuoteOut,
    Right,
)
from ib_gateway_mcp.models.options import (
    ImpliedVolatilityOut,
    OptionPriceOut,
    OptionQuoteList,
    SkippedOptionLeg,
)
from ib_gateway_mcp.services.base import BaseService, describe_spec

if TYPE_CHECKING:
    from collections.abc import Callable

    from ib_gateway_mcp.models.common import SecType

__all__ = [
    "CALCULATION_TIMEOUT",
    "MAX_VOLATILITY",
    "QUOTES_LIMIT_DEFAULT",
    "QUOTES_LIMIT_MAX",
    "STRIKES_AROUND_ATM_DEFAULT",
    "OptionsService",
]

QUOTES_LIMIT_DEFAULT = 20
"""Option legs (strike and right pairs) ``option_quotes`` quotes by default."""
QUOTES_LIMIT_MAX = 40
"""Most legs one ``option_quotes`` call quotes: each leg holds a market-data line for up to
about 11 seconds, and IBKR allows 50 API messages per second."""
STRIKES_AROUND_ATM_DEFAULT = 5
"""Strikes nearest the underlying price ``option_quotes`` picks when no range is given."""
MAX_VOLATILITY = 10.0
"""Largest volatility the price calculator accepts (1000%); it is a decimal, not percent."""
CALCULATION_TIMEOUT = 4
"""Most seconds an option calculation waits for IBKR (less when ``IB_REQUEST_TIMEOUT`` is
shorter); IBKR's model answers well within it."""

_REQ_CALC_IMPLIED_VOLAT: Final = 54
"""TWS API message id of ``calculateImpliedVolatility``."""
_REQ_CALC_OPTION_PRICE: Final = 55
"""TWS API message id of ``calculateOptionPrice``."""
_CALCULATION_VERSION: Final = 3
"""Version field of both calculator messages."""
_NO_CALCULATION_OPTIONS: Final = ""
"""The calculators' misc-options field: an empty ``tag=value;`` list, sent without a count."""

_Leg = tuple[float, Right, Contract]
"""A qualified option leg: strike, right and the contract."""

_OPTION_TYPES = frozenset({"OPT", "FOP"})
_NOT_AN_UNDERLYING = frozenset({"OPT", "FOP", "BAG"})
_RIGHTS: tuple[Right, Right] = ("C", "P")
_EXPIRATION = re.compile(r"^\d{8}$")
_NEARBY_EXPIRATIONS_BEFORE = 3
_NEARBY_EXPIRATIONS_AFTER = 5

_MAX_TICKERS = 101
"""IB error 101: the connection's market-data lines are all in use."""
_PERMISSION_ERRORS = frozenset({354, 10089, 10090, 10091, 10168})
"""IB errors meaning the login lacks a market data subscription (or API access to it)."""
_COMPETING_SESSION = 10197
"""IB error 10197: another session on the same login holds the market data."""

_OPRA_HINT = (
    "Quotes and greeks for US equity and index options need an options market data "
    "subscription (OPRA) for API use, plus data for the underlying; futures options need "
    "the futures exchange's data. Without one, call set_market_data_type with data_type "
    "'delayed' for delayed data where IBKR offers it."
)
_COMPETING_HINT = (
    "Another session on the same IBKR login (TWS, the mobile app, Client Portal) holds the "
    "market data; log it out or switch it to delayed data."
)
_LINES_HINT = (
    "All of this login's market-data lines are in use; unsubscribe streams "
    "(list_subscriptions shows them) or quote fewer option legs (limit)."
)


def _data_hint(code: int) -> str | None:
    if code in _PERMISSION_ERRORS:
        return _OPRA_HINT
    if code == _COMPETING_SESSION:
        return _COMPETING_HINT
    return None


def _check_quote_args(
    underlying: ContractSpec,
    strike_min: float | None,
    strike_max: float | None,
    strikes_around_atm: int | None,
) -> int:
    """Check :meth:`OptionsService.option_quotes`' strike selection; return ``around``."""
    if (strike_min is not None or strike_max is not None) and strikes_around_atm is not None:
        raise InvalidRequestError(
            "Give either strike_min/strike_max or strikes_around_atm, not both."
        )
    for name, value in (("strike_min", strike_min), ("strike_max", strike_max)):
        if value is not None:
            _check_positive(name, value)
    if strike_min is not None and strike_max is not None and strike_min > strike_max:
        raise InvalidRequestError(
            f"strike_min ({strike_min:g}) is above strike_max ({strike_max:g})."
        )
    around = STRIKES_AROUND_ATM_DEFAULT if strikes_around_atm is None else strikes_around_atm
    if around < 1:
        raise InvalidRequestError("strikes_around_atm must be at least 1.")
    if underlying.sec_type in _NOT_AN_UNDERLYING:
        raise InvalidRequestError(
            "underlying must be the instrument the options are on (STK, IND, FUT...), "
            f"not {underlying.sec_type}."
        )
    return around


def _select_strikes(
    strikes: Sequence[float],
    *,
    strike_min: float | None,
    strike_max: float | None,
    around: int,
    price: float | None,
) -> list[float]:
    """The strikes in ``[strike_min, strike_max]`` when either is set, else the
    ``around`` strikes nearest ``price`` (ties go to the lower strike)."""
    if strike_min is not None or strike_max is not None or price is None:
        return [
            strike
            for strike in strikes
            if (strike_min is None or strike >= strike_min)
            and (strike_max is None or strike <= strike_max)
        ]
    return sorted(strikes, key=lambda strike: (abs(strike - price), strike))[:around]


def _check_positive(name: str, value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise InvalidRequestError(f"{name} must be a positive number, not {value}.")


def _normalize_expiration(expiration: str) -> str:
    text = expiration.strip().replace("-", "")
    if not _EXPIRATION.match(text):
        raise InvalidRequestError(
            f"expiration must be a date as YYYYMMDD (e.g. 20261218), not {expiration!r}; "
            "get_option_chain lists the expirations."
        )
    return text


def _nearby(expirations: Sequence[str], expiration: str) -> list[str]:
    """Listed expirations around ``expiration`` (a few before, a few after)."""
    ordered = sorted(set(expirations))
    index = bisect.bisect_left(ordered, expiration)
    start = max(0, index - _NEARBY_EXPIRATIONS_BEFORE)
    return ordered[start : index + _NEARBY_EXPIRATIONS_AFTER]


def _reference_price(ticker: Ticker) -> float | None:
    """A price for the underlying: the bid/ask midpoint, else the last trade, else the close."""
    bid = clean_float(ticker.bid)
    ask = clean_float(ticker.ask)
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        return (bid + ask) / 2
    for value in (ticker.last, ticker.close):
        price = clean_float(value)
        if price is not None and price > 0:
            return price
    return None


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, IbApiError):
        reason = f"IB error {exc.error_code}: {exc.error_message}"
        if exc.error_code in _PERMISSION_ERRORS:
            reason += " (needs an options market data subscription, e.g. OPRA)"
        elif exc.error_code == _MAX_TICKERS:
            reason += f" ({_LINES_HINT})"
        elif exc.error_code == _COMPETING_SESSION:
            reason += f" ({_COMPETING_HINT})"
        return reason
    return str(exc)


def _raise_data_error(exc: BaseException, what: str, advice: str | None = None) -> NoReturn:
    """Re-raise an IBKR failure for ``what`` with a hint on what to do.

    ``advice`` is appended to every :class:`IbApiError` and :class:`RequestTimeoutError`
    (e.g. how to avoid the failing request); other errors are re-raised unchanged.
    """
    suffix = f" {advice}" if advice else ""
    if isinstance(exc, IbApiError):
        if exc.error_code == _MAX_TICKERS:
            raise SubscriptionLimitError(
                f"IBKR refused market data for {what} (error 101). {_LINES_HINT}{suffix}"
            ) from exc
        hint = _data_hint(exc.error_code)
        if hint is not None or advice:
            advise = " ".join(part for part in (hint, advice) if part)
            raise exc.with_hint(advise, context=what) from exc
    elif isinstance(exc, RequestTimeoutError) and advice:
        raise RequestTimeoutError(f"{exc}{suffix}") from exc
    raise exc


class OptionsService(BaseService):
    """IBKR's option calculators and snapshot quotes for a slice of an option chain.

    The calculators run IBKR's option model on the gateway; the quotes are one-shot
    market data snapshots and need market data subscriptions (OPRA for US options).
    """

    # --- calculators -------------------------------------------------------------------

    async def implied_volatility(
        self, contract: ContractSpec, *, option_price: float, underlying_price: float
    ) -> ImpliedVolatilityOut:
        """Compute an option's implied volatility from its price (``calculateImpliedVolatility``).

        The contract is qualified first. IBKR's model also returns the greeks at that
        volatility.

        Args:
            contract: The option (OPT or FOP), or its con_id.
            option_price: Option price per share (unmultiplied), e.g. 5.20.
            underlying_price: Underlying price to assume.

        Raises:
            InvalidRequestError: Not an option, a non-positive price, or IBKR's model could
                not imply a volatility from these prices (e.g. below intrinsic value).
            NotFoundError, AmbiguousContractError: The contract could not be resolved.
            RequestTimeoutError: IBKR did not answer within :data:`CALCULATION_TIMEOUT`.
            IbApiError: The gateway rejected the calculation.
        """
        _check_positive("option_price", option_price)
        _check_positive("underlying_price", underlying_price)
        option = await self._qualify_option(contract)
        greeks = await self._calculate(
            _REQ_CALC_IMPLIED_VOLAT,
            self.ib.client.cancelCalculateImpliedVolatility,
            option,
            option_price,
            underlying_price,
            what=f"the implied volatility of {_describe_contract(option)}",
        )
        if greeks.implied_vol is None:
            raise InvalidRequestError(
                f"IBKR could not imply a volatility for {_describe_contract(option)} at option "
                f"price {option_price:g} and underlying price {underlying_price:g}. The option "
                "price may be below intrinsic value or above what any volatility gives."
            )
        return ImpliedVolatilityOut(
            contract=contract_to_out(option),
            option_price=option_price,
            underlying_price=underlying_price,
            implied_vol=greeks.implied_vol,
            greeks=greeks,
        )

    async def option_price(
        self, contract: ContractSpec, *, volatility: float, underlying_price: float
    ) -> OptionPriceOut:
        """Compute an option's theoretical price and greeks (``calculateOptionPrice``).

        Args:
            contract: The option (OPT or FOP), or its con_id.
            volatility: Annualized volatility as a decimal (0.25 = 25%), at most
                :data:`MAX_VOLATILITY`.
            underlying_price: Underlying price to assume.

        Raises:
            InvalidRequestError: Not an option, a non-positive input, a volatility above
                :data:`MAX_VOLATILITY` (probably given in percent), or no price computed.
            NotFoundError, AmbiguousContractError: The contract could not be resolved.
            RequestTimeoutError: IBKR did not answer within :data:`CALCULATION_TIMEOUT`.
            IbApiError: The gateway rejected the calculation.
        """
        _check_positive("volatility", volatility)
        _check_positive("underlying_price", underlying_price)
        if volatility > MAX_VOLATILITY:
            raise InvalidRequestError(
                f"volatility is a decimal (0.25 = 25%); {volatility:g} would be "
                f"{volatility * 100:g}%. Pass at most {MAX_VOLATILITY:g}."
            )
        option = await self._qualify_option(contract)
        greeks = await self._calculate(
            _REQ_CALC_OPTION_PRICE,
            self.ib.client.cancelCalculateOptionPrice,
            option,
            volatility,
            underlying_price,
            what=f"the theoretical price of {_describe_contract(option)}",
        )
        if greeks.opt_price is None:
            raise InvalidRequestError(
                f"IBKR computed no price for {_describe_contract(option)} at volatility "
                f"{volatility:g} and underlying price {underlying_price:g}."
            )
        return OptionPriceOut(
            contract=contract_to_out(option),
            volatility=volatility,
            underlying_price=underlying_price,
            option_price=greeks.opt_price,
            greeks=greeks,
        )

    async def _calculate(
        self,
        message: int,
        cancel: Callable[[int], None],
        option: Contract,
        value: float,
        underlying_price: float,
        *,
        what: str,
    ) -> OptionGreeks:
        """Run one option calculation (``message``: the IV or price calculator) on the gateway.

        The request goes out through ``ib.client`` in the official client's layout, not
        through ib_async's ``calculateImpliedVolatilityAsync``/``calculateOptionPriceAsync``:
        ib_async 2.1.0 sends a tag count before the misc-options string, which the official
        client does not, so the gateway reads the count ("0") as the options and rejects
        the request (IB error 320, "Please use 'Key=Value' format for Misc Options").
        ib_async's per-request future still carries the answer (``tickOptionComputation``)
        or the error. The calculation is cancelled at IBKR afterwards, as ib_async does.

        Args:
            message: :data:`_REQ_CALC_IMPLIED_VOLAT` or :data:`_REQ_CALC_OPTION_PRICE`.
            cancel: The matching ``ib.client`` cancel method (takes the request id).
            option: The qualified option.
            value: The option price (IV calculator) or the volatility (price calculator).
            underlying_price: Underlying price to assume.
            what: Short description for error messages.
        """
        ib = self.ib
        client = ib.client
        wrapper = ib.wrapper
        req_id = self._new_req_id(ib, what)
        answer = wrapper.startReq(req_id, option)

        async def request() -> object:
            client.send(
                message,
                _CALCULATION_VERSION,
                req_id,
                option,
                value,
                underlying_price,
                _NO_CALCULATION_OPTIONS,
            )
            return await answer

        seconds = min(CALCULATION_TIMEOUT, self.settings.request_timeout)
        try:
            result = await self._call(request, what=what, timeout=seconds, req_id=req_id)
        except IbApiError as exc:
            _raise_data_error(exc, what)
        except RequestTimeoutError as exc:
            raise RequestTimeoutError(
                f"{exc} Retry; if it keeps failing, check get_health and the market data "
                "permissions for the option and its underlying."
            ) from exc
        finally:
            best_effort(functools.partial(cancel, req_id), f"cancel {what}")
            end_request(wrapper, req_id)  # forget the request if it never finished
        greeks = greeks_from_computation(result) if isinstance(result, OptionComputation) else None
        if greeks is None:
            raise InvalidRequestError(f"IBKR's option model returned no values for {what}.")
        return greeks

    async def _qualify_option(self, spec: ContractSpec) -> Contract:
        """Qualify ``spec`` and check that it is an option (OPT or FOP)."""
        con_id_only = spec.con_id is not None and "sec_type" not in spec.model_fields_set
        if not con_id_only and spec.sec_type not in _OPTION_TYPES:
            raise InvalidRequestError(
                f"The option calculators need an option (sec_type OPT or FOP), not "
                f"{spec.sec_type}; give symbol, sec_type, expiry, strike and right, or the "
                "option's con_id."
            )
        contract = await self.qualify(spec)
        if contract.secType not in _OPTION_TYPES:
            raise InvalidRequestError(
                f"{describe_spec(spec)} is a {contract.secType}, not an option (OPT or FOP)."
            )
        return contract

    # --- chain-slice quotes ------------------------------------------------------------

    async def option_quotes(
        self,
        underlying: ContractSpec,
        expiration: str,
        *,
        right: Right | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
        strikes_around_atm: int | None = None,
        exchange: str | None = None,
        trading_class: str | None = None,
        limit: int | None = None,
    ) -> OptionQuoteList:
        """Snapshot quotes with greeks for a slice of one option expiration.

        Steps: qualify the underlying; read its chains (``reqSecDefOptParams``); pick the
        chain on ``exchange`` with ``expiration`` (and ``trading_class``); choose strikes
        in ``[strike_min, strike_max]``, or the ``strikes_around_atm`` strikes nearest the
        underlying's price (from a snapshot); qualify each option, skipping strikes not
        listed for the expiration; snapshot each option's quote and model greeks.

        Args:
            underlying: The stock, index or future the options are on.
            expiration: Expiration date, YYYYMMDD (dashes are ignored).
            right: C or P; None quotes both.
            strike_min: Lowest strike (inclusive); with ``strike_max``, selects a range.
            strike_max: Highest strike (inclusive).
            strikes_around_atm: How many strikes nearest the underlying price to take
                (default :data:`STRIKES_AROUND_ATM_DEFAULT`); not with a range.
            exchange: The chain's exchange. None means SMART when listed there, else the
                only exchange listed (futures options list on e.g. CME).
            trading_class: The chain's trading class, e.g. SPXW. None prefers the class
                named like the underlying when several list the expiration.
            limit: Legs (strike and right pairs) to quote; default
                :data:`QUOTES_LIMIT_DEFAULT`, capped at :data:`QUOTES_LIMIT_MAX`. With a
                range the lowest strikes are kept, around the money the nearest.

        Raises:
            InvalidRequestError: Bad arguments, an option as underlying, or several
                trading classes list the expiration and none was chosen.
            NotFoundError: Unknown underlying, no options, no chain on ``exchange``, the
                expiration or strikes are not listed, or the underlying has no price.
            IbApiError: Market data refused (the message explains the subscription
                needed), when no leg could be quoted.
            SubscriptionLimitError: IBKR's market-data line limit was hit (error 101).
        """
        expiry = _normalize_expiration(expiration)
        around = _check_quote_args(underlying, strike_min, strike_max, strikes_around_atm)
        use_range = strike_min is not None or strike_max is not None
        cap = clamp_limit(limit, default=QUOTES_LIMIT_DEFAULT, maximum=QUOTES_LIMIT_MAX)

        contract = await self.qualify(underlying)
        if contract.secType in _NOT_AN_UNDERLYING:
            # A con_id-only spec keeps the STK default, so the check above cannot see it.
            raise InvalidRequestError(
                f"{describe_spec(underlying)} is an option or combo ({contract.secType}); "
                "underlying must be the instrument the options are on (STK, IND, FUT...)."
            )
        name = f"{contract.symbol} {contract.secType} (con_id {contract.conId})"
        chain = await self._chain(contract, name, expiry, exchange, trading_class)
        strikes = sorted(set(chain.strikes))

        underlying_quote: QuoteOut | None = None
        price: float | None = None
        if not use_range:
            underlying_quote, price = await self._underlying_price(contract, name)
        chosen = _select_strikes(
            strikes, strike_min=strike_min, strike_max=strike_max, around=around, price=price
        )
        if not chosen:
            low = f"{strike_min:g}" if strike_min is not None else "the lowest"
            high = f"{strike_max:g}" if strike_max is not None else "the highest"
            span = f"{strikes[0]:g} to {strikes[-1]:g}" if strikes else "nowhere"
            raise NotFoundError(
                f"No {name} strikes from {low} to {high} in class {chain.tradingClass}; "
                f"its strikes run from {span}."
            )
        rights: Sequence[Right] = (right,) if right else _RIGHTS
        legs = [(strike, leg_right) for strike in chosen for leg_right in rights]
        kept, truncated = truncate(legs, cap)
        kept.sort()

        options, skipped = await self._qualify_legs(contract, chain, expiry, kept)
        if not options:
            listed = ", ".join(f"{strike:g}" for strike, _right in kept)
            raise NotFoundError(
                f"None of the selected {name} strikes ({listed}) is listed for expiration "
                f"{expiry} in class {chain.tradingClass}; strikes differ per expiration. "
                "Pick others with strike_min/strike_max, or check get_option_chain."
            )
        quotes = await self._quote_legs(options, skipped)
        return OptionQuoteList(
            underlying=contract_to_out(contract),
            underlying_quote=underlying_quote,
            underlying_price=price,
            expiration=expiry,
            exchange=chain.exchange,
            trading_class=chain.tradingClass,
            multiplier=chain.multiplier or None,
            market_data_type=MARKET_DATA_TYPE_NAMES.get(self.connection.market_data_type),
            total=len(legs),
            legs=quotes,
            skipped=sorted(skipped, key=lambda leg: (leg.strike, leg.right)),
            truncated=truncated,
        )

    async def _chain(
        self,
        contract: Contract,
        name: str,
        expiry: str,
        exchange: str | None,
        trading_class: str | None,
    ) -> OptionChain:
        """Pick the one chain (exchange and trading class) that lists ``expiry``.

        Returns an ``OptionChain`` whose strikes are the union of the chosen rows.
        """
        ib = self.ib
        rows = await self._call(
            ib.reqSecDefOptParamsAsync(
                contract.symbol,
                fop_exchange(contract),
                option_underlying_sec_type(contract.secType),
                contract.conId,
            ),
            what=f"the option chain for {name}",
        )
        rows = list(rows or [])
        if not rows:
            raise NotFoundError(
                f"IBKR lists no options on {name}. Check that the instrument has listed "
                "options; for futures options the underlying is the future (sec_type FUT "
                "with its exchange and contract month)."
            )
        exchanges = sorted({row.exchange for row in rows})
        wanted = exchange.strip().upper() if exchange and exchange.strip() else None
        if wanted is None:
            if "SMART" in exchanges:
                wanted = "SMART"
            elif len(exchanges) == 1:
                wanted = exchanges[0]
            else:
                raise InvalidRequestError(
                    f"{name} options are listed on {', '.join(exchanges)}; pass exchange."
                )
        rows = [row for row in rows if row.exchange == wanted]
        if not rows:
            raise NotFoundError(
                f"No option chain for {name} on exchange {wanted}; chains are listed on "
                f"{', '.join(exchanges)}."
            )
        if trading_class and trading_class.strip():
            wanted_class = trading_class.strip().upper()
            classes = sorted({row.tradingClass for row in rows})
            rows = [row for row in rows if row.tradingClass == wanted_class]
            if not rows:
                raise NotFoundError(
                    f"No {name} options of trading class {wanted_class} on {wanted}; classes "
                    f"listed: {', '.join(classes)}."
                )
        listing = [row for row in rows if expiry in row.expirations]
        if not listing:
            nearby = _nearby([exp for row in rows for exp in row.expirations], expiry)
            raise NotFoundError(
                f"No {name} options expire on {expiry} on {wanted}"
                + (f" in class {rows[0].tradingClass}" if trading_class else "")
                + f". Listed expirations nearby: {', '.join(nearby) or 'none'}; "
                "get_option_chain lists them all."
            )
        classes = sorted({row.tradingClass for row in listing})
        if len(classes) > 1:
            preferred = [row for row in listing if row.tradingClass == contract.symbol]
            if not preferred:
                raise InvalidRequestError(
                    f"Several {name} option classes expire on {expiry}: {', '.join(classes)}. "
                    "Pass trading_class to choose one."
                )
            listing = preferred
        first = listing[0]
        strikes = sorted({strike for row in listing for strike in row.strikes})
        return OptionChain(
            first.exchange,
            first.underlyingConId,
            first.tradingClass,
            first.multiplier,
            [expiry],
            strikes,
        )

    async def _underlying_price(self, contract: Contract, name: str) -> tuple[QuoteOut, float]:
        """Snapshot the underlying and return its quote and a reference price."""
        what = f"a quote for the underlying {name}"
        try:
            ticker = await self._snapshot(contract, what)
        except (IbApiError, RequestTimeoutError) as exc:
            _raise_data_error(
                exc,
                what,
                "The underlying's price only picks the at-the-money strikes: pass "
                "strike_min/strike_max to skip it.",
            )
        price = _reference_price(ticker)
        if price is None:
            raise NotFoundError(
                f"No price for the underlying {name} (no bid/ask, last or close with the "
                "current market data type), so the at-the-money strikes are unknown. Pass "
                "strike_min and strike_max, or switch set_market_data_type (e.g. to "
                "'delayed' or 'frozen')."
            )
        return quote_from_ticker(ticker, contract_to_out(contract)), price

    async def _qualify_legs(
        self,
        underlying: Contract,
        chain: OptionChain,
        expiry: str,
        legs: Sequence[tuple[float, Right]],
    ) -> tuple[list[_Leg], list[SkippedOptionLeg]]:
        """Qualify each leg; strikes not listed for ``expiry`` are skipped, not fatal."""
        sec_type: SecType = (
            "FOP" if option_underlying_sec_type(underlying.secType) == "FUT" else "OPT"
        )
        specs = [
            ContractSpec(
                symbol=underlying.symbol,
                sec_type=sec_type,
                exchange=chain.exchange,
                currency=underlying.currency or "USD",
                last_trade_date_or_contract_month=expiry,
                strike=strike,
                right=leg_right,
                trading_class=chain.tradingClass or None,
                multiplier=chain.multiplier or None,
            )
            for strike, leg_right in legs
        ]
        results = await asyncio.gather(
            *(self.qualify(spec) for spec in specs), return_exceptions=True
        )
        options: list[_Leg] = []
        skipped: list[SkippedOptionLeg] = []
        for (strike, leg_right), result in zip(legs, results, strict=True):
            if isinstance(result, NotFoundError):
                skipped.append(
                    SkippedOptionLeg(
                        strike=strike, right=leg_right, reason="not listed for this expiration"
                    )
                )
            elif isinstance(result, AmbiguousContractError):
                con_ids = ", ".join(
                    str(candidate.con_id)
                    for candidate in result.candidates
                    if isinstance(candidate, ContractOut) and candidate.con_id
                )
                skipped.append(
                    SkippedOptionLeg(
                        strike=strike,
                        right=leg_right,
                        reason=(
                            f"ambiguous: {len(result.candidates)} contracts match"
                            + (f" (con_id {con_ids})" if con_ids else "")
                            + "; quote one by con_id with get_quotes"
                        ),
                    )
                )
            elif isinstance(result, BaseException):
                raise result
            else:
                options.append((strike, leg_right, result))
        return options, skipped

    async def _quote_legs(
        self, legs: Sequence[_Leg], skipped: list[SkippedOptionLeg]
    ) -> list[QuoteOut]:
        """Snapshot every option; legs that fail are added to ``skipped``.

        When no leg could be quoted, the first failure is raised with a hint.
        """
        results = await asyncio.gather(
            *(
                self._snapshot(option, f"a quote for {_describe_contract(option)}")
                for _strike, _right, option in legs
            ),
            return_exceptions=True,
        )
        quotes: list[QuoteOut] = []
        failures: list[BaseException] = []
        for (strike, leg_right, option), result in zip(legs, results, strict=True):
            if isinstance(result, IbApiError | RequestTimeoutError):
                failures.append(result)
                skipped.append(
                    SkippedOptionLeg(
                        strike=strike,
                        right=leg_right,
                        con_id=option.conId or None,
                        reason=_failure_reason(result),
                    )
                )
            elif isinstance(result, BaseException):
                raise result
            else:
                quotes.append(quote_from_ticker(result, contract_to_out(option)))
        if not quotes and failures:
            _raise_data_error(failures[0], f"all {len(legs)} option legs")
        return quotes

    async def _snapshot(self, contract: Contract, what: str) -> Ticker:
        """One market data snapshot (``reqTickers``) under the connection's market data type."""
        ib = self.ib
        tickers = await self._call(functools.partial(ib.reqTickersAsync, contract), what=what)
        if not tickers:
            raise RequestTimeoutError(f"IBKR returned no snapshot for {what}.")
        return tickers[0]


def _describe_contract(contract: Contract) -> str:
    """``AAPL OPT 20261218 200 C (con_id 700001)``, for messages."""
    if contract.localSymbol:
        parts = [contract.localSymbol]
    else:
        parts = [
            contract.symbol,
            contract.secType,
            contract.lastTradeDateOrContractMonth,
            f"{contract.strike:g}" if contract.strike else "",
            contract.right,
        ]
    if contract.conId:
        parts.append(f"(con_id {contract.conId})")
    return " ".join(part for part in parts if part)
