"""The orders service: previews, token-bound submission, cancel and status."""

from __future__ import annotations

import copy
import secrets
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from ib_async import ComboLeg, Contract, OrderStatus, TagValue, Trade
from ib_async.objects import PriceIncrement

from ib_gateway_mcp._util import clean_int, contract_to_out
from ib_gateway_mcp.accounts import account_of, is_paper_account
from ib_gateway_mcp.errors import InvalidRequestError, NotFoundError, TokenError
from ib_gateway_mcp.models.common import ConfirmationRequest
from ib_gateway_mcp.models.orders import (
    BracketSpec,
    CancelScope,
    ComboSpec,
    ExerciseSpec,
    ModifySpec,
    OcaSpec,
    OrderLine,
    OrderPreview,
    OrderResult,
    OrderSpec,
    OrderStatusOut,
)
from ib_gateway_mcp.safety.audit import AuditEvent
from ib_gateway_mcp.safety.policy import estimate_notional
from ib_gateway_mcp.services._account_rows import trade_quantities
from ib_gateway_mcp.services.orders._cancel import _CancelAllMixin
from ib_gateway_mcp.services.orders._constants import (
    _OCA_TYPES,
    _OPTION_TYPES,
    _SUBMIT_EVENTS,
    EXERCISE_WAIT,
    MAX_LISTED_ORDERS,
    STATUS_WAIT,
)
from ib_gateway_mcp.services.orders._describe import (
    _contract_label,
    _describe_order,
    _money,
    _price_problems,
    _symbol,
)
from ib_gateway_mcp.services.orders._payloads import (
    _cancel_all_data,
    _exercise_data,
    _order_payload,
    _stored,
)
from ib_gateway_mcp.services.orders._planning import (
    _apply_changes,
    _cancel_all_text,
    _cancel_lines,
    _delivered_summary,
    _exercise_summary,
    _exercise_text,
    _modify_problems,
    _new_order,
    _order_from_spec,
    _Planned,
)
from ib_gateway_mcp.services.orders._preview import _PreviewMixin
from ib_gateway_mcp.services.orders._trades import (
    _find,
    _is_global_cancel,
    _is_open,
    _is_paper_action,
    _kept_current,
    _looks_stale,
    _messages,
    _order_count,
    _order_errors,
    _status_out,
)

if TYPE_CHECKING:
    from ib_gateway_mcp.gateway import Gateway


class OrdersService(_PreviewMixin, _CancelAllMixin):
    """Order previews (what-if), token-bound submission, modification, cancellation and exercise.

    Every method that can change something at IBKR (previews included, since a token
    is useless while trading is off) calls ``connection.require_trading()`` first.

    Attributes:
        status_wait: Seconds a submit or cancel waits for IBKR's first status.
        exercise_wait: Seconds an exercise watches for an error.
    """

    status_wait: float = STATUS_WAIT
    exercise_wait: float = EXERCISE_WAIT

    def __init__(self, gateway: Gateway) -> None:
        super().__init__(gateway)
        self._perm_ids: OrderedDict[int, int] = OrderedDict()
        self._market_rules: dict[int, tuple[PriceIncrement, ...]] = {}

    # --- previews ---------------------------------------------------------------------

    async def preview_order(self, spec: OrderSpec, *, account: str | None = None) -> OrderPreview:
        """Validate and what-if one order; return a token for :meth:`submit`.

        Raises:
            AccountNotAllowedError, ConfigurationError: Account problems.
            NotFoundError, AmbiguousContractError: The contract cannot be resolved.
            OrderLimitError: The order breaks a configured limit.
            IbApiError: IBKR rejected the what-if check (e.g. 201, or 321 read-only API).
            RequestTimeoutError, NotConnectedError: Gateway problems.
        """
        with self._refusals("preview", "order", account):
            self._start_preview()
            acct = self.accounts.resolve(account)
            contract, details = await self._qualified(spec.contract)
            order = _order_from_spec(spec, acct)
            planned = [
                _Planned(
                    "order", contract, order, _describe_order(contract, order), details=details
                )
            ]
            return await self._finish_preview(
                "order", acct, planned, what_if=[0], description=planned[0].description
            )

    async def preview_bracket(
        self, spec: BracketSpec, *, account: str | None = None
    ) -> OrderPreview:
        """Validate a bracket (entry, take profit, stop loss); what-if the entry.

        The children cannot be what-if checked on their own (they depend on the entry),
        so all three prices are checked against the contract's price increments (market
        rules) here. At submit, only the stop loss (the last order) transmits the three;
        if IBKR rejects any of them, the rest of the bracket is cancelled.
        """
        with self._refusals("preview", "bracket", account):
            self._start_preview()
            acct = self.accounts.resolve(account)
            contract, details = await self._qualified(spec.contract)
            reverse = "SELL" if spec.action == "BUY" else "BUY"
            common: dict[str, Any] = {
                "quantity": spec.quantity,
                "account": acct,
                "tif": spec.tif,
                "good_till_date": spec.good_till_date,
                "outside_rth": spec.outside_rth,
                "order_ref": spec.order_ref,
                "model_code": spec.model_code,
                "soft_dollar_tier": spec.soft_dollar_tier,
            }
            entry_limit = spec.entry_price if spec.entry_type in ("LMT", "STP LMT") else None
            entry_stop = spec.entry_price if spec.entry_type == "STP" else spec.entry_stop_price
            entry = _new_order(
                action=spec.action,
                order_type=spec.entry_type,
                limit_price=entry_limit,
                aux_price=entry_stop,
                transmit=False,
                **common,
            )
            take_profit = _new_order(
                action=reverse,
                order_type="LMT",
                limit_price=spec.take_profit_price,
                transmit=False,
                **common,
            )
            stop_loss = _new_order(
                action=reverse,
                order_type="STP",
                aux_price=spec.stop_loss_price,
                transmit=True,
                **common,
            )
            planned = [
                _Planned("entry", contract, entry, _describe_order(contract, entry)),
                _Planned(
                    "take_profit",
                    contract,
                    take_profit,
                    _describe_order(contract, take_profit),
                    parent=0,
                ),
                _Planned(
                    "stop_loss", contract, stop_loss, _describe_order(contract, stop_loss), parent=0
                ),
            ]
            for item in planned:
                item.details = details
            description = (
                f"BRACKET {planned[0].description}; take profit {_money(spec.take_profit_price)}; "
                f"stop loss {_money(spec.stop_loss_price)}"
            )
            return await self._finish_preview(
                "bracket", acct, planned, what_if=[0], description=description
            )

    async def preview_oca(self, spec: OcaSpec, *, account: str | None = None) -> OrderPreview:
        """Validate and what-if 2-10 orders placed as one One-Cancels-All group."""
        with self._refusals("preview", "oca", account):
            self._start_preview()
            acct = self.accounts.resolve(account)
            found = await self._qualified_many([member.contract for member in spec.orders])
            group = f"mcp-oca-{secrets.token_hex(6)}"
            planned: list[_Planned] = []
            for member, (contract, details) in zip(spec.orders, found, strict=True):
                order = _order_from_spec(member, acct)
                order.ocaGroup = group
                order.ocaType = spec.oca_type
                planned.append(
                    _Planned(
                        "oca_member",
                        contract,
                        order,
                        _describe_order(contract, order),
                        details=details,
                    )
                )
            description = (
                f"OCA group of {len(planned)} orders ({_OCA_TYPES[spec.oca_type]}): "
                + "; ".join(item.description for item in planned)
            )
            return await self._finish_preview(
                "oca", acct, planned, what_if=list(range(len(planned))), description=description
            )

    async def preview_combo(self, spec: ComboSpec, *, account: str | None = None) -> OrderPreview:
        """Qualify the legs, build the BAG contract and what-if the combo order."""
        with self._refusals("preview", "combo", account):
            self._start_preview()
            acct = self.accounts.resolve(account)
            legs = await self.qualify_many([leg.contract for leg in spec.legs])
            con_ids = [leg.conId for leg in legs]
            if len(set(con_ids)) != len(con_ids):
                raise InvalidRequestError("Each combo leg must be a different contract.")
            currencies = {leg.currency for leg in legs}
            if len(currencies) != 1:
                raise InvalidRequestError(
                    "All combo legs must trade in one currency; got "
                    + ", ".join(sorted(currencies))
                    + "."
                )
            exchanges = {leg.exchange for leg in legs}
            bag = Contract(
                secType="BAG",
                symbol=",".join(dict.fromkeys(_symbol(leg) for leg in legs)),
                currency=currencies.pop(),
                exchange=exchanges.pop() if len(exchanges) == 1 else "SMART",
            )
            bag.comboLegs = [
                ComboLeg(
                    conId=contract.conId,
                    ratio=leg.ratio,
                    action=leg.action,
                    exchange=contract.exchange or "SMART",
                )
                for leg, contract in zip(spec.legs, legs, strict=True)
            ]
            order = _new_order(
                action=spec.action,
                quantity=spec.quantity,
                order_type=spec.order_type,
                account=acct,
                tif=spec.tif,
                outside_rth=spec.outside_rth,
                order_ref=spec.order_ref,
                limit_price=spec.limit_price,
                model_code=spec.model_code,
                soft_dollar_tier=spec.soft_dollar_tier,
            )
            if spec.non_guaranteed:
                order.smartComboRoutingParams = [TagValue("NonGuaranteed", "1")]
            leg_text = " + ".join(
                f"{leg.action} {leg.ratio} {_contract_label(contract)}"
                for leg, contract in zip(spec.legs, legs, strict=True)
            )
            label = f"combo [{leg_text}]"
            description = _describe_order(bag, order, label=label)
            if spec.non_guaranteed:
                description += " (non-guaranteed: legs may fill separately)"
            planned = [_Planned("combo", bag, order, description, legs=legs)]
            return await self._finish_preview(
                "combo", acct, planned, what_if=[0], description=description
            )

    async def preview_modify(self, order_id: int, changes: ModifySpec) -> OrderPreview:
        """Validate changes to a working order placed by this server; what-if the result.

        The what-if evaluates the modified order as if it were new (its whole margin
        impact, not the difference). The token remembers the order's terms and filled
        quantity; the submit is refused if either changed in between.

        Raises:
            NotFoundError: No such order from this server's client id.
            InvalidRequestError: Another client's order, a finished order, a change the
                order type does not support, or prices off the contract's increments.
            AccountNotAllowedError: The order carries no account, or one outside the
                allowlist.
        """
        with self._refusals("preview", "modify", None):
            self._start_preview()
            trade = await self._own_trade(order_id)
            acct = self._order_account(trade, order_id)
            if not _is_open(trade):
                raise InvalidRequestError(
                    f"Order {order_id} is {trade.orderStatus.status}; only working orders can "
                    "be modified."
                )
            problems = _modify_problems(trade.order, changes)
            if problems:
                raise InvalidRequestError(
                    f"Cannot modify order {order_id}: " + "; ".join(problems) + "."
                )
            modified = copy.copy(trade.order)
            _apply_changes(modified, changes)
            modified.account = acct
            contract = trade.contract
            combo = contract.secType == "BAG"
            if not combo:
                before = _price_problems(trade.order)
                new = [problem for problem in _price_problems(modified) if problem not in before]
                if new:
                    raise InvalidRequestError(
                        f"Cannot modify order {order_id}: " + "; ".join(new) + "."
                    )
            legs = await self._combo_legs(contract, order_id) if combo else []
            price_changed = any(
                value is not None
                for value in (changes.limit_price, changes.aux_price, changes.trail_stop_price)
            )
            details = await self._details_for(contract) if price_changed and not combo else None
            original = _describe_order(contract, trade.order)
            item = _Planned(
                "modify",
                contract,
                modified,
                _describe_order(contract, modified),
                legs=legs,
                order_id=order_id,
                details=details,
            )
            description = f"MODIFY order {order_id}: {original} -> {item.description}"
            filled, _remaining = trade_quantities(trade)
            return await self._finish_preview(
                "modify",
                acct,
                [item],
                what_if=[0],
                description=description,
                warnings=["The what-if shows the modified order's full impact, as if it were new."],
                modify={
                    "order_id": order_id,
                    "perm_id": clean_int(trade.order.permId) or None,
                    "original": _order_payload(trade.order),
                    "filled": filled,
                },
            )

    async def preview_exercise(
        self, spec: ExerciseSpec, *, account: str | None = None
    ) -> OrderPreview:
        """Describe an option exercise or lapse (IBKR has no what-if for exercises).

        The order limits apply as if the exercise traded ``quantity x multiplier`` of the
        underlying at the strike: to the option itself, and for an exercise also to what
        it delivers (the underlying's symbol and sec type, e.g. STK for OPT, FUT for FOP;
        cash-settled index options deliver nothing).
        """
        with self._refusals("preview", "exercise", account):
            self._start_preview()
            acct = self.accounts.resolve(account)
            requested = spec.contract.sec_type
            # sec_type defaults to STK; next to a con_id that default is no claim (the id is
            # looked up alone), so a con_id-only spec is checked once qualified.
            claimed = "sec_type" in spec.contract.model_fields_set or not spec.contract.con_id
            if claimed and requested not in _OPTION_TYPES:
                raise InvalidRequestError(
                    f"Only options (OPT) and futures options (FOP) can be exercised, not "
                    f"{requested}."
                )
            contract, option_details = await self._qualified(spec.contract)
            if contract.secType not in _OPTION_TYPES:
                raise InvalidRequestError(
                    f"Only options (OPT) and futures options (FOP) can be exercised, not "
                    f"{contract.secType or requested} ({_contract_label(contract)})."
                )
            summary = _exercise_summary(contract, spec.quantity)
            summaries = [summary]
            if spec.action == "exercise":
                delivered = _delivered_summary(
                    contract, option_details, spec.quantity, summary.action
                )
                if delivered is not None:
                    summaries.append(delivered)
            for checked in summaries:
                self.safety.policy.check(checked)
            estimate = estimate_notional(summary)
            description, details = _exercise_text(spec, contract, summary.action)
            warnings = self._exercise_warnings(contract, acct, spec.quantity)
            line = OrderLine(
                role="exercise",
                description=description,
                contract=contract_to_out(contract),
                action="EXERCISE" if spec.action == "exercise" else "LAPSE",
                quantity=float(spec.quantity),
                notional=float(estimate.value) if estimate is not None else None,
            )
            return self._issue(
                "exercise",
                acct,
                [line],
                description=description,
                details=[*details, *warnings],
                warnings=warnings,
                payload={
                    "exercise": _exercise_data(spec, contract),
                    "summaries": [checked.model_dump(mode="json") for checked in summaries],
                },
            )

    async def preview_cancel_all(
        self, *, account: str | None = None, scope: CancelScope = "this_client"
    ) -> OrderPreview:
        """List the working orders a cancel-all would cancel; return a token for it.

        ``this_client`` covers the orders this server's client id placed in ``account``;
        they are cancelled one by one. ``global`` is IBKR's global cancel: every working
        order on the login (all accounts, all API clients, manual TWS orders); it is
        refused unless the account allowlist covers every managed account.

        Raises:
            NotFoundError: No working orders to cancel.
            AccountNotAllowedError: ``global`` while some managed account is not allowed.
        """
        with self._refusals("preview", "cancel_all", account):
            self._start_preview()
            acct = self.accounts.resolve(account)
            own = self.settings.ib_client_id
            targets = await self._cancel_all_targets(scope, acct)
            if not targets:
                where = (
                    "on this login"
                    if scope == "global"
                    else f"placed by this server (client id {own}) in account {acct}"
                )
                raise NotFoundError(f"There are no working orders {where}; nothing to cancel.")
            description, warnings = _cancel_all_text(scope, len(targets), own, acct)
            shown = _cancel_lines(targets[:MAX_LISTED_ORDERS])
            return self._issue(
                "cancel_all",
                acct,
                shown,
                description=description,
                details=[line.description for line in shown],
                warnings=warnings,
                payload={"cancel_all": _cancel_all_data(scope, targets), "summaries": []},
            )

    # --- confirmation and submit --------------------------------------------------------

    def confirmation_request(self, token: str) -> ConfirmationRequest:
        """Describe what ``token`` would do, for a human to approve. Does not consume it.

        Deterministic: the text comes from what the preview stored. A global cancel on a
        login that manages live accounts names those accounts (it cancels their orders
        too), whichever account the preview was made for.

        Raises:
            TokenNotFoundError, TokenExpiredError, TokenMismatchError: Unusable token.
        """
        record, kind = self._order_record(token)
        payload = _stored(record)
        paper = _is_paper_action(self.accounts, kind, record.account, payload)
        account = record.account
        if not paper and _is_global_cancel(kind, payload):
            account = ", ".join(self.accounts.live_accounts_in_scope) or account
        return ConfirmationRequest(
            account=account,
            is_paper=paper,
            action=payload["description"],
            details=list(payload["details"]),
        )

    def precheck(self, token: str) -> None:
        """Run submit's refusals for ``token`` without consuming it or taking a rate slot.

        The circuit breaker, the order limits, the live-trading switch and the rate limit
        are checked as :meth:`submit` would; only the human confirmation is left out. The
        MCP layer calls this before asking a human, so nobody approves an action the
        server would refuse anyway. :meth:`submit` checks everything again.

        Raises:
            TokenNotFoundError, TokenExpiredError, TokenMismatchError: Unusable token.
            OrderLimitError, CircuitOpenError, RateLimitError, LiveTradingDisabledError:
                The submit would be refused.
        """
        record, kind = self._order_record(token)
        with self._refusals("precheck", kind, record.account, token=token):
            self._check_submit(kind, record.account, _stored(record), human_confirmed=True)

    def discard(self, token: str, *, reason: str) -> None:
        """Burn a token that will not be submitted (e.g. a human declined) and audit why.

        Later uses of the token say it was discarded and nothing was sent.
        """
        note = reason.split("; ", 1)[0].split(". ", 1)[0].rstrip(".")
        try:
            record = self.safety.previews.discard(token, note=note)
        except TokenError:
            return
        self.safety.audit.record(
            AuditEvent.REJECTED,
            stage="confirmation",
            token=token,
            kind=record.kind,
            account=record.account,
            reason=reason,
        )

    async def submit(self, token: str, *, human_confirmed: bool = False) -> OrderResult:
        """Execute a previewed action by its token (single-use).

        Re-checks the order limits and the circuit breaker, refuses live accounts
        without ``IBKR_MCP_ALLOW_LIVE`` or (with ``IBKR_MCP_LIVE_CONFIRM`` on) without
        ``human_confirmed``, and checks the rate limit (one slot per order: a bracket
        takes 3, an OCA group one per member). Only when every check passes is the token
        consumed and the slots taken; a refused submit leaves the token usable until it
        expires (e.g. retry after a rate-limit wait). Then it places exactly the stored
        orders and waits up to :attr:`status_wait` seconds for IBKR's first status.
        Cancel-all is not rate limited or halted by the breaker (it reduces risk).

        Raises:
            TokenNotFoundError, TokenExpiredError, TokenMismatchError: Unusable token.
            OrderLimitError, CircuitOpenError, RateLimitError: Safety rails refused.
            LiveTradingDisabledError, ConfirmationUnavailableError: Live gate refused.
            NotConnectedError: The gateway went away.
        """
        safety = self.safety
        with self._refusals("submit", None, None, token=token):
            self.connection.require_trading()
            record, kind = self._order_record(token)
        account = record.account
        payload = _stored(record)
        with self._refusals("submit", kind, account, token=token):
            self._check_submit(kind, account, payload, human_confirmed=human_confirmed)
            # No await since the checks: nothing can take the token or the slots between.
            safety.previews.consume_record(token)
            count = _order_count(kind, payload)
            if count:
                safety.rate_limiter.acquire(count)
        safety.audit.record(
            _SUBMIT_EVENTS.get(kind, AuditEvent.SUBMIT),
            token=token,
            kind=kind,
            account=account,
            description=payload["description"],
            human_confirmed=human_confirmed,
        )
        with self._refusals("submit", kind, account, token=token):
            if kind == "exercise":
                result = await self._submit_exercise(account, payload)
            elif kind == "modify":
                result = await self._submit_modify(account, payload)
            elif kind == "cancel_all":
                result = await self._submit_cancel_all(account, payload)
            else:
                result = await self._submit_orders(kind, account, payload)
        safety.audit.record(
            AuditEvent.SUBMIT_RESULT,
            token=token,
            kind=kind,
            account=account,
            accepted=result.accepted,
            status=result.status,
            order_ids=result.order_ids,
            perm_id=result.perm_id,
            messages=result.messages,
        )
        return result

    # --- cancel and status --------------------------------------------------------------

    async def cancel_confirmation_request(self, order_id: int) -> ConfirmationRequest:
        """Describe cancelling order ``order_id``, for a human to approve (live accounts).

        Side-effect free apart from re-syncing open orders when the cache is unsure.
        Raises what :meth:`cancel` would (other than the missing confirmation), so nobody
        is asked to approve a cancel that would be refused.

        Raises:
            NotFoundError, InvalidRequestError, AccountNotAllowedError: As :meth:`cancel`.
            LiveTradingDisabledError: A live account without IBKR_MCP_ALLOW_LIVE.
        """
        trade = await self._own_trade(order_id)
        account = self._order_account(trade, order_id)
        if not _is_open(trade):  # refuse before anyone is asked, as cancel() would
            raise InvalidRequestError(
                f"Order {order_id} is already {trade.orderStatus.status}; there is nothing "
                "to cancel."
            )
        self._check_live(account, human_confirmed=True)
        # No live status in the text: the question must stay the same on every round.
        details: list[str] = []
        parent = clean_int(trade.order.parentId)
        if parent:
            details.append(
                f"This order is attached to order {parent} (e.g. a bracket's take profit or "
                "stop loss): cancelling it removes that exit from the position."
            )
        elif trade.order.orderType in ("STP", "STP LMT", "TRAIL", "TRAIL LIMIT"):
            details.append("A stop order: cancelling it may leave a position unprotected.")
        return ConfirmationRequest(
            account=account,
            is_paper=is_paper_account(account),
            action=f"CANCEL order {order_id}: " + _describe_order(trade.contract, trade.order),
            details=details,
        )

    def cancel_declined(self, order_id: int, *, reason: str) -> None:
        """Audit a cancel that a human declined (or could not be asked about)."""
        self.safety.audit.record(
            AuditEvent.REJECTED,
            stage="confirmation",
            kind="cancel",
            order_id=order_id,
            reason=reason,
        )

    async def cancel(self, order_id: int, *, human_confirmed: bool = False) -> OrderResult:
        """Cancel one working order placed by this server's client id (no preview).

        On a live account this needs ``IBKR_MCP_ALLOW_LIVE`` and, with
        ``IBKR_MCP_LIVE_CONFIRM`` on, ``human_confirmed`` (cancelling a stop loss can
        leave a real position unprotected), like a cancel-all.

        Raises:
            NotFoundError: No such order from this client id.
            InvalidRequestError: Another client's order, or already finished.
            AccountNotAllowedError: The order's account is missing or outside the allowlist.
            LiveTradingDisabledError, ConfirmationUnavailableError: Live gate refused.
        """
        with self._refusals("cancel", "cancel", None):
            self.connection.require_trading()
            trade = await self._own_trade(order_id)
            account = self._order_account(trade, order_id)
            if not _is_open(trade):
                raise InvalidRequestError(
                    f"Order {order_id} is already {trade.orderStatus.status}; there is nothing "
                    "to cancel."
                )
            self._check_live(account, human_confirmed=human_confirmed)
            self.safety.audit.record(
                AuditEvent.CANCEL,
                order_id=order_id,
                account=account,
                description=_describe_order(trade.contract, trade.order),
                human_confirmed=human_confirmed,
            )
            start = len(trade.log)
            self._send_cancel(trade)
        await self._wait([trade], lambda t: t.isDone(), self.status_wait)
        errors = _order_errors(trade, start)
        out = _status_out(trade, self.settings.ib_client_id, "cancel")
        messages = _messages([(trade, start)])
        if not trade.isDone() and not errors:
            messages.append(
                f"IBKR has not confirmed the cancellation within {self.status_wait:g} s; "
                "check get_order_status."
            )
        if trade.orderStatus.status == OrderStatus.Filled:
            messages.append(f"Order {order_id} filled before it could be cancelled.")
        result = OrderResult(
            kind="cancel",
            account=account,
            accepted=not errors,
            status=out.status,
            order_id=out.order_id,
            perm_id=out.perm_id,
            filled=out.filled,
            remaining=out.remaining,
            avg_fill_price=out.avg_fill_price,
            order_ids=[order_id],
            orders=[out],
            messages=messages,
        )
        self.safety.audit.record(
            AuditEvent.SUBMIT_RESULT,
            kind="cancel",
            account=account,
            accepted=result.accepted,
            status=result.status,
            order_ids=result.order_ids,
            messages=result.messages,
        )
        return result

    async def order_status(
        self, *, order_id: int | None = None, perm_id: int | None = None
    ) -> OrderStatusOut:
        """Status, fills and log of one order in an allowed account.

        ``order_id`` finds orders placed by this server's client id; ``perm_id`` finds any
        order on the login. Looks in the session cache (this client's orders, which IBKR
        keeps current), then in IBKR's answer for the open orders of every client, then
        in recently completed orders. IBKR's completed orders carry no order
        id, so ``order_id`` reaches a completed one only if this server placed it (or
        saw it) since it started; ``perm_id`` always works.

        Raises:
            InvalidRequestError: Not exactly one of ``order_id`` and ``perm_id``.
            NotFoundError: No such order in an allowed account.
        """
        if (order_id is None) == (perm_id is None):
            raise InvalidRequestError("Give exactly one of order_id and perm_id.")
        own = self.settings.ib_client_id
        known_perm = self._perm_ids.get(order_id) if order_id is not None else None

        def match(trade: Trade) -> bool:
            if not self.accounts.is_allowed(account_of(trade)):
                return False
            if perm_id is not None:
                return bool(trade.order.permId == perm_id)
            if trade.order.orderId == order_id and trade.order.clientId == own:
                return True
            return bool(known_perm and trade.order.permId == known_perm)

        ib = self.ib
        # Only this client's cached orders are current; any other order is looked up in
        # IBKR's fresh answers below, never in the cache.
        trade = _find((t for t in ib.trades() if _kept_current(t, own)), match)
        if trade is not None and _looks_stale(trade):
            await self._resync_open_orders()
        if trade is None:
            opened = await self._call(
                ib.reqAllOpenOrdersAsync, what="open orders", exclusive="openOrders"
            )
            trade = _find(opened or [], match)
        if trade is None:
            completed = await self._call(
                lambda: ib.reqCompletedOrdersAsync(False),
                what="completed orders",
                exclusive="completedOrders",
            )
            trade = _find(completed or [], match)
        if trade is None:
            what = (
                f"perm_id {perm_id}"
                if perm_id is not None
                else f"order_id {order_id} (placed by this server, client id {own})"
            )
            raise NotFoundError(
                f"No order with {what} in an allowed account, among working and recently "
                "completed orders. get_open_orders lists working orders with their ids; "
                "orders completed before this server started are found by perm_id."
            )
        self._remember(trade)
        out = _status_out(trade, own)
        if order_id is not None and out.order_id is None:
            # A completed order from IBKR, found by the perm id this server remembered.
            out = out.model_copy(
                update={"order_id": order_id, "client_id": own, "placed_by_this_server": True}
            )
        return out
