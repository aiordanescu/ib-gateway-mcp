"""Shared tool parameter types, so every toolset describes the same inputs the same way.

The SDK builds each tool's input schema from its signature, and a ``Field(description=...)``
inside ``Annotated`` becomes the parameter's description in that schema. Use these
aliases for the parameters many tools share; give other parameters their own
``Annotated[..., Field(description=...)]``::

    @ib_tool("account", Tier.READ, "Executions")
    async def get_executions(
        ctx: ToolContext,
        account: AccountArg = None,
        limit: LimitArg = None,
    ) -> ExecutionList:
        ...

Services resolve ``account`` with ``AccountScope.resolve`` and ``limit`` with
``_util.clamp_limit(limit, default=..., maximum=...)``; list results derive from
:class:`~ib_gateway_mcp.models.common.Truncatable`.

No ``from __future__ import annotations`` here: these aliases are evaluated by the SDK.
"""

from typing import Annotated

from pydantic import Field

from ib_gateway_mcp.models.common import BarSize, ContractSpec, LiveBarSize, WhatToShow

__all__ = [
    "SUBSCRIPTION_ID_HELP",
    "AccountArg",
    "BarSizeArg",
    "ContractArg",
    "LimitArg",
    "LiveBarSizeArg",
    "SubscriptionIdArg",
    "UseRthArg",
    "WhatToShowArg",
]

AccountArg = Annotated[
    str | None,
    Field(
        description=(
            "IBKR account id, e.g. DU1234567. Omit to use the default account; "
            "list_accounts shows which accounts are allowed."
        )
    ),
]
"""An optional account id; declare it as ``account: AccountArg = None``."""

LimitArg = Annotated[
    int | None,
    Field(
        ge=1,
        description=(
            "Maximum number of items to return. Omit for the tool's default; larger values "
            "are capped. The result's truncated flag says whether more were available."
        ),
    ),
]
"""An optional result limit; declare it as ``limit: LimitArg = None``."""

ContractArg = Annotated[
    ContractSpec,
    Field(
        description=(
            "The instrument. A con_id alone is unambiguous; otherwise give symbol and "
            "sec_type, plus expiry, strike and right for options."
        )
    ),
]
"""A required instrument; declare it as ``contract: ContractArg``."""

UseRthArg = Annotated[
    bool,
    Field(
        description=(
            "True: regular trading hours only. False: include pre-market, after-hours and "
            "overnight data."
        )
    ),
]
"""Regular trading hours only, or every session; declare it with the tool's default."""

WhatToShowArg = Annotated[
    WhatToShow,
    Field(
        description=(
            "Data the values are built from. TRADES (not for forex), MIDPOINT, BID, ASK, "
            "BID_ASK (counts double for pacing), ADJUSTED_LAST (split/dividend adjusted; "
            "end must be empty), HISTORICAL_VOLATILITY and OPTION_IMPLIED_VOLATILITY "
            "(stocks, indexes), REBATE_RATE and FEE_RATE (stock loan), YIELD_BID, "
            "YIELD_ASK, YIELD_BID_ASK, YIELD_LAST (bonds), AGGTRADES (crypto)."
        )
    ),
]
"""What bars (or ticks) are built from, for historical and live-updating bars."""

BarSizeArg = Annotated[
    BarSize,
    Field(description="Length of one bar, e.g. '5 secs', '1 min', '15 mins', '1 hour', '1 day'."),
]
"""A bar size, spelled as IBKR spells it."""

LiveBarSizeArg = Annotated[
    LiveBarSize,
    Field(description="Length of one bar, 5 secs or more, e.g. '5 secs', '1 min', '1 hour'."),
]
"""A bar size for live-updating bars (no 1 secs)."""

SUBSCRIPTION_ID_HELP = "The id a subscribe_* tool returned (see list_subscriptions)."
"""Description of a ``subscription_id`` parameter (also for an optional one)."""

SubscriptionIdArg = Annotated[str, Field(description=SUBSCRIPTION_ID_HELP)]
"""A subscription handle; declare it as ``subscription_id: SubscriptionIdArg``."""
