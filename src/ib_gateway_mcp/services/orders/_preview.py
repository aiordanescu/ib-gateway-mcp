"""Preview plumbing of the orders service: contracts, checks, what-if and tokens."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import functools
import logging
from collections.abc import Iterator, Sequence

from ib_async import Contract, ContractDetails, Order, OrderState
from ib_async.objects import PriceIncrement

from ib_gateway_mcp.errors import IbApiError, IbGatewayMcpError, InvalidRequestError, NotFoundError
from ib_gateway_mcp.models.common import ContractSpec
from ib_gateway_mcp.models.orders import OrderLine, OrderPreview, PreviewKind, WhatIfOut
from ib_gateway_mcp.safety.audit import AuditEvent
from ib_gateway_mcp.safety.policy import LegSummary, OrderSummary, estimate_notional
from ib_gateway_mcp.services.base import REQUEST_ENDING_WARNINGS, BaseService, market_rule_key
from ib_gateway_mcp.services.orders._constants import (
    _PAYLOAD_VERSION,
    _ROLE_LABELS,
    REFERENCE_PRICE_TIMEOUT,
)
from ib_gateway_mcp.services.orders._describe import (
    _amount,
    _contract_label,
    _is_fresh,
    _multiplier,
    _needs_reference,
    _num,
    _price,
    _quantity,
    _Reference,
    _symbol,
    _tick_prices,
    _tick_problem,
    _ticker_price,
    _what_if_lines,
    _what_if_out,
)
from ib_gateway_mcp.services.orders._payloads import (
    _Body,
    _contract_payload,
    _ModifyData,
    _order_payload,
    _Payload,
)
from ib_gateway_mcp.services.orders._planning import _Planned
from ib_gateway_mcp.services.orders._trades import _is_paper_action

logger = logging.getLogger(__name__)


class _PreviewMixin(BaseService):
    """Preview plumbing of :class:`~ib_gateway_mcp.services.orders.OrdersService`:
    contract lookups, price-increment and limit checks, what-if, and the token."""

    _market_rules: dict[int, tuple[PriceIncrement, ...]]

    def _start_preview(self) -> None:
        """The trading gate, then a slot of the preview rate limit (previews reach IBKR)."""
        self.connection.require_trading()
        self.safety.preview_limiter.acquire()

    @contextlib.contextmanager
    def _refusals(
        self, stage: str, kind: str | None, account: str | None, *, token: str | None = None
    ) -> Iterator[None]:
        """Audit any library error raised in the block as a rejection, then re-raise it."""
        try:
            yield
        except IbGatewayMcpError as exc:
            self.safety.audit.record(
                AuditEvent.REJECTED,
                stage=stage,
                kind=kind,
                account=account,
                token=token,
                code=exc.code,
                reason=str(exc),
            )
            raise

    async def _qualified(self, spec: ContractSpec) -> tuple[Contract, ContractDetails]:
        """The qualified contract of ``spec`` and its details (one ``reqContractDetails``)."""
        details = await self.qualify_details(spec)
        contract = details.contract
        if contract is None:  # pragma: no cover - qualify_details only returns rows with one
            raise NotFoundError(f"IBKR returned no contract for con_id {spec.con_id}.")
        return contract, details

    async def _qualified_many(
        self, specs: Sequence[ContractSpec]
    ) -> list[tuple[Contract, ContractDetails]]:
        """:meth:`_qualified` for several specs concurrently; the first failure is raised."""
        results = await asyncio.gather(
            *(self._qualified(spec) for spec in specs), return_exceptions=True
        )
        found: list[tuple[Contract, ContractDetails]] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            found.append(result)
        return found

    async def _details_for(self, contract: Contract) -> ContractDetails | None:
        """Details of an already known contract (by conId), or None if IBKR has none."""
        if not contract.conId:
            return None
        try:
            return await self.qualify_details(ContractSpec(con_id=contract.conId))
        except IbGatewayMcpError as exc:
            logger.info("No contract details for con_id %s: %s", contract.conId, exc)
            return None

    async def _combo_legs(self, contract: Contract, order_id: int) -> list[Contract]:
        """The qualified leg contracts of a working combo order, in leg order."""
        legs = contract.comboLegs or []
        if not legs or any(not leg.conId for leg in legs):
            raise InvalidRequestError(
                f"Order {order_id} is a combo whose legs this session does not know; cancel "
                "it and preview the combo again instead."
            )
        return await self.qualify_many([ContractSpec(con_id=leg.conId) for leg in legs])

    async def _check_ticks(self, planned: Sequence[_Planned]) -> None:
        """Refuse prices off the contract's price increments (IBKR's error 110).

        Uses the contract's market rules (a price-dependent ladder, e.g. 0.0001 below
        1.00 and 0.01 above for US stocks). When the rules are unknown the check is
        skipped and IBKR decides. Combos are not checked (their net price has its own
        rules).
        """
        problems: list[str] = []
        for item in planned:
            if item.details is None or item.contract.secType == "BAG":
                continue
            ladders = await self._ladders(item.details, item.contract.exchange)
            if not ladders:
                continue
            label = _ROLE_LABELS[item.role]
            for what, price in _tick_prices(item.order):
                problem = _tick_problem(f"{label} {what}", price, ladders)
                if problem is not None:
                    problems.append(problem)
        if problems:
            raise InvalidRequestError(
                "Prices off the contract's price increments (IBKR would reject them with "
                "error 110): " + "; ".join(problems) + "."
            )

    async def _ladders(
        self, details: ContractDetails, exchange: str
    ) -> list[tuple[PriceIncrement, ...]]:
        """The market-rule ladders that apply on ``exchange``; empty when unknown.

        ``marketRuleIds`` lines up with ``validExchanges``. On an exchange not listed,
        every distinct rule is a candidate and a price only needs to fit one of them.
        """
        ids = [part.strip() for part in (details.marketRuleIds or "").split(",")]
        venues = [part.strip() for part in (details.validExchanges or "").split(",")]
        if not any(ids):
            return []
        if len(ids) == len(venues) and exchange in venues:
            chosen = [ids[venues.index(exchange)]]
        else:
            chosen = list(dict.fromkeys(ids))
        ladders: list[tuple[PriceIncrement, ...]] = []
        for text in chosen:
            if not text.isdigit():
                return []
            ladder = await self._market_rule(int(text))
            if ladder is None:
                return []  # a missing candidate could be the one the price fits
            ladders.append(ladder)
        return ladders

    async def _market_rule(self, rule_id: int) -> tuple[PriceIncrement, ...] | None:
        """One market rule's price increments, cached for the session; None if unknown."""
        cached = self._market_rules.get(rule_id)
        if cached is not None:
            return cached
        ib = self.ib
        try:
            rows = await self._call(
                functools.partial(ib.reqMarketRuleAsync, rule_id),
                what=f"market rule {rule_id}",
                exclusive=market_rule_key(rule_id),
            )
        except IbGatewayMcpError as exc:
            logger.info("No market rule %s for the price-increment check: %s", rule_id, exc)
            return None
        ladder = tuple(row for row in rows or () if isinstance(row, PriceIncrement))
        if not ladder:
            return None
        self._market_rules[rule_id] = ladder
        return ladder

    async def _finish_preview(
        self,
        kind: PreviewKind,
        account: str,
        planned: list[_Planned],
        *,
        what_if: Sequence[int],
        description: str,
        warnings: Sequence[str] = (),
        modify: _ModifyData | None = None,
    ) -> OrderPreview:
        """Price increments, reference prices, order limits, what-if checks, then the token."""
        await self._check_ticks(planned)
        await self._check_limits(planned)
        for index in what_if:
            item = planned[index]
            state = await self._what_if(item.contract, item.order, what=item.description)
            item.what_if = _what_if_out(state)
        lines = [item.line() for item in planned]
        reference_warnings = self._reference_warnings(planned)
        warnings = [*warnings, *reference_warnings]
        details = [*self._details(lines), *reference_warnings]
        payload: _Body = {
            "orders": [
                {
                    "role": item.role,
                    "contract": _contract_payload(item.contract),
                    "order": _order_payload(item.order),
                    "parent": item.parent,
                }
                for item in planned
            ],
            "summaries": [item.summary.model_dump(mode="json") for item in planned if item.summary],
        }
        if modify is not None:
            payload["modify"] = modify
        return self._issue(
            kind,
            account,
            lines,
            description=description,
            details=details,
            warnings=list(warnings),
            payload=payload,
            what_if=lines[what_if[0]].what_if if what_if else None,
        )

    def _issue(
        self,
        kind: PreviewKind,
        account: str,
        lines: list[OrderLine],
        *,
        description: str,
        details: list[str],
        warnings: list[str],
        payload: _Body,
        what_if: WhatIfOut | None = None,
    ) -> OrderPreview:
        """Store the payload behind a token, audit the preview and describe it."""
        paper = _is_paper_action(self.accounts, kind, account, payload)
        if not paper:
            warnings = [
                *warnings,
                "Live (real-money) account: submit_order may ask a human to confirm.",
            ]
        stored: _Payload = {
            "v": _PAYLOAD_VERSION,
            "description": description,
            "details": details,
            **payload,
        }
        issued = self.safety.previews.issue(stored, account, kind)
        self.safety.audit.record(
            AuditEvent.PREVIEW,
            token=issued.token,
            kind=kind,
            account=account,
            description=description,
            details=details,
        )
        return OrderPreview(
            token=issued.token,
            expires_at=issued.expires_at,
            account=account,
            is_paper=paper,
            kind=kind,
            summary=description,
            orders=lines,
            what_if=what_if,
            details=details,
            warnings=warnings,
        )

    @staticmethod
    def _details(lines: Sequence[OrderLine]) -> list[str]:
        details: list[str] = []
        several = len(lines) > 1
        for line in lines:
            prefix = f"{_ROLE_LABELS[line.role]}: " if several else ""
            if several:
                details.append(prefix + line.description)
            currency = line.contract.currency or ""
            if line.algo_strategy:
                params = ", ".join(f"{tag}={value}" for tag, value in line.algo_params.items())
                details.append(f"{prefix}Algo {line.algo_strategy}: {params or 'defaults'}")
            if line.notional is not None:
                details.append(f"{prefix}Estimated notional: {_amount(line.notional)} {currency}")
            if line.what_if is not None:
                details.extend(prefix + text for text in _what_if_lines(line.what_if))
        return details

    async def _check_limits(self, planned: list[_Planned]) -> None:
        """Fill each order's summary (reference price, multiplier) and check the limits."""
        prices: dict[int, _Reference] = {}
        if self.safety.policy.max_notional is not None:
            wanted: dict[int, Contract] = {}
            for item in planned:
                if item.contract.secType == "BAG":
                    wanted.update((leg.conId, leg) for leg in item.legs)
                elif _needs_reference(item.order):
                    wanted[item.contract.conId] = item.contract
            prices = await self._reference_prices(list(wanted.values()))
        for item in planned:
            item.summary = self._summary(item, prices)
            estimate = estimate_notional(item.summary)
            item.notional = float(estimate.value) if estimate is not None else None
        for item in planned:
            if item.summary is not None:
                self.safety.policy.check(item.summary)

    @staticmethod
    def _summary(item: _Planned, prices: dict[int, _Reference]) -> OrderSummary:
        contract, order = item.contract, item.order
        legs: tuple[LegSummary, ...] = ()
        if contract.secType == "BAG":
            leg_refs = [prices.get(leg_contract.conId) for leg_contract in item.legs]
            legs = tuple(
                LegSummary(
                    symbol=_symbol(leg_contract),
                    sec_type=leg_contract.secType,
                    action=leg.action,
                    ratio=leg.ratio,
                    multiplier=_multiplier(leg_contract),
                    price=ref.price if ref is not None else None,
                    con_id=leg_contract.conId,
                )
                for leg, leg_contract, ref in zip(
                    contract.comboLegs, item.legs, leg_refs, strict=True
                )
            )
            stale = [ref for ref in leg_refs if ref is not None and not ref.is_live]
            item.reference = stale[0] if stale else None
        else:
            item.reference = prices.get(contract.conId)
            item.reference_price = item.reference.price if item.reference else None
        return OrderSummary(
            symbol=_symbol(contract),
            sec_type=contract.secType or "STK",
            action=order.action,
            quantity=_quantity(order.totalQuantity),
            order_type=order.orderType,
            limit_price=_price(order.lmtPrice),
            aux_price=_price(order.auxPrice),
            multiplier=None if legs else _multiplier(contract),
            currency=contract.currency or "USD",
            reference_price=item.reference_price,
            legs=legs,
        )

    async def _reference_prices(self, contracts: Sequence[Contract]) -> dict[int, _Reference]:
        """Current prices by conId: a live ticker if one streams, else one snapshot.

        Failures (no market data permission, timeouts) leave the price out; the order
        limits then refuse orders whose notional cannot be bounded. Each price records
        its market data type (the session's, e.g. delayed after set_market_data_type),
        so a preview can say when the check rests on data that is not live.
        """
        if not contracts:
            return {}
        ib = self.ib
        prices: dict[int, _Reference] = {}
        missing: list[Contract] = []
        for contract in contracts:
            try:
                cached = ib.ticker(contract)
            except ValueError:  # unqualified contract: ib_async cannot hash it
                cached = None
            price = _ticker_price(cached) if _is_fresh(cached) else None
            if price is None:
                missing.append(contract)
            else:
                prices[contract.conId] = price
        if missing:
            try:
                tickers = await self._call(
                    ib.reqTickersAsync(*missing),
                    what="a market snapshot for the notional check",
                    timeout=min(self.settings.request_timeout, REFERENCE_PRICE_TIMEOUT),
                )
            except IbGatewayMcpError as exc:
                logger.info("No reference price for the notional check: %s", exc)
                tickers = []
            for ticker in tickers or []:
                price = _ticker_price(ticker)
                if price is not None and ticker.contract is not None:
                    prices[ticker.contract.conId] = price
        return prices

    @staticmethod
    def _reference_warnings(planned: Sequence[_Planned]) -> list[str]:
        """Say when a notional check rests on a price that is not live market data."""
        warnings: list[str] = []
        for item in planned:
            ref = item.reference
            if ref is None or ref.is_live:
                continue
            warnings.append(
                f"{_ROLE_LABELS[item.role]}: the notional check used a {ref.describe()} "
                f"for {_contract_label(item.contract)}, not a live price; the fill can be "
                "far from it."
            )
        return warnings

    async def _what_if(self, contract: Contract, order: Order, *, what: str) -> OrderState:
        """Run IBKR's what-if check for one order, failing fast on warning 321.

        The request goes out with a private copy of the contract: ib_async attaches it
        to errors about the request, which is how a 321 for this what-if is recognised.
        """
        ib = self.ib
        probe = copy.copy(contract)
        test = copy.copy(order)
        test.transmit = True
        test.parentId = 0

        def ended(
            req_id: int, code: int, message: str, error_contract: object
        ) -> IbApiError | None:
            if error_contract is probe and code in REQUEST_ENDING_WARNINGS:
                return IbApiError(code, message, req_id)
            return None

        return await self._call(
            lambda: self._await_or_reject(lambda: ib.whatIfOrderAsync(probe, test), ended),
            what=f"the what-if check of {what}",
        )

    def _exercise_warnings(self, contract: Contract, account: str, quantity: int) -> list[str]:
        held = sum(
            _quantity(position.position)
            for position in self.ib.positions(account)
            if position.contract.conId == contract.conId
        )
        label = _contract_label(contract)
        if held <= 0:
            return [
                f"No long position in {label} is known for account {account}; IBKR rejects "
                "exercising contracts the account does not hold."
            ]
        if held < quantity:
            return [f"Account {account} holds only {_num(held)} of {label}."]
        return []
