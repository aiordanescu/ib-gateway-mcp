"""Financial-advisor tools: FA configuration (read and replace), soft dollar tiers, family codes.

Tools here follow :mod:`ib_gateway_mcp.mcp.tools.ops` and use the parameter types in
:mod:`ib_gateway_mcp.mcp.params`. Replacing the FA configuration is a two-step flow of
its own: ``preview_replace_fa_config`` returns a token and
``apply_fa_config(token)`` applies it, asking a human first on live logins. It does not
go through ``submit_order``, so the advisor toolset works without the orders toolset.

The question follows :mod:`ib_gateway_mcp.mcp.confirm` (``plan_confirmation`` decides
whether to ask, ``require_confirmation`` turns the answer into a go/no-go) but is worded
for an FA change: that module's own text speaks of live orders and real money being sent.
"""

from typing import Annotated, Any

from mcp.server.mcpserver import Context, Elicit, ElicitationResult, Resolve
from pydantic import BaseModel, Field

from ib_gateway_mcp.mcp.confirm import (
    REQUESTER_TEXT_NOTE,
    ConfirmationSkipped,
    ConfirmationStatus,
    LiveConfirmation,
    one_line,
    plan_confirmation,
    require_confirmation,
)
from ib_gateway_mcp.mcp.context import ToolContext, gateway_from, require_trading
from ib_gateway_mcp.mcp.registry import Tier, ib_tool, tool_errors
from ib_gateway_mcp.models.advisor import (
    FaApplyResult,
    FaConfig,
    FaDataType,
    FamilyCodeList,
    FaReplaceDataType,
    FaReplacePreview,
    SoftDollarTierList,
)
from ib_gateway_mcp.models.common import ConfirmationRequest
from ib_gateway_mcp.services.advisor import (
    FA_XML_CHARS_DEFAULT,
    FA_XML_CHARS_MAX,
    MAX_FA_XML_CHARS,
)

# --- live confirmation of an FA replacement -----------------------------------------------


class FaApplyConfirmation(LiveConfirmation):
    """The form shown to the human. Unticked by default: approval must be deliberate.

    A :class:`~ib_gateway_mcp.mcp.confirm.LiveConfirmation`, so
    :func:`~ib_gateway_mcp.mcp.confirm.require_confirmation` accepts it.
    """

    confirm: bool = Field(
        default=False,
        title="Replace the live FA group configuration",
        description=(
            "Tick to replace the FA allocation groups of the live login. Leave unticked or "
            "decline to keep the current groups."
        ),
    )


FaApplyOutcome = ElicitationResult[FaApplyConfirmation]
"""An accepted outcome's ``data`` is a :class:`FaApplyConfirmation` or, when nothing was
asked, a :class:`~ib_gateway_mcp.mcp.confirm.ConfirmationSkipped`."""


def fa_confirmation_message(request: ConfirmationRequest) -> str:
    """The question shown to the human. Deterministic for a given token."""
    lines = [
        f"LIVE FA CONFIGURATION CHANGE for account(s) {request.account}: orders placed for "
        "these groups are allocated across real-money accounts.",
        "",
        request.action,
        *request.details,
        "",
        REQUESTER_TEXT_NOTE,
        "Tick the box to apply it. Decline or cancel to keep the current configuration.",
    ]
    return "\n".join(one_line(line) for line in lines)


async def _confirm_fa_apply(
    ctx: Context[Any, Any], token: str
) -> Elicit[FaApplyConfirmation] | ConfirmationSkipped:
    """Ask the human on live logins (see :func:`~ib_gateway_mcp.mcp.confirm.plan_confirmation`)."""
    with tool_errors():
        gateway = gateway_from(ctx)
        # Refuse before asking anyone when writes are off (the tool body would anyway).
        require_trading(gateway, "apply_fa_config")
        request = gateway.advisor.confirmation_request(token)  # peeks, never consumes
        return plan_confirmation(
            ctx,
            gateway,
            request,
            message=fa_confirmation_message(request),
            form=FaApplyConfirmation,
        )


def fa_apply_confirmed(outcome: BaseModel) -> bool:
    """True when a human approved, False when no approval was needed (paper, or off).

    Raises:
        ConfirmationUnavailableError: Approval was needed but the client cannot ask.
        ConfirmationDeclinedError: The human declined, cancelled, or left the box unticked.
    """
    status = require_confirmation(
        outcome,
        declined="The FA configuration change was not confirmed ({how}); nothing was changed.",
        unavailable=(
            "Replacing the FA configuration of a live login needs a human's confirmation, "
            "but this MCP client cannot show one (no elicitation support). Nothing was "
            "changed. Use a client with elicitation, or set IBKR_MCP_LIVE_CONFIRM=false to "
            "accept the risk."
        ),
        form=FaApplyConfirmation,
    )
    return status is ConfirmationStatus.CONFIRMED


# --- tools --------------------------------------------------------------------------------


@ib_tool("advisor", Tier.READ, "FA configuration")
async def get_fa_config(
    ctx: ToolContext,
    data_type: Annotated[
        FaDataType,
        Field(description="groups (FA allocation groups) or aliases (account display names)."),
    ] = "groups",
    include_xml: Annotated[
        bool,
        Field(
            description=(
                "Also return IBKR's raw XML, e.g. as the starting point for "
                "preview_replace_fa_config. Needs every account of the login in the allowlist."
            )
        ),
    ] = False,
    max_chars: Annotated[
        int,
        Field(
            ge=1,
            le=FA_XML_CHARS_MAX,
            description=(
                f"Longest raw XML to return, in characters (at most {FA_XML_CHARS_MAX:,}); "
                "longer XML is cut and truncated is true."
            ),
        ),
    ] = FA_XML_CHARS_DEFAULT,
) -> FaConfig:
    """Read the financial-advisor (FA) configuration: allocation groups or account aliases.

    Groups list each group's name, default allocation method (AvailableEquity, Equal,
    NetLiq, ContractsOrShares, Ratio, Percent, MonetaryAmount) and member accounts with
    their allocation values. Orders placed for a group are allocated across its members.
    Only accounts in this server's allowlist are named; other members are counted in
    `other_accounts`. IBKR merged the old "profiles" into groups.

    Works only on FA master and IBroker logins.

    Errors: invalid_request (IBKR refused: not an FA login), request_timeout (IBKR sent
    nothing within its 4 s window: not an FA login), not_found (an FA login without
    groups or aliases).
    """
    return await gateway_from(ctx).advisor.fa_config(
        data_type, include_xml=include_xml, max_chars=max_chars
    )


@ib_tool("advisor", Tier.READ, "Preview an FA configuration change")
async def preview_replace_fa_config(
    ctx: ToolContext,
    xml: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_FA_XML_CHARS,
            description=(
                "The complete new FA groups document: <ListOfGroups> with one <Group> per "
                "group (<name>, <defaultMethod>, <ListOfAccts> of <Account><acct>ID</acct>"
                "<amount>N</amount></Account>). It replaces every group; groups left out "
                "are deleted. No DOCTYPE."
            ),
        ),
    ],
    data_type: Annotated[
        FaReplaceDataType, Field(description="What to replace; only groups is supported.")
    ] = "groups",
) -> FaReplacePreview:
    """Check a new FA group configuration and show how it differs from the current one.

    Nothing changes at IBKR. The XML must be well-formed, have a <ListOfGroups> root,
    and name each group once with no account listed twice in a group. The result lists
    added, removed and changed groups, warnings (unknown allocation methods, empty
    groups, accounts this login does not manage) and a `token` for apply_fa_config,
    valid for a couple of minutes and usable once. Get the current XML from
    get_fa_config(include_xml=true) and edit it.

    Replacing covers every account of the login, so this needs all of them in the
    server's allowlist (account_not_allowed otherwise). Works only on FA master and
    IBroker logins, and only while writes are allowed (the trading-gate errors
    otherwise). Group names, methods and account ids must be one line of plain text:
    they are shown to the human who confirms a live change.
    """
    return await gateway_from(ctx).advisor.preview_replace_fa_config(xml, data_type)


@ib_tool("advisor", Tier.WRITE, "Apply an FA configuration change")
async def apply_fa_config(
    ctx: ToolContext,
    token: Annotated[str, Field(description="The token preview_replace_fa_config returned.")],
    confirmation: Annotated[FaApplyOutcome, Resolve(_confirm_fa_apply)],
) -> FaApplyResult:
    """Replace the FA group configuration with a previewed one. Destructive.

    Applies exactly what preview_replace_fa_config checked; the token works once. On a
    live login the human is asked to confirm first (clients without elicitation are
    refused unless the server allows live changes without confirmation). If the
    configuration at IBKR changed since the preview, nothing is applied: preview again.
    On request_timeout IBKR may still have applied it; check get_fa_config before
    retrying. Every attempt is audited, including the previous XML so a human can
    restore it. Returns IBKR's confirmation text and the applied changes.
    """
    confirmed = fa_apply_confirmed(confirmation)
    return await gateway_from(ctx).advisor.apply_fa_config(token, human_confirmed=confirmed)


@ib_tool("advisor", Tier.READ, "Soft dollar tiers")
async def get_soft_dollar_tiers(ctx: ToolContext) -> SoftDollarTierList:
    """List the soft dollar tiers orders can reference (name, value, display name).

    Soft dollar arrangements direct part of the commission to research; they are set up
    with IBKR for advisors and institutions. To use one, pass it as soft_dollar_tier
    {name, value} in preview_order, preview_bracket_order, preview_oca_group or
    preview_combo_order. Logins without any fail with not_found.
    """
    return await gateway_from(ctx).advisor.soft_dollar_tiers()


@ib_tool("advisor", Tier.READ, "Family codes")
async def get_family_codes(ctx: ToolContext) -> FamilyCodeList:
    """List the family codes IBKR assigned to the accounts (linked-account grouping).

    Accounts that share a family code are linked (e.g. an advisor's client accounts).
    Only accounts in this server's allowlist are named; others are counted in
    `other_accounts`. Errors: not_found when IBKR reports none for them.
    """
    return await gateway_from(ctx).advisor.family_codes()
