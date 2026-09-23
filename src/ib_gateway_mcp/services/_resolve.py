"""Resolving a :class:`~ib_gateway_mcp.models.common.ContractSpec` to one IBKR contract.

Every service qualifies contracts through :meth:`BaseService.qualify
<ib_gateway_mcp.services.base.BaseService.qualify>`; the contracts service also lists
every match. Both build the ``reqContractDetails`` request, word "not found" and pick
the one contract a set of rows describes with the helpers here.
"""

from __future__ import annotations

from collections.abc import Sequence

from ib_async import Contract, ContractDetails

from ib_gateway_mcp._util import contract_from_spec, contract_to_out
from ib_gateway_mcp.errors import AmbiguousContractError, NotFoundError
from ib_gateway_mcp.models.common import ContractOut, ContractSpec

__all__ = [
    "MAX_LISTED_CANDIDATES",
    "NO_SECURITY_DEFINITION",
    "describe_spec",
    "details_request",
    "not_found_message",
    "pick_details",
]

MAX_LISTED_CANDIDATES = 20
"""How many candidates an :class:`AmbiguousContractError` from :meth:`BaseService.qualify`
carries and lists."""

NO_SECURITY_DEFINITION = 200
"""IB error 200: no security definition matches the request."""


def describe_spec(spec: ContractSpec) -> str:
    """A short, readable description of a contract spec for messages, e.g. ``SPY OPT 20261218
    500 C SMART USD`` or ``con_id 265598``."""
    if spec.con_id:
        return f"con_id {spec.con_id}"
    parts: list[str] = []
    if spec.sec_id_type and spec.sec_id:
        parts.append(f"{spec.sec_id_type} {spec.sec_id}")
    if spec.issuer_id:
        parts.append(f"issuer {spec.issuer_id}")
    parts.append(spec.symbol or spec.local_symbol or "")
    parts.append(spec.sec_type)
    if spec.last_trade_date_or_contract_month:
        parts.append(spec.last_trade_date_or_contract_month)
    if spec.strike is not None:
        parts.append(f"{spec.strike:g}")
    if spec.right:
        parts.append(spec.right)
    if spec.trading_class:
        parts.append(f"class {spec.trading_class}")
    exchange = spec.exchange
    if spec.primary_exchange:
        exchange = f"{exchange}/{spec.primary_exchange}" if exchange else spec.primary_exchange
    parts.extend([exchange, spec.currency])
    return " ".join(part for part in parts if part)


def _describe_candidate(out: ContractOut) -> str:
    fields = [
        f"con_id {out.con_id}",
        out.local_symbol or out.symbol,
        out.sec_type,
        out.last_trade_date_or_contract_month,
        f"{out.strike:g}" if out.strike is not None else None,
        out.right,
        f"class {out.trading_class}" if out.trading_class else None,
        f"x{out.multiplier}" if out.multiplier else None,
        "/".join(part for part in (out.exchange, out.primary_exchange) if part) or None,
        out.currency,
        f"({out.description})" if out.description else None,
    ]
    return " ".join(field for field in fields if field)


def details_request(spec: ContractSpec) -> Contract:
    """The contract to send with ``reqContractDetails`` for ``spec``.

    With a ``con_id``, only the id goes out, plus the security type and exchange when the
    caller set them explicitly: the spec's defaults (STK, SMART, USD) would otherwise
    make IBKR reject the id of a future or an option.
    """
    if not spec.con_id:
        return contract_from_spec(spec)
    explicit = spec.model_fields_set
    return Contract(
        conId=spec.con_id,
        secType=spec.sec_type if "sec_type" in explicit else "",
        exchange=spec.exchange if "exchange" in explicit else "",
        includeExpired=spec.include_expired,
    )


def not_found_message(spec: ContractSpec) -> str:
    return (
        f"No contract matches {describe_spec(spec)}. Check the symbol, sec_type, currency and "
        "exchange (futures and indexes need their listing exchange, e.g. CME or CBOE, not "
        "SMART); search_symbols finds symbols and get_option_chain lists option expiries "
        "and strikes."
    )


def pick_details(
    spec: ContractSpec, request: Contract, rows: Sequence[ContractDetails]
) -> tuple[Contract, ContractDetails]:
    """Choose the one contract ``rows`` describe, or raise not-found or ambiguous."""
    found = [(row.contract, row) for row in rows if row.contract is not None and row.contract.conId]
    if request.secType:
        # IBKR sometimes adds rows of another type (event contracts to a FOP request).
        found = [pair for pair in found if pair[0].secType == request.secType] or found
    by_con_id: dict[int, list[tuple[Contract, ContractDetails]]] = {}
    for contract, row in found:
        by_con_id.setdefault(contract.conId, []).append((contract, row))
    if not by_con_id:
        raise NotFoundError(not_found_message(spec))
    if len(by_con_id) > 1:
        candidates = [
            contract_to_out(group[0][0], description=group[0][1].longName)
            for group in list(by_con_id.values())[:MAX_LISTED_CANDIDATES]
        ]
        listed = "; ".join(_describe_candidate(candidate) for candidate in candidates)
        more = len(by_con_id) - len(candidates)
        if more:
            listed += f"; and {more} more"
        raise AmbiguousContractError(
            f"{describe_spec(spec)} matches {len(by_con_id)} contracts. Retry with the con_id "
            "of the one you mean, or add fields (primary_exchange, currency, expiry, strike, "
            f"right, trading_class, multiplier) to narrow it down. Candidates: {listed}",
            candidates,
        )
    group = next(iter(by_con_id.values()))
    contract, chosen = next((pair for pair in group if pair[0].exchange == spec.exchange), group[0])
    if spec.exchange == "SMART" and contract.exchange != "SMART":
        valid = {code.strip() for code in (chosen.validExchanges or "").split(",")}
        if request.exchange == "SMART" or "SMART" in valid:
            contract.exchange = "SMART"
    contract.includeExpired = spec.include_expired
    return contract, chosen
