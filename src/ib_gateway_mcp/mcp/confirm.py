"""Human confirmation of live orders through MCP elicitation.

Why a resolver, and not a call inside the service
--------------------------------------------------
The SDK's resolver pattern fills a tool parameter annotated
``Annotated[T, Resolve(fn)]`` by running ``fn`` *before* the tool body, and the parameter
is left out of the tool's input schema, so the model cannot supply it. When ``fn``
returns an ``Elicit``, the SDK asks the client's user:

* on protocol 2026-07-28 and later it returns an ``InputRequiredResult``; the client
  shows the form, then retries the call, and the resolver runs again with the answer;
* on earlier protocols it sends a standalone ``elicitation/create`` request mid-call.

Either way the tool body only runs after the human has answered, so the question cannot
be asked from inside a service call; the confirmation is settled first and its outcome
handed to the body.

How the orders tools use it
---------------------------
1. The service exposes a side-effect-free way to describe what a token would do,
   returning a :class:`~ib_gateway_mcp.models.common.ConfirmationRequest` without
   consuming the token (``PreviewStore.peek`` exists for this). It runs on every
   elicitation round, so the same token must always produce the same text; the SDK
   re-asks when the rendered question changes. It should also raise when the action
   would be refused anyway (the orders tools run ``OrdersService.precheck``: breaker,
   limits, rate limit), so no human approves something the server then refuses.
2. The tool module builds a resolver with :func:`live_confirmation` and declares the
   parameter. The tool must have a ``token: str`` argument; the resolver reads it by name::

       async def _describe(gw: Gateway, token: str) -> ConfirmationRequest:
           gw.orders.precheck(token)  # refuse now what submit would refuse
           return gw.orders.confirmation_request(token)  # a peek: synchronous

       confirm_submit = live_confirmation(_describe, "submit_order")

       @ib_tool("orders", Tier.WRITE, "Submit a previewed order")
       async def submit_order(
           ctx: ToolContext,
           token: str,
           confirmation: Annotated[ConfirmationOutcome, Resolve(confirm_submit)],
       ) -> OrderResult:
           \"\"\"...\"\"\"
           confirmed = require_confirmation(confirmation) is ConfirmationStatus.CONFIRMED
           return await gateway_from(ctx).orders.submit(token, human_confirmed=confirmed)

3. The resolver decides, in this order:

   * trading disabled (``ConnectionManager.require_trading`` fails): the error is
     raised as a tool error; nothing is asked;
   * paper account, or ``IBKR_MCP_LIVE_CONFIRM=false``: returns
     :class:`ConfirmationSkipped` with ``NOT_REQUIRED``; nothing is asked;
   * live, and the client did not declare form elicitation: returns
     :class:`ConfirmationSkipped` with ``UNAVAILABLE`` (the body then refuses);
   * live, and the client can elicit: returns ``Elicit(message, LiveConfirmation)``.

4. :func:`require_confirmation` turns the outcome into a :class:`ConfirmationStatus`
   (``CONFIRMED`` or ``NOT_REQUIRED``) or raises
   :class:`~ib_gateway_mcp.errors.ConfirmationUnavailableError` (fail closed) or
   :class:`~ib_gateway_mcp.errors.ConfirmationDeclinedError` (declined, cancelled, or
   accepted with the box unticked).

5. The service stays the final gate: a live submit with ``live_confirm`` on and
   ``human_confirmed=False`` must be refused there too, so a wiring mistake in a tool
   cannot skip the human.

Other confirmations reuse the same pieces with their own wording: the FA replacement
passes its own ``message``, ``form`` and refusal texts to :func:`plan_confirmation` and
:func:`require_confirmation`; the circuit-breaker reset asks on paper accounts too, so it
calls :func:`ask_human` directly.

Errors from ``describe`` (unknown or expired token, not connected) are raised as tool
errors with their message, like errors from a tool body. ``live_confirm`` is read from
the gateway's settings, which govern every safety decision. ``tests/mcp/test_confirm.py``
drives a demo tool wired this way through both protocol eras.

This module has no ``from __future__ import annotations`` on purpose: the SDK inspects
the resolver's annotations at registration.
"""

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Literal, overload

from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    Context,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
)
from pydantic import BaseModel, Field

from ib_gateway_mcp.errors import ConfirmationDeclinedError, ConfirmationUnavailableError
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.mcp.context import gateway_from, require_trading
from ib_gateway_mcp.mcp.registry import tool_errors
from ib_gateway_mcp.models.common import ConfirmationRequest

__all__ = [
    "DECLINED",
    "REQUESTER_TEXT_NOTE",
    "UNAVAILABLE",
    "ConfirmationOutcome",
    "ConfirmationSkipped",
    "ConfirmationStatus",
    "Describe",
    "LiveConfirmation",
    "ask_human",
    "client_can_elicit",
    "live_confirmation",
    "one_line",
    "plan_confirmation",
    "render_message",
    "require_confirmation",
]


class ConfirmationStatus(StrEnum):
    """How a destructive call was cleared to run."""

    CONFIRMED = "confirmed"
    """A human approved it through elicitation."""
    NOT_REQUIRED = "not_required"
    """Paper account, or live confirmation is switched off."""
    UNAVAILABLE = "unavailable"
    """Confirmation is required but the client cannot elicit; the call must be refused."""


class LiveConfirmation(BaseModel):
    """The form shown to the human. Unticked by default: approval must be deliberate."""

    confirm: bool = Field(
        default=False,
        title="Approve this action on the live account",
        description=(
            "Tick to go ahead on the real-money account. Leave unticked or decline to stop it."
        ),
    )


class ConfirmationSkipped(BaseModel):
    """Resolver result when no question was asked."""

    status: Literal[ConfirmationStatus.NOT_REQUIRED, ConfirmationStatus.UNAVAILABLE]
    reason: str


ConfirmationOutcome = ElicitationResult[LiveConfirmation]
"""Annotate the tool parameter with this (inside ``Annotated[..., Resolve(...)]``).

At runtime an accepted outcome's ``data`` is either a :class:`LiveConfirmation` (the
human answered) or a :class:`ConfirmationSkipped` (nothing was asked); pass it to
:func:`require_confirmation` rather than inspecting it.
"""

Describe = Callable[[Gateway, str], Awaitable[ConfirmationRequest]]
"""Describes, without side effects, what the action behind a token would do."""


def client_can_elicit(ctx: Context[Any, Any]) -> bool:
    """True when the client declared form elicitation (a bare ``elicitation: {}`` counts)."""
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    if elicitation is None:
        return False
    return elicitation.form is not None or elicitation.url is None


REQUESTER_TEXT_NOTE = (
    "Text in double quotes was written by the requester (the AI model), not by this server or IBKR."
)
"""Closes every confirmation prompt: model-supplied values (a model code, a tier or FA
group name, a reason) are always quoted, so a human can tell them from the server's own
words."""


def one_line(text: str) -> str:
    """A server-built line, with any line break or invisible character made visible."""
    return "".join(char if char.isprintable() else f"\\u{ord(char):04x}" for char in text)


def render_message(request: ConfirmationRequest) -> str:
    """The text shown to the human. Deterministic for a given request.

    Worded for any action on a live account (an order, a modification, a cancel, an
    exercise); ``request.action`` says which. Each line of the request stays one line.
    """
    lines = [
        f"LIVE ACCOUNT {one_line(request.account)} (real money). Approve this action:",
        "",
        one_line(request.action),
        *(one_line(line) for line in request.details),
        "",
        REQUESTER_TEXT_NOTE,
        "Tick the box to go ahead. Decline or cancel to stop it; nothing is sent then.",
    ]
    return "\n".join(lines)


def ask_human[F: LiveConfirmation](
    ctx: Context[Any, Any], message: str, form: type[F]
) -> Elicit[F] | ConfirmationSkipped:
    """Ask ``message`` with ``form``, or skip as UNAVAILABLE when the client cannot ask."""
    if not client_can_elicit(ctx):
        return ConfirmationSkipped(
            status=ConfirmationStatus.UNAVAILABLE,
            reason="the MCP client does not support elicitation",
        )
    return Elicit(message, form)


@overload
def plan_confirmation(
    ctx: Context[Any, Any], gateway: Gateway, request: ConfirmationRequest
) -> Elicit[LiveConfirmation] | ConfirmationSkipped: ...


@overload
def plan_confirmation[F: LiveConfirmation](
    ctx: Context[Any, Any],
    gateway: Gateway,
    request: ConfirmationRequest,
    *,
    message: str | None = None,
    form: type[F],
) -> Elicit[F] | ConfirmationSkipped: ...


def plan_confirmation(
    ctx: Context[Any, Any],
    gateway: Gateway,
    request: ConfirmationRequest,
    *,
    message: str | None = None,
    form: type[LiveConfirmation] = LiveConfirmation,
) -> Elicit[Any] | ConfirmationSkipped:
    """Decide whether to ask the human, and what to ask (see the module docstring).

    Args:
        message: The question; :func:`render_message` of ``request`` by default.
        form: The form to show (a :class:`LiveConfirmation` with its own wording).
    """
    if request.is_paper:
        return ConfirmationSkipped(status=ConfirmationStatus.NOT_REQUIRED, reason="paper account")
    if not gateway.settings.live_confirm:
        return ConfirmationSkipped(
            status=ConfirmationStatus.NOT_REQUIRED, reason="IBKR_MCP_LIVE_CONFIRM is off"
        )
    return ask_human(ctx, message if message is not None else render_message(request), form)


def live_confirmation(
    describe: Describe, tool: str = "submit_order"
) -> Callable[[Context[Any, Any], str], Awaitable[Elicit[LiveConfirmation] | ConfirmationSkipped]]:
    """Build the resolver for a tool that acts on a preview ``token``.

    Args:
        describe: Returns the :class:`ConfirmationRequest` for a token without consuming it.
        tool: The tool's name, for the audit entry of a trading-gate refusal.

    Returns:
        A resolver to use as ``Resolve(resolver)`` on a :data:`ConfirmationOutcome` parameter.
    """

    async def resolve_live_confirmation(
        ctx: Context[Any, Any], token: str
    ) -> Elicit[LiveConfirmation] | ConfirmationSkipped:
        with tool_errors():
            gateway = gateway_from(ctx)
            # Refuse before asking anyone when trading is off (the tool body would anyway).
            require_trading(gateway, tool)
            request = await describe(gateway, token)
            return plan_confirmation(ctx, gateway, request)

    return resolve_live_confirmation


DECLINED = "The live action was not confirmed ({how}); nothing was sent."
"""Default refusal when the human said no; ``{how}`` says how."""
UNAVAILABLE = (
    "This action goes to a live account and needs a human confirmation, but this MCP client "
    "cannot show one (no elicitation support). Nothing was sent. Use a client with "
    "elicitation (e.g. Claude Code), or set IBKR_MCP_LIVE_CONFIRM=false to accept the risk."
)
"""Default refusal when a human must confirm but the client cannot ask."""


def require_confirmation(
    outcome: BaseModel,
    *,
    declined: str = DECLINED,
    unavailable: str = UNAVAILABLE,
    form: type[LiveConfirmation] = LiveConfirmation,
) -> ConfirmationStatus:
    """Turn a resolved :data:`ConfirmationOutcome` into a go/no-go.

    Args:
        outcome: What the resolver's question produced.
        declined: The refusal when the human declined, cancelled or left the box
            unticked; ``{how}`` is replaced with which of those happened.
        unavailable: The refusal when approval was needed but the client cannot ask.
        form: The form the resolver showed; only its ticked box approves.

    Returns:
        ``CONFIRMED`` when a human approved, ``NOT_REQUIRED`` when no approval was needed.

    Raises:
        ConfirmationUnavailableError: Approval was needed but the client cannot ask.
        ConfirmationDeclinedError: The human declined, cancelled, or left the box unticked.
    """
    if isinstance(outcome, DeclinedElicitation | CancelledElicitation):
        raise ConfirmationDeclinedError(declined.format(how=f"the human chose {outcome.action}"))
    if not isinstance(outcome, AcceptedElicitation):
        raise TypeError(f"unexpected confirmation outcome {type(outcome).__name__}")
    data = outcome.data
    if isinstance(data, ConfirmationSkipped):
        if data.status is ConfirmationStatus.UNAVAILABLE:
            raise ConfirmationUnavailableError(unavailable)
        return ConfirmationStatus.NOT_REQUIRED
    if isinstance(data, form) and data.confirm:
        return ConfirmationStatus.CONFIRMED
    raise ConfirmationDeclinedError(declined.format(how="the box was left unticked"))
