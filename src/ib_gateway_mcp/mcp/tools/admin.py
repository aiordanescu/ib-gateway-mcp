"""Admin tools: the order circuit breaker, display groups and the gateway's server log level.

Tools here follow :mod:`ib_gateway_mcp.mcp.tools.ops` and use the parameter types in
:mod:`ib_gateway_mcp.mcp.params`.

``reset_circuit_breaker`` always asks a human, on paper accounts too: the model whose
orders tripped the breaker must not be able to clear it. Its resolver follows the
pattern of :mod:`ib_gateway_mcp.mcp.confirm` (the question is settled before the body
runs and the model cannot supply the answer), but asks whenever there is something to
reset (:func:`~ib_gateway_mcp.mcp.confirm.ask_human`) and fails closed when the client
cannot elicit.
"""

from typing import Annotated, Any

from mcp.server.mcpserver import Context, Elicit, ElicitationResult, Resolve
from pydantic import BaseModel, Field

from ib_gateway_mcp.mcp.confirm import (
    REQUESTER_TEXT_NOTE,
    ConfirmationSkipped,
    ConfirmationStatus,
    LiveConfirmation,
    ask_human,
    one_line,
    require_confirmation,
)
from ib_gateway_mcp.mcp.context import ToolContext, gateway_from, require_trading
from ib_gateway_mcp.mcp.params import ContractArg, SubscriptionIdArg
from ib_gateway_mcp.mcp.registry import Tier, ib_tool, tool_errors
from ib_gateway_mcp.models.admin import (
    CircuitBreakerReset,
    CircuitBreakerStatus,
    DisplayGroupList,
    DisplayGroupUpdated,
    ServerLogLevel,
    ServerLogLevelOut,
)
from ib_gateway_mcp.models.common import SubscriptionOut, quoted

# --- circuit breaker confirmation ---------------------------------------------------------


class ResetConfirmation(LiveConfirmation):
    """The form shown to the human. Unticked by default: approval must be deliberate."""

    confirm: bool = Field(
        default=False,
        title="Resume order submission",
        description=(
            "Tick to close the circuit breaker so orders can be submitted again. Leave "
            "unticked or decline to keep trading halted."
        ),
    )


ResetOutcome = ElicitationResult[ResetConfirmation]
"""An accepted outcome's ``data`` is a :class:`ResetConfirmation` or, when nothing was
asked, a :class:`~ib_gateway_mcp.mcp.confirm.ConfirmationSkipped`."""


def reset_message(status: CircuitBreakerStatus, reason: str) -> str:
    """The question shown to the human. Deterministic for a given state and reason."""
    if status.is_open and status.opened_at is not None:
        state = (
            f"It opened at {status.opened_at.isoformat()} after {status.threshold} "
            "consecutive order rejections; order submission is halted."
        )
    else:
        opens = (
            f"it opens at {status.threshold}"
            if status.threshold is not None
            else "it is disabled and never opens"
        )
        state = (
            f"It is closed with {status.consecutive_rejections} consecutive order "
            f"rejection(s) counted ({opens}); the count goes back to 0."
        )
    lines = ["RESET THE ORDER CIRCUIT BREAKER", "", state]
    if status.last_reason:
        lines.append(one_line(f"Last rejection: {status.last_reason}"))
    lines += [
        f"Reason given by the requester: {quoted(' '.join(reason.split()))}",
        "",
        REQUESTER_TEXT_NOTE,
        "Tick the box only after looking into the rejections. Decline or cancel to keep "
        "trading halted.",
    ]
    return "\n".join(lines)


async def _confirm_reset(
    ctx: Context[Any, Any], reason: str
) -> Elicit[ResetConfirmation] | ConfirmationSkipped:
    """Ask the human whenever a reset would change something, paper accounts included."""
    with tool_errors():
        gateway = gateway_from(ctx)
        require_trading(gateway, "reset_circuit_breaker")
        status = gateway.admin.circuit_breaker_status()
        if not status.needs_reset:
            return ConfirmationSkipped(
                status=ConfirmationStatus.NOT_REQUIRED, reason="nothing to reset"
            )
        return ask_human(ctx, reset_message(status, reason), ResetConfirmation)


def reset_confirmed(outcome: BaseModel) -> bool:
    """Turn the resolved outcome into "a human confirmed" (True) or "nothing to reset" (False).

    Raises:
        ConfirmationUnavailableError: There is something to reset and the client cannot ask.
        ConfirmationDeclinedError: The human declined, cancelled, or left the box unticked.
    """
    status = require_confirmation(
        outcome,
        declined="The circuit breaker reset was not confirmed ({how}); trading stays halted.",
        unavailable=(
            "Resetting the circuit breaker needs a human's confirmation, but this MCP client "
            "cannot show one (no elicitation support). Nothing was changed. Tell the user: "
            "they can reset it from a client with elicitation."
        ),
        form=ResetConfirmation,
    )
    return status is ConfirmationStatus.CONFIRMED


@ib_tool("admin", Tier.ADMIN, "Reset the order circuit breaker")
async def reset_circuit_breaker(
    ctx: ToolContext,
    reason: Annotated[
        str,
        Field(
            min_length=3,
            max_length=500,
            description="Why order submission may resume; shown to the human and audited.",
        ),
    ],
    confirmation: Annotated[ResetOutcome, Resolve(_confirm_reset)],
) -> CircuitBreakerReset:
    """Re-arm order submission after repeated IBKR rejections tripped the circuit breaker.

    Always asks the human to confirm through the client (paper accounts too) and fails
    with confirmation_unavailable when the client cannot ask: the breaker exists so a
    model cannot keep sending rejected orders. Only call it when the user asked for it.
    When the breaker is closed with no rejections counted, nothing changes and nobody is
    asked. The reset and its reason are audited; `before` shows the breaker's state.
    """
    confirmed = reset_confirmed(confirmation)
    return gateway_from(ctx).admin.reset_circuit_breaker(reason, human_confirmed=confirmed)


# --- display groups -----------------------------------------------------------------------


@ib_tool("admin", Tier.READ, "Display groups")
async def list_display_groups(ctx: ToolContext) -> DisplayGroupList:
    """List the TWS display groups (the colour-linked window groups) by id.

    Display groups are a TWS window feature. IB Gateway has no windows, so it normally
    reports none (not_found) or does not answer (request_timeout).
    """
    return await gateway_from(ctx).admin.list_display_groups()


@ib_tool("admin", Tier.READ, "Follow a display group")
async def subscribe_display_group(
    ctx: ToolContext,
    group_id: Annotated[
        int, Field(ge=1, description="Display group id, from list_display_groups (TWS: 1-7).")
    ],
) -> SubscriptionOut:
    """Follow which contract a TWS display group shows (read with get_subscription_data).

    Returns a subscription handle. get_subscription_data returns `current` (the latest
    selection: con_id and exchange, 'none' or 'combo'), recent `updates` and the last
    IBKR `error`. update_display_group changes the contract. Subscribing to the same
    group again returns the same handle; stop it with unsubscribe. Display groups are a
    TWS window feature; on IB Gateway the stream normally stays empty.
    """
    return await gateway_from(ctx).admin.subscribe_display_group(group_id)


@ib_tool("admin", Tier.ADMIN, "Set a display group's contract", idempotent=True)
async def update_display_group(
    ctx: ToolContext,
    subscription_id: SubscriptionIdArg,
    contract: ContractArg,
) -> DisplayGroupUpdated:
    """Make a subscribed TWS display group show a contract, so linked TWS windows follow.

    The contract is resolved to its con_id first (combos are not supported). IBKR sends
    no reply of its own; the subscription's data shows the new selection. Audited.
    Display groups are a TWS window feature; IB Gateway ignores this.
    """
    return await gateway_from(ctx).admin.update_display_group(subscription_id, contract)


# --- server log level ---------------------------------------------------------------------


@ib_tool("admin", Tier.ADMIN, "Set the gateway API log level", idempotent=True)
async def set_server_log_level(
    ctx: ToolContext,
    level: Annotated[
        ServerLogLevel,
        Field(
            description=(
                "system (least), error, warning, information or detail (most, large logs)."
            )
        ),
    ],
) -> ServerLogLevelOut:
    """Set how much the gateway writes to its own API log (not this server's log).

    Useful while diagnosing API problems with IBKR support; 'detail' logs every message
    and grows the gateway's log files quickly, so set it back to 'error' afterwards.
    IBKR sends no acknowledgement. Audited.
    """
    return gateway_from(ctx).admin.set_server_log_level(level)
