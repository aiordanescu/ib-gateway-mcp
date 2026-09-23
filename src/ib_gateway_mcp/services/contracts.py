"""Contract search, details, qualification, option chains and market rules."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Sequence
from datetime import date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from ib_async import Contract, ContractDetails

from ib_gateway_mcp._util import (
    clamp_limit,
    clean_float,
    clean_int,
    clean_str,
    contract_to_out,
    option_underlying_sec_type,
    truncate,
)
from ib_gateway_mcp._util import fop_exchange as default_fop_exchange
from ib_gateway_mcp.errors import (
    IbApiError,
    InvalidRequestError,
    NotFoundError,
    RequestTimeoutError,
)
from ib_gateway_mcp.models.common import ContractOut, ContractSpec
from ib_gateway_mcp.models.contracts import (
    BondDetails,
    ContractDetailsList,
    ContractDetailsOut,
    DepthExchange,
    DepthExchangeList,
    MarketRule,
    MarketRuleList,
    OptionChainList,
    OptionChainOut,
    PriceIncrementOut,
    SmartComponentList,
    SmartComponentOut,
    SymbolMatch,
    SymbolSearchResult,
    TradingSessionOut,
)
from ib_gateway_mcp.services._ibtime import zone_or_none
from ib_gateway_mcp.services._resolve import describe_spec, details_request, not_found_message
from ib_gateway_mcp.services.base import BaseService, market_rule_key

if TYPE_CHECKING:
    from ib_gateway_mcp.gateway import Gateway

__all__ = [
    "DETAILS_LIMIT_DEFAULT",
    "DETAILS_LIMIT_MAX",
    "MARKET_RULES_MAX",
    "SEARCH_INTERVAL",
    "SEARCH_LIMIT_DEFAULT",
    "SEARCH_LIMIT_MAX",
    "ContractsService",
]

logger = logging.getLogger(__name__)

SEARCH_LIMIT_DEFAULT = 16
"""Matches ``search_symbols`` returns by default (IBKR sends about 16 at most)."""
SEARCH_LIMIT_MAX = 50
SEARCH_INTERVAL = 1.0
"""Seconds between two symbol searches: IBKR paces ``reqMatchingSymbols`` to about 1/s."""
DETAILS_LIMIT_DEFAULT = 20
"""Contracts ``contract_details`` returns by default."""
DETAILS_LIMIT_MAX = 200
MARKET_RULES_MAX = 20
"""Market rule ids one ``market_rules`` call may ask for."""

_NO_SECURITY_DEFINITION = 200
_UNSET_SIZE = 1e30
"""IBKR's unset decimal (2**127 - 1) arrives as a huge float; sizes above this are unset."""
_HOURS_FORMAT = "%Y%m%d:%H%M"
_NOT_AN_UNDERLYING = frozenset({"OPT", "FOP", "BAG"})


class ContractsService(BaseService):
    """Contract search, details, qualification, option chains and market rules.

    None of these requests needs a market data subscription.
    """

    def __init__(self, gateway: Gateway) -> None:
        super().__init__(gateway)
        self._search_lock = asyncio.Lock()
        self._last_search: float | None = None
        self._depth_cache: tuple[datetime | None, DepthExchangeList] | None = None
        self._depth_lock = asyncio.Lock()

    # --- search ------------------------------------------------------------------------

    async def search_symbols(self, pattern: str, *, limit: int | None = None) -> SymbolSearchResult:
        """Find instruments whose symbol or name matches ``pattern`` (``reqMatchingSymbols``).

        IBKR returns a short list (about 16) and paces the request to about one per
        second, so calls are spaced by :data:`SEARCH_INTERVAL`.

        Raises:
            InvalidRequestError: ``pattern`` is blank.
            NotFoundError: Nothing matches.
            RequestTimeoutError: No answer within ib_async's fixed 4 seconds.
        """
        text = pattern.strip()
        if not text:
            raise InvalidRequestError("pattern is empty; give a symbol prefix or part of a name.")
        count = clamp_limit(limit, default=SEARCH_LIMIT_DEFAULT, maximum=SEARCH_LIMIT_MAX)
        ib = self.ib
        what = f"symbols matching {text!r}"
        async with self._search_lock:
            if self._last_search is not None:
                wait = self._last_search + SEARCH_INTERVAL - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            try:
                rows = await self._call(ib.reqMatchingSymbolsAsync(text), what=what)
            finally:
                self._last_search = time.monotonic()
        if rows is None:  # ib_async gives up after 4 seconds and returns None
            raise self._no_answer(what, seconds=4)
        matches = [_symbol_match(row.contract, row.derivativeSecTypes) for row in rows]
        found = [match for match in matches if match is not None]
        if not found:
            raise NotFoundError(
                f"No instrument matches {text!r}. Try the ticker's first letters or one word "
                "of the company name; IBKR's search does not cover every derivative "
                "(get_contract_details and get_option_chain do)."
            )
        kept, truncated = truncate(found, count)
        return SymbolSearchResult(pattern=text, total=len(found), matches=kept, truncated=truncated)

    # --- details and qualification -----------------------------------------------------

    async def contract_details(
        self, spec: ContractSpec, *, limit: int | None = None
    ) -> ContractDetailsList:
        """Return the details of every contract ``spec`` matches (``reqContractDetails``).

        ``spec`` may be partial (e.g. a symbol and ``sec_type="FUT"`` lists every
        expiry). Rows of the same contract id are merged (the row on ``spec.exchange``
        wins) and derivatives are sorted by expiry, strike and right before ``limit``
        applies.

        Raises:
            InvalidRequestError: ``spec`` is a combo (BAG).
            NotFoundError: IBKR knows no matching instrument (error 200 or no rows).
        """
        if spec.sec_type == "BAG":
            raise InvalidRequestError(
                "Combos (BAG) have no contract details; look up each leg by its con_id."
            )
        count = clamp_limit(limit, default=DETAILS_LIMIT_DEFAULT, maximum=DETAILS_LIMIT_MAX)
        request = details_request(spec)
        ib = self.ib
        try:
            rows = await self._call(
                ib.reqContractDetailsAsync(request),
                what=f"contract details for {describe_spec(spec)}",
            )
        except IbApiError as exc:
            if exc.error_code == _NO_SECURITY_DEFINITION:
                raise NotFoundError(not_found_message(spec)) from exc
            raise
        unique = _unique_details(rows or [], request.secType, spec.exchange)
        if not unique:
            raise NotFoundError(not_found_message(spec))
        kept, truncated = truncate(unique, count)
        return ContractDetailsList(
            total=len(unique),
            contracts=[_details_out(contract, row) for contract, row in kept],
            truncated=truncated,
        )

    async def qualify_contract(self, spec: ContractSpec) -> ContractOut:
        """Resolve ``spec`` to exactly one contract, named with its long name.

        Uses :meth:`BaseService.qualify_details` (one ``reqContractDetails``), so the
        contract is the one :meth:`BaseService.qualify` would return.

        Raises:
            NotFoundError, AmbiguousContractError: No match, or several (with candidates).
            InvalidRequestError: ``spec`` is a combo (BAG).
        """
        details = await self.qualify_details(spec)
        if details.contract is None:  # qualify_details never returns such a row
            raise NotFoundError(not_found_message(spec))
        return contract_to_out(details.contract, description=details.longName)

    # --- option chains -----------------------------------------------------------------

    async def option_chain(
        self,
        underlying: ContractSpec,
        *,
        exchange: str | None = None,
        fut_fop_exchange: str | None = None,
    ) -> OptionChainList:
        """List option expirations and strikes for ``underlying`` (``reqSecDefOptParams``).

        The underlying is qualified first; a continuous future (CONTFUT) is asked for as
        the future it stands for. Chains that are identical on several exchanges are
        merged into one entry listing those exchanges.

        Args:
            underlying: The stock, index or future the options are on.
            exchange: Keep only chains listed on this exchange (e.g. SMART, CBOE).
            fut_fop_exchange: Exchange of futures options; defaults to the future's own
                exchange for a FUT underlying and to all exchanges otherwise.

        Raises:
            InvalidRequestError: ``underlying`` is itself an option or combo (also when
                it is given by such a con_id).
            NotFoundError: The underlying is unknown or lists no options (on ``exchange``).
        """
        if underlying.sec_type in _NOT_AN_UNDERLYING:
            raise InvalidRequestError(
                f"underlying must be the instrument the options are on (STK, IND, FUT...), "
                f"not {underlying.sec_type}."
            )
        contract = await self.qualify(underlying)
        name = f"{contract.symbol} {contract.secType} (con_id {contract.conId})"
        if contract.secType in _NOT_AN_UNDERLYING:  # e.g. an option given by con_id alone
            raise InvalidRequestError(
                f"{name} is itself a derivative; give the instrument the options are on "
                "(its under_con_id in get_contract_details)."
            )
        # A continuous future stands for its front month, which is what IBKR lists
        # options on.
        sec_type = option_underlying_sec_type(contract.secType)
        if fut_fop_exchange is None:
            fop_exchange = default_fop_exchange(contract)
        else:
            fop_exchange = fut_fop_exchange.strip().upper()
        ib = self.ib
        rows = await self._call(
            ib.reqSecDefOptParamsAsync(contract.symbol, fop_exchange, sec_type, contract.conId),
            what=f"the option chain for {name}",
        )
        rows = list(rows or [])
        if not rows:
            raise NotFoundError(
                f"IBKR lists no options on {name}"
                + (f" at fut_fop_exchange {fop_exchange}" if fop_exchange else "")
                + ". Check that the instrument has listed options; for futures options, "
                "the underlying is the future and fut_fop_exchange its exchange (e.g. CME)."
            )
        wanted = clean_str(exchange.upper()) if exchange else None
        if wanted is not None:
            available = sorted({row.exchange for row in rows})
            rows = [row for row in rows if row.exchange == wanted]
            if not rows:
                raise NotFoundError(
                    f"No option chain for {name} on exchange {wanted}; chains are listed on "
                    f"{', '.join(available)}."
                )
        return OptionChainList(underlying=contract_to_out(contract), chains=_merge_chains(rows))

    # --- market rules, SMART components, depth exchanges --------------------------------

    async def market_rules(self, market_rule_ids: Sequence[int]) -> MarketRuleList:
        """Return the price-increment ladders of market rules (``reqMarketRule``).

        Rule ids come from :meth:`contract_details` (``market_rule_ids``). Duplicates
        are asked once. ib_async waits only 1 second per rule and then gives up; ids
        without an answer are listed in ``missing_ids``.

        Raises:
            InvalidRequestError: No ids, a negative id, or more than
                :data:`MARKET_RULES_MAX` distinct ids.
            NotFoundError: No rule was answered.
        """
        ids = list(dict.fromkeys(market_rule_ids))
        if not ids:
            raise InvalidRequestError("Give at least one market rule id.")
        if len(ids) > MARKET_RULES_MAX:
            raise InvalidRequestError(
                f"At most {MARKET_RULES_MAX} market rule ids per call; got {len(ids)}."
            )
        if any(rule_id < 0 for rule_id in ids):
            raise InvalidRequestError("Market rule ids are non-negative integers.")
        ib = self.ib
        # ib_async keys each rule request by "marketRule-<id>", so the same id must
        # never be in flight twice (a second request orphans the first one's future).
        results = await asyncio.gather(
            *(
                self._call(
                    functools.partial(ib.reqMarketRuleAsync, rule_id),
                    what=f"market rule {rule_id}",
                    exclusive=market_rule_key(rule_id),
                )
                for rule_id in ids
            ),
            return_exceptions=True,
        )
        rules: list[MarketRule] = []
        missing: list[int] = []
        for rule_id, result in zip(ids, results, strict=True):
            if isinstance(result, BaseException):
                raise result
            if result is None:
                missing.append(rule_id)
                continue
            rules.append(MarketRule(market_rule_id=rule_id, increments=_increments(result)))
        if not rules:
            listed = ", ".join(str(rule_id) for rule_id in missing)
            raise NotFoundError(
                f"IBKR did not answer for market rule id(s) {listed} within ib_async's fixed "
                "1-second wait. Unknown ids get no answer: take them from get_contract_details "
                "(market_rule_ids). If the ids are right, the gateway may be slow; retry once."
            )
        return MarketRuleList(rules=rules, missing_ids=missing)

    async def smart_components(self, bbo_exchange: str) -> SmartComponentList:
        """Expand a SMART BBO exchange code into its exchanges (``reqSmartComponents``).

        The code comes from a market data response (``QuoteOut.bbo_exchange``). IBKR
        may return nothing outside trading hours.

        Raises:
            InvalidRequestError: ``bbo_exchange`` is blank.
            NotFoundError: IBKR returned no components.
        """
        code = bbo_exchange.strip()
        if not code:
            raise InvalidRequestError("bbo_exchange is empty; take it from get_quotes.")
        ib = self.ib
        rows = await self._call(
            ib.reqSmartComponentsAsync(code), what=f"SMART components of {code}"
        )
        components = [
            SmartComponentOut(
                bit_number=int(row.bitNumber),
                exchange=row.exchange,
                exchange_letter=row.exchangeLetter,
            )
            for row in rows or []
        ]
        if not components:
            raise NotFoundError(
                f"IBKR returned no exchanges for BBO exchange code {code!r}. The code must "
                "come from a quote (get_quotes bbo_exchange); IBKR may also return nothing "
                "outside trading hours."
            )
        return SmartComponentList(bbo_exchange=code, components=components)

    async def depth_exchanges(self) -> DepthExchangeList:
        """List the exchanges that offer market depth (``reqMktDepthExchanges``).

        The list is static, so it is fetched once per gateway session and cached.

        Raises:
            NotFoundError: IBKR returned an empty list.
        """
        ib = self.ib
        # Concurrent first calls wait here for one request instead of each sending one.
        async with self._depth_lock:
            session = self.connection.connected_since
            cached = self._depth_cache
            if cached is not None and cached[0] == session:
                return cached[1]
            rows = await self._call(
                ib.reqMktDepthExchangesAsync,
                what="the market depth exchanges",
                exclusive="mktDepthExchanges",
            )
            exchanges = [
                DepthExchange(
                    exchange=row.exchange,
                    sec_type=row.secType,
                    listing_exchange=clean_str(row.listingExch),
                    service_data_type=clean_str(row.serviceDataType),
                    agg_group=clean_int(row.aggGroup),
                )
                for row in rows or []
            ]
            if not exchanges:
                raise NotFoundError("IBKR reported no exchanges that offer market depth.")
            result = DepthExchangeList(exchanges=exchanges)
            self._depth_cache = (session, result)
            return result

    # --- helpers -----------------------------------------------------------------------

    def _no_answer(self, what: str, *, seconds: int) -> RequestTimeoutError:
        """The error for an ib_async request that gave up after its fixed internal wait."""
        message = f"No answer for {what} within ib_async's fixed {seconds}-second wait."
        health = self.connection.health()
        if health.hint:
            message += f" Gateway state: {health.state.value}. {health.hint}"
        else:
            message += " IBKR may be pacing requests; wait a moment and retry."
        return RequestTimeoutError(message)


# --- conversions ------------------------------------------------------------------------------


def _symbol_match(contract: Contract | None, derivatives: Sequence[str]) -> SymbolMatch | None:
    if contract is None or not contract.conId:
        return None
    return SymbolMatch(
        contract=contract_to_out(contract),
        derivative_sec_types=[kind for kind in derivatives if kind],
        issuer_id=clean_str(contract.issuerId),
    )


def _unique_details(
    rows: Sequence[ContractDetails], sec_type: str, exchange: str
) -> list[tuple[Contract, ContractDetails]]:
    """One ``(contract, row)`` per contract id, in a stable order.

    Rows of another security type are dropped when the request named one (IBKR adds
    event contracts to futures-option answers). Of several rows for one contract id,
    the one on ``exchange`` wins. Derivatives are sorted by expiry, strike and right.
    """
    found = [(row.contract, row) for row in rows if row.contract is not None and row.contract.conId]
    if sec_type:
        found = [pair for pair in found if pair[0].secType == sec_type] or found
    by_con_id: dict[int, tuple[Contract, ContractDetails]] = {}
    for contract, row in found:
        current = by_con_id.get(contract.conId)
        if current is None or (contract.exchange == exchange and current[0].exchange != exchange):
            by_con_id[contract.conId] = (contract, row)
    return sorted(by_con_id.values(), key=lambda pair: _derivative_order(pair[0]))


def _derivative_order(contract: Contract) -> tuple[str, float, str]:
    return (
        contract.lastTradeDateOrContractMonth or "",
        clean_float(contract.strike) or 0.0,
        contract.right or "",
    )


def _split(text: str | None) -> list[str]:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def _int_list(text: str | None) -> list[int]:
    numbers: list[int] = []
    for part in _split(text):
        try:
            numbers.append(int(part))
        except ValueError:
            logger.debug("Ignoring non-numeric market rule id %r", part)
    return numbers


def _positive_int(value: Any) -> int | None:
    number = clean_int(value)
    return number if number is not None and number > 0 else None


def _positive_float(value: Any) -> float | None:
    number = clean_float(value)
    return number if number is not None and number > 0 else None


def _size(value: Any) -> float | None:
    number = clean_float(value)
    if number is None or number < 0 or number > _UNSET_SIZE:
        return None
    return number


def _stamp(text: str, day: str, zone: tzinfo) -> datetime:
    """``YYYYMMDD:HHMM``, or ``HHMM`` on ``day``, as an aware datetime in ``zone``."""
    full = text if ":" in text else f"{day}:{text}"
    return datetime.strptime(full, _HOURS_FORMAT).replace(tzinfo=zone)


def _parse_hours(hours: str, zone: tzinfo) -> tuple[list[TradingSessionOut], list[date]]:
    """Parse IBKR's trading-hours text into sessions and closed days.

    Accepts the current format (``20260105:0930-20260105:1600;20260106:CLOSED``) and
    the older one (``20260105:0700-1830,1830-2330``), where an end before its start
    falls on the next day.

    Raises:
        ValueError: The text is in neither format.
    """
    sessions: list[TradingSessionOut] = []
    closed: list[date] = []
    for raw in hours.split(";"):
        segment = raw.strip()
        if not segment:
            continue
        day, _, rest = segment.partition(":")
        if rest == "CLOSED":
            closed.append(datetime.strptime(day, "%Y%m%d").date())
            continue
        for part in segment.split(","):
            start_text, end_text = part.split("-")
            start = _stamp(start_text.strip(), day, zone)
            end = _stamp(end_text.strip(), day, zone)
            if end <= start and ":" not in end_text:
                end += timedelta(days=1)
            if end < start:
                raise ValueError(f"session ends before it starts: {part!r}")
            sessions.append(TradingSessionOut(start=start, end=end))
    return sessions, closed


def _sessions(
    hours: str | None, zone: tzinfo | None
) -> tuple[list[TradingSessionOut] | None, list[date], str | None]:
    """``(sessions, closed days, raw text)``; the raw text only when parsing failed."""
    text = clean_str(hours)
    if text is None:
        return [], [], None
    if zone is None:
        return None, [], text
    try:
        sessions, closed = _parse_hours(text, zone)
    except ValueError:
        logger.debug("Could not parse trading hours %r", text)
        return None, [], text
    return sessions, closed, None


def _bond(contract: Contract, row: ContractDetails) -> BondDetails | None:
    if contract.secType != "BOND" and not (row.cusip or row.maturity or row.bondType):
        return None
    # Without bond reference data on the login IBKR withholds the terms: no maturity and an
    # empty coupon, which ib_async decodes as 0. That is an unknown coupon, not a
    # zero-coupon bond. (A perpetual has no maturity either, but a nonzero coupon, kept.)
    terms_withheld = not clean_str(row.maturity)
    return BondDetails(
        cusip=clean_str(row.cusip),
        ratings=clean_str(row.ratings),
        desc_append=clean_str(row.descAppend),
        bond_type=clean_str(row.bondType),
        coupon_type=clean_str(row.couponType),
        coupon=None if terms_withheld and not row.coupon else clean_float(row.coupon),
        maturity=clean_str(row.maturity),
        issue_date=clean_str(row.issueDate),
        callable=bool(row.callable),
        putable=bool(row.putable),
        convertible=bool(row.convertible),
        next_option_date=clean_str(row.nextOptionDate),
        next_option_type=clean_str(row.nextOptionType),
        next_option_partial=bool(row.nextOptionPartial),
        notes=clean_str(row.notes),
    )


def _details_out(contract: Contract, row: ContractDetails) -> ContractDetailsOut:
    zone = zone_or_none(row.timeZoneId, what="contract details")
    trading, closed, trading_raw = _sessions(row.tradingHours, zone)
    liquid, _liquid_closed, liquid_raw = _sessions(row.liquidHours, zone)
    return ContractDetailsOut(
        contract=contract_to_out(contract, description=row.longName),
        market_name=clean_str(row.marketName),
        industry=clean_str(row.industry),
        category=clean_str(row.category),
        subcategory=clean_str(row.subcategory),
        stock_type=clean_str(row.stockType),
        contract_month=clean_str(row.contractMonth),
        real_expiration_date=clean_str(row.realExpirationDate),
        last_trade_time=clean_str(row.lastTradeTime),
        under_con_id=_positive_int(row.underConId),
        under_symbol=clean_str(row.underSymbol),
        under_sec_type=clean_str(row.underSecType),
        time_zone_id=clean_str(row.timeZoneId),
        trading_sessions=trading,
        liquid_sessions=liquid,
        closed_dates=closed,
        trading_hours=trading_raw,
        liquid_hours=liquid_raw,
        min_tick=_positive_float(row.minTick),
        price_magnifier=_positive_int(row.priceMagnifier),
        min_size=_size(row.minSize),
        size_increment=_size(row.sizeIncrement),
        suggested_size_increment=_size(row.suggestedSizeIncrement),
        valid_exchanges=_split(row.validExchanges),
        market_rule_ids=_int_list(row.marketRuleIds),
        order_types=_split(row.orderTypes),
        sec_ids={tag_value.tag: tag_value.value for tag_value in row.secIdList or []},
        ev_rule=clean_str(row.evRule),
        ev_multiplier=_positive_float(row.evMultiplier),
        agg_group=clean_int(row.aggGroup),
        bond=_bond(contract, row),
    )


def _merge_chains(rows: Sequence[Any]) -> list[OptionChainOut]:
    """Merge ``OptionChain`` rows that differ only by exchange, sorted by trading class."""
    groups: dict[tuple[str, str, tuple[str, ...], tuple[float, ...]], list[str]] = {}
    for row in rows:
        expirations = tuple(sorted({str(expiry) for expiry in row.expirations if expiry}))
        strikes = tuple(
            sorted({strike for strike in map(clean_float, row.strikes) if strike is not None})
        )
        key = (row.tradingClass, row.multiplier, expirations, strikes)
        exchanges = groups.setdefault(key, [])
        if row.exchange not in exchanges:
            exchanges.append(row.exchange)
    chains = [
        OptionChainOut(
            exchanges=sorted(exchanges),
            trading_class=trading_class,
            multiplier=clean_str(multiplier),
            expirations=list(expirations),
            strikes=list(strikes),
        )
        for (trading_class, multiplier, expirations, strikes), exchanges in groups.items()
    ]
    return sorted(chains, key=lambda chain: (chain.trading_class, chain.exchanges))


def _increments(rows: Sequence[Any]) -> list[PriceIncrementOut]:
    increments: list[PriceIncrementOut] = []
    for row in rows:
        low_edge, increment = clean_float(row.lowEdge), clean_float(row.increment)
        if low_edge is not None and increment is not None:
            increments.append(PriceIncrementOut(low_edge=low_edge, increment=increment))
    return increments
