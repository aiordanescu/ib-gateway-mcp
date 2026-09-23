"""Order tools: preview (what-if) and token-bound submit, modify, cancel, exercise, status.

Orders are two-step: a ``preview_*`` tool validates the action, checks the order limits,
runs IBKR's what-if check and returns a single-use token; ``submit_order(token)``
executes exactly what was previewed. ``submit_order`` and ``cancel_order`` are
destructive (Tier.WRITE): the registry runs the trading gate (``require_trading``) before
their body, and the service calls it again. Live submits and live cancels go through the
human confirmation in :mod:`ib_gateway_mcp.mcp.confirm`: the resolver first runs the
service's side-effect-free precheck (breaker, limits, rate limit), so nobody is asked to
approve what the server would refuse, then describes the action
(``OrdersService.confirmation_request``, a peek, never a consume;
``cancel_confirmation_request`` for a cancel). The service refuses a live action without
the confirmation. ``reset_circuit_breaker`` is not here: it lives in the admin toolset
and asks a human to confirm. Working orders are listed by ``get_open_orders`` in the
account toolset.

No ``from __future__ import annotations``: the SDK introspects these signatures.
"""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, Elicit, Resolve
from pydantic import AwareDatetime, Field

from ib_gateway_mcp.errors import SafetyError
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.mcp.confirm import (
    ConfirmationOutcome,
    ConfirmationSkipped,
    ConfirmationStatus,
    LiveConfirmation,
    live_confirmation,
    plan_confirmation,
    require_confirmation,
)
from ib_gateway_mcp.mcp.context import ToolContext, gateway_from, require_trading
from ib_gateway_mcp.mcp.params import AccountArg, ContractArg
from ib_gateway_mcp.mcp.registry import Tier, ib_tool, tool_errors
from ib_gateway_mcp.models.common import Action, ConfirmationRequest
from ib_gateway_mcp.models.orders import (
    MODEL_CODE_PATTERN,
    BracketEntryType,
    BracketSpec,
    BracketTimeInForce,
    CancelScope,
    ComboOrderLegSpec,
    ComboOrderType,
    ComboSpec,
    ExerciseAction,
    ExerciseSpec,
    ModifySpec,
    OcaSpec,
    OcaType,
    OrderPreview,
    OrderResult,
    OrderSpec,
    OrderStatusOut,
    SoftDollarTierRef,
    invalid_request_on,
)

TokenArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        description=(
            "The token from a preview_* tool: single-use, valid until its expires_at (the "
            "server's token TTL, 120 s by default); preview again after that."
        ),
    ),
]
OrderIdArg = Annotated[
    int,
    Field(ge=1, description="Order id from preview/submit results or get_open_orders."),
]
QuantityArg = Annotated[
    float, Field(gt=0, allow_inf_nan=False, description="Number of shares or contracts.")
]


async def _describe(gateway: Gateway, token: str) -> ConfirmationRequest:
    """What the human is asked to approve for ``token`` (consumes nothing).

    The precheck raises first when the submit would be refused anyway (circuit breaker,
    order limits, rate limit, live trading off), so no human approves a doomed order.
    """
    gateway.orders.precheck(token)
    return gateway.orders.confirmation_request(token)


confirm_submit = live_confirmation(_describe, "submit_order")


async def confirm_cancel(
    ctx: Context[Any, Any], order_id: int
) -> Elicit[LiveConfirmation] | ConfirmationSkipped:
    """Ask the human before cancelling an order in a live account (paper: never asks)."""
    with tool_errors():
        gateway = gateway_from(ctx)
        require_trading(gateway, "cancel_order")
        request = await gateway.orders.cancel_confirmation_request(order_id)
        return plan_confirmation(ctx, gateway, request)


@ib_tool("orders", Tier.READ, "Preview an order")
async def preview_order(
    ctx: ToolContext,
    order: Annotated[
        OrderSpec,
        Field(
            description=(
                "The order: contract, action, quantity, order_type, the prices its type needs, "
                "tif, and optional attributes (algo, all_or_none, hidden, display_size, "
                "good_after_time, model_code, soft_dollar_tier)."
            )
        ),
    ],
    account: AccountArg = None,
) -> OrderPreview:
    """Check one order without sending it: limits, IBKR what-if, and a token to submit it.

    Nothing is placed. Returns the order as it would be sent, IBKR's what-if (initial and
    maintenance margin change, equity-with-loan change, commission estimate, warning
    text), the estimated notional, and a `token` for submit_order (single-use; valid
    until `expires_at`, 120 s by default, so preview again if the user takes longer).
    Show the user the summary and what-if before submitting.

    Prices by order_type: MKT and MOC none; LMT and LOC limit_price; STP aux_price
    (stop); STP LMT aux_price + limit_price; MIT aux_price (trigger); LIT aux_price +
    limit_price; TRAIL aux_price (trailing amount) or trailing_percent, optional
    trail_stop_price; TRAIL LIMIT as TRAIL but trail_stop_price is required, plus
    limit_price or limit_price_offset; REL optional aux_price (offset) and limit_price
    (cap); MIDPRICE optional limit_price (cap); PEG MID (pegged to the midpoint) optional
    aux_price (offset) and limit_price (cap); PEG MKT (pegged to the market) optional
    aux_price (offset).
    tif: DAY, GTC, IOC, FOK, GTD (needs good_till_date with a time zone), OPG (MKT/LMT at
    the open). good_after_time delays the start. algo (MKT/LMT, SMART only): Adaptive
    (priority Urgent/Normal/Patient), Twap, Vwap, ArrivalPx, PctVol (pct_vol of the
    market's volume), ClosePx (aims at the close). all_or_none, hidden (NASDAQ-routed
    only) and display_size (iceberg, less than quantity) shape the fill. model_code
    trades within an advisor model portfolio of the account; soft_dollar_tier {name,
    value} comes from get_soft_dollar_tiers. Not supported: order conditions, FA group
    allocation, cash quantity, PEG BEST and other exotic order types.
    Combos use preview_combo_order. Prices must sit on the contract's price increments
    (e.g. 0.01 for US stocks above 1.00); others are refused with invalid_request naming
    the nearest valid prices.

    Errors: invalid_request (e.g. a price off the tick grid), order_limit (server limits
    on symbols, sec types, quantity, notional; a notional check needs a price, so market
    orders may need market data), not_found or ambiguous_contract, ib_api_error (IBKR
    rejected the what-if, e.g. 201 with the reason; 321 means the gateway API is
    read-only), account_not_allowed, live_trading_disabled or configuration_error when
    trading is off.
    """
    return await gateway_from(ctx).orders.preview_order(order, account=account)


@ib_tool("orders", Tier.READ, "Preview a bracket order")
async def preview_bracket_order(
    ctx: ToolContext,
    *,
    contract: ContractArg,
    action: Annotated[Action, Field(description="BUY or SELL for the entry.")],
    quantity: QuantityArg,
    take_profit_price: Annotated[
        float,
        Field(gt=0, allow_inf_nan=False, description="Limit price of the take-profit order."),
    ],
    stop_loss_price: Annotated[
        float,
        Field(gt=0, allow_inf_nan=False, description="Stop price of the stop-loss order."),
    ],
    entry_type: Annotated[
        BracketEntryType, Field(description="Entry order type: LMT, MKT, STP or STP LMT.")
    ] = "LMT",
    entry_price: Annotated[
        float | None,
        Field(
            gt=0,
            allow_inf_nan=False,
            description="Entry limit price (LMT, STP LMT) or stop price (STP).",
        ),
    ] = None,
    entry_stop_price: Annotated[
        float | None,
        Field(gt=0, allow_inf_nan=False, description="Stop (trigger) price of an STP LMT entry."),
    ] = None,
    tif: Annotated[
        BracketTimeInForce, Field(description="DAY, GTC or GTD for all three orders.")
    ] = "DAY",
    good_till_date: Annotated[
        AwareDatetime | None, Field(description="Expiry for tif GTD (ISO 8601 with time zone).")
    ] = None,
    outside_rth: Annotated[
        bool, Field(description="Allow fills outside regular trading hours.")
    ] = False,
    model_code: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=64,
            pattern=MODEL_CODE_PATTERN,
            description=(
                "Advisor model portfolio to trade within the account (FA logins). Letters, "
                "digits, space, dot, underscore and hyphen."
            ),
        ),
    ] = None,
    soft_dollar_tier: Annotated[
        SoftDollarTierRef | None,
        Field(description="Soft dollar tier {name, value} from get_soft_dollar_tiers."),
    ] = None,
    account: AccountArg = None,
) -> OrderPreview:
    """Check a bracket: an entry plus a take-profit limit and a stop-loss stop, as one unit.

    The exits only work once the entry fills; when one exit fills IBKR cancels the other.
    For a BUY: stop_loss_price < entry_price < take_profit_price (SELL: the reverse).
    All prices must sit on the contract's price increments (tick size). The what-if
    covers the entry (IBKR cannot what-if the dependent exits); the order limits apply to
    all three orders and count 3 against the order rate limit. Nothing is placed:
    submit_order(token) sends the three orders, and only the last one transmits, so they
    go live together. If IBKR rejects any of the three, the server cancels the rest (no
    entry is left without both exits) and says so in `messages`.

    Errors: invalid_request (prices on the wrong side or off the tick grid, missing
    entry_price), order_limit, not_found, ib_api_error (what-if rejected), plus the
    trading-gate errors.
    """
    with invalid_request_on(BracketSpec):
        spec = BracketSpec(
            contract=contract,
            action=action,
            quantity=quantity,
            entry_type=entry_type,
            entry_price=entry_price,
            entry_stop_price=entry_stop_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            tif=tif,
            good_till_date=good_till_date,
            outside_rth=outside_rth,
            model_code=model_code,
            soft_dollar_tier=soft_dollar_tier,
        )
    return await gateway_from(ctx).orders.preview_bracket(spec, account=account)


@ib_tool("orders", Tier.READ, "Preview an OCA group")
async def preview_oca_group(
    ctx: ToolContext,
    *,
    orders: Annotated[
        list[OrderSpec],
        Field(
            min_length=2,
            max_length=10,
            description="2-10 orders (any instruments) that belong to one group.",
        ),
    ],
    oca_type: Annotated[
        OcaType,
        Field(
            description=(
                "1: a fill cancels the other orders (with block); 2: a fill reduces the others "
                "proportionally (with block); 3: reduces them without overfill protection."
            )
        ),
    ] = 1,
    account: AccountArg = None,
) -> OrderPreview:
    """Check a One-Cancels-All group: when one order fills, the others are cancelled or reduced.

    Typical use: an exit at a profit target OR a stop, or entries at several prices where
    only one should fill. Each order gets IBKR's what-if and the order limits; the token
    places all of them with a shared OCA group name, and each counts against the order
    rate limit. Order fields as in preview_order.
    Errors: invalid_request, order_limit, not_found, ib_api_error, plus the trading-gate
    errors.
    """
    with invalid_request_on(OcaSpec):
        spec = OcaSpec(orders=orders, oca_type=oca_type)
    return await gateway_from(ctx).orders.preview_oca(spec, account=account)


@ib_tool("orders", Tier.READ, "Preview a combo order")
async def preview_combo_order(
    ctx: ToolContext,
    *,
    legs: Annotated[
        list[ComboOrderLegSpec],
        Field(
            min_length=2,
            max_length=8,
            description=(
                "2-8 legs: contract, ratio and action (BUY or SELL when the combo is bought)."
            ),
        ),
    ],
    action: Annotated[Action, Field(description="BUY or SELL the combo as a whole.")],
    quantity: QuantityArg,
    order_type: Annotated[ComboOrderType, Field(description="LMT, MKT or REL.")] = "LMT",
    limit_price: Annotated[
        float | None,
        Field(allow_inf_nan=False, description="Net price per combo unit; negative for a credit."),
    ] = None,
    tif: Annotated[Literal["DAY", "GTC", "IOC"], Field(description="DAY, GTC or IOC.")] = "DAY",
    outside_rth: Annotated[
        bool, Field(description="Allow fills outside regular trading hours.")
    ] = False,
    non_guaranteed: Annotated[
        bool,
        Field(
            description=(
                "Let IBKR fill legs separately (required for stock pairs and legs on "
                "different underlyings); a leg may then fill without the others."
            )
        ),
    ] = False,
    model_code: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=64,
            pattern=MODEL_CODE_PATTERN,
            description=(
                "Advisor model portfolio to trade within the account (FA logins). Letters, "
                "digits, space, dot, underscore and hyphen."
            ),
        ),
    ] = None,
    soft_dollar_tier: Annotated[
        SoftDollarTierRef | None,
        Field(description="Soft dollar tier {name, value} from get_soft_dollar_tiers."),
    ] = None,
    account: AccountArg = None,
) -> OrderPreview:
    """Check a multi-leg (BAG) order such as a vertical spread, strangle or stock pair.

    The legs are qualified to contract ids and combined into one combo contract; the
    net price is per combo unit (quantity x ratio of each leg trades). Returns IBKR's
    what-if for the combo, the order limits, and a token for submit_order. The notional
    is the gross sum over legs, so under a server notional limit every leg needs a
    market price (options need OPRA data); the net price never stands in for it.

    Errors: invalid_request (duplicate legs, mixed currencies), order_limit, not_found or
    ambiguous_contract for a leg, ib_api_error, plus the trading-gate errors.
    """
    with invalid_request_on(ComboSpec):
        spec = ComboSpec(
            legs=legs,
            action=action,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            tif=tif,
            outside_rth=outside_rth,
            non_guaranteed=non_guaranteed,
            model_code=model_code,
            soft_dollar_tier=soft_dollar_tier,
        )
    return await gateway_from(ctx).orders.preview_combo(spec, account=account)


@ib_tool("orders", Tier.READ, "Preview an order modification")
async def preview_modify_order(
    ctx: ToolContext,
    order_id: OrderIdArg,
    changes: Annotated[
        ModifySpec,
        Field(
            description=(
                "Fields to change: quantity, limit_price, aux_price, trailing_percent, "
                "trail_stop_price, tif, good_till_date, outside_rth. Omitted fields stay."
            )
        ),
    ],
) -> OrderPreview:
    """Check a change to a working order that this server placed; returns a token.

    Only orders placed by this server's API client can be changed (IBKR rule); others
    show modifiable=false in get_open_orders. The order type, side and contract cannot
    change: cancel and place a new order instead. New prices must sit on the contract's
    price increments. The what-if shows the modified order's full impact as if it were
    new. If the order fills (even partly) or is edited elsewhere before submit_order, the
    submit is refused. If IBKR rejects the modification, submit_order returns
    accepted=false and the original order keeps working unchanged. Errors: not_found,
    invalid_request (another client's order, finished order, a field its type lacks, a
    price off the tick grid), order_limit, ib_api_error.
    """
    return await gateway_from(ctx).orders.preview_modify(order_id, changes)


@ib_tool("orders", Tier.READ, "Preview an option exercise")
async def preview_exercise_options(
    ctx: ToolContext,
    *,
    contract: ContractArg,
    action: Annotated[
        ExerciseAction,
        Field(description="exercise, or lapse (let the options expire unexercised)."),
    ],
    quantity: Annotated[int, Field(ge=1, description="Number of option contracts.")],
    override: Annotated[
        bool,
        Field(description="Override IBKR's automatic action (e.g. exercise out of the money)."),
    ] = False,
    account: AccountArg = None,
) -> OrderPreview:
    """Check exercising (or letting lapse) option contracts the account holds; returns a token.

    Irreversible once submitted, and IBKR sends no acknowledgement: the result shows up
    later in positions and account values. The contract may be given by con_id alone
    (e.g. from get_positions). There is no what-if for exercises; the order limits treat
    an exercise as trading quantity x multiplier of the underlying at the strike, so the
    underlying's symbol and sec type (STK for OPT, FUT for FOP) must be allowed too.
    Warns when the positions cache shows fewer contracts than requested.
    Errors: invalid_request (not an option), not_found, order_limit, plus the
    trading-gate errors.
    """
    with invalid_request_on(ExerciseSpec):
        spec = ExerciseSpec(contract=contract, action=action, quantity=quantity, override=override)
    return await gateway_from(ctx).orders.preview_exercise(spec, account=account)


@ib_tool("orders", Tier.READ, "Preview cancelling all orders")
async def preview_cancel_all_orders(
    ctx: ToolContext,
    *,
    account: AccountArg = None,
    scope: Annotated[
        CancelScope,
        Field(
            description=(
                "this_client: working orders this server placed in the account. global: "
                "IBKR's global cancel of every working order on the login (all accounts, "
                "other programs, manual TWS orders); only allowed when the operator enabled "
                "it and every account of the login is in the server's allowlist."
            )
        ),
    ] = "this_client",
) -> OrderPreview:
    """List the working orders a cancel-all would cancel; returns a token for submit_order.

    Nothing is cancelled yet. With this_client (default), submit_order cancels exactly
    the listed orders one by one. With global, it sends IBKR's global cancel, which also
    hits orders placed after this preview. The result's status is Cancelled only when
    every order ended cancelled (PartlyCancelled when some filled first; see messages).
    To cancel one order, use cancel_order.
    Errors: not_found (no working orders), configuration_error (global while the
    operator has not enabled it), account_not_allowed (global without every account
    allowed), plus the trading-gate errors.
    """
    return await gateway_from(ctx).orders.preview_cancel_all(account=account, scope=scope)


@ib_tool("orders", Tier.WRITE, "Submit a previewed order")
async def submit_order(
    ctx: ToolContext,
    token: TokenArg,
    confirmation: Annotated[ConfirmationOutcome, Resolve(confirm_submit)],
) -> OrderResult:
    """Execute a previewed action (order, bracket, OCA, combo, modify, exercise, cancel-all).

    Takes only the token, so exactly what was previewed is sent. The server re-checks
    its order limits, the order rate limit (one slot per order: a bracket takes 3) and
    the circuit breaker first; a refused submit leaves the token usable until it expires.
    On a live (real-money) account a human must approve in the client; you cannot
    confirm for them, and a client without that ability is refused (that token is then
    discarded). Then it waits a few seconds for IBKR's first status and returns it:
    `accepted` false means IBKR rejected the order (see `messages`); PreSubmitted/
    Submitted mean it is working; check later with get_order_status. Repeated
    rejections halt trading until a human resets it.

    Errors: token_not_found (unknown, already used, or discarded), token_expired
    (preview again), order_limit, rate_limit (wait the stated time, then retry the
    same token while it is valid, else preview again), circuit_open (stop and tell the
    user), confirmation_declined, confirmation_unavailable, live_trading_disabled,
    invalid_request (a modified order changed or filled since the preview),
    not_connected.
    """
    gateway = gateway_from(ctx)
    try:
        status = require_confirmation(confirmation)
    except SafetyError as exc:
        gateway.orders.discard(token, reason=str(exc))
        raise
    return await gateway.orders.submit(
        token, human_confirmed=status is ConfirmationStatus.CONFIRMED
    )


@ib_tool("orders", Tier.WRITE, "Cancel an order", idempotent=True)
async def cancel_order(
    ctx: ToolContext,
    order_id: OrderIdArg,
    confirmation: Annotated[ConfirmationOutcome, Resolve(confirm_cancel)],
) -> OrderResult:
    """Cancel one working order that this server placed (no preview needed).

    Waits a few seconds for IBKR to confirm and returns the order's status (Cancelled,
    or PendingCancel while IBKR is still processing). A partly filled order keeps its
    fills. Only orders placed by this server's API client can be cancelled (IBKR rule).
    On a live (real-money) account a human must approve in the client first (cancelling
    a stop loss can leave a position unprotected). To cancel every order, use
    preview_cancel_all_orders then submit_order.
    Errors: not_found, invalid_request (another client's order, or already finished),
    account_not_allowed, confirmation_declined, confirmation_unavailable, not_connected.
    """
    gateway = gateway_from(ctx)
    try:
        status = require_confirmation(confirmation)
    except SafetyError as exc:
        gateway.orders.cancel_declined(order_id, reason=str(exc))
        raise
    return await gateway.orders.cancel(
        order_id, human_confirmed=status is ConfirmationStatus.CONFIRMED
    )


@ib_tool("orders", Tier.READ, "Order status")
async def get_order_status(
    ctx: ToolContext,
    *,
    order_id: Annotated[
        int | None,
        Field(ge=1, description="Order id of an order this server placed."),
    ] = None,
    perm_id: Annotated[
        int | None,
        Field(ge=1, description="IBKR permanent id of any order on the login."),
    ] = None,
) -> OrderStatusOut:
    """Return one order's status, fills (price, size, commission), remaining quantity and log.

    Give exactly one of order_id (orders this server placed) or perm_id (any order,
    including other programs' and manual TWS orders, as shown by get_open_orders). The
    log lists status changes and IBKR messages (rejection reasons, warnings). Covers
    working orders and recently completed ones; only orders in allowed accounts. IBKR's
    completed orders carry no order id, so an order completed before this server started
    is only found by perm_id (every submit result includes it).
    Errors: invalid_request (both or neither id), not_found.
    """
    return await gateway_from(ctx).orders.order_status(order_id=order_id, perm_id=perm_id)
