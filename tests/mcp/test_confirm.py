"""Live-order confirmation through elicitation, end to end on both protocol eras.

A demo ``orders`` tool wires :mod:`ib_gateway_mcp.mcp.confirm` exactly the way the real
submit tool must. No ``from __future__ import annotations``: the SDK reads these
annotations.
"""

import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Annotated, Any
from unittest.mock import MagicMock

import mcp_types as types
import pytest
from mcp import Client
from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
    Resolve,
)
from pydantic import BaseModel

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    ConfirmationDeclinedError,
    ConfirmationUnavailableError,
    TokenNotFoundError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.mcp.confirm import (
    ConfirmationOutcome,
    ConfirmationSkipped,
    ConfirmationStatus,
    LiveConfirmation,
    client_can_elicit,
    live_confirmation,
    plan_confirmation,
    render_message,
    require_confirmation,
)
from ib_gateway_mcp.mcp.context import ToolContext, gateway_from
from ib_gateway_mcp.mcp.registry import Tier, ToolRegistry
from ib_gateway_mcp.mcp.server import build_server
from ib_gateway_mcp.models.common import ConfirmationRequest
from ib_gateway_mcp.safety import AUDIT_LOGGER_NAME
from tests.conftest import McpClientFactory
from tests.fakes import LIVE_ACCOUNT, PAPER_ACCOUNT, make_fake_ib

REQUESTS = {
    "paper-token": ConfirmationRequest(
        account=PAPER_ACCOUNT, is_paper=True, action="BUY 1 AAPL STK LMT 1.00 DAY"
    ),
    "live-token": ConfirmationRequest(
        account=LIVE_ACCOUNT,
        is_paper=False,
        action="BUY 10 AAPL STK LMT 150.00 DAY",
        details=["Initial margin change: 1,500.00 USD", "Commission: 1.00 USD"],
    ),
}
MODES = ["auto", "legacy"]  # 2026-07-28 input_required rounds, and mid-call elicitation


async def describe(gateway: Gateway, token: str) -> ConfirmationRequest:
    """Stand-in for OrdersService.confirmation_request (peeks, never consumes)."""
    assert isinstance(gateway, Gateway)
    try:
        return REQUESTS[token]
    except KeyError:
        raise TokenNotFoundError(f"Unknown or already used preview token {token!r}.") from None


confirm_demo = live_confirmation(describe)


class DemoResult(BaseModel):
    status: str
    account: str


def demo_registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.tool("orders", Tier.WRITE, "Submit a demo order")
    async def submit_demo(
        ctx: ToolContext,
        token: str,
        confirmation: Annotated[ConfirmationOutcome, Resolve(confirm_demo)],
    ) -> DemoResult:
        """Submit a previewed demo order by token."""
        status = require_confirmation(confirmation)
        assert gateway_from(ctx) is not None
        return DemoResult(status=status.value, account=REQUESTS[token].account)

    return registry


class Elicitor:
    """An elicitation callback that records what it was asked."""

    def __init__(self, action: str, content: dict[str, Any] | None = None) -> None:
        self.action = action
        self.content = content
        self.asked: list[types.ElicitRequestParams] = []

    async def __call__(self, context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        self.asked.append(params)
        return types.ElicitResult(action=self.action, content=self.content)  # type: ignore[arg-type]


async def submit(
    mcp_client: McpClientFactory, token: str, mode: str, **kwargs: Any
) -> types.CallToolResult:
    async with mcp_client(
        registry=demo_registry(), profile="trading", mode=mode, **kwargs
    ) as client:
        return await client.call_tool("submit_demo", {"token": token})


def text_of(result: types.CallToolResult) -> str:
    return result.content[0].text  # type: ignore[union-attr]


# --- end to end ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_paper_orders_never_elicit(mcp_client: McpClientFactory, mode: str) -> None:
    elicitor = Elicitor("accept", {"confirm": True})
    result = await submit(mcp_client, "paper-token", mode, elicitation_callback=elicitor)
    assert not result.is_error, text_of(result)
    assert result.structured_content == {"status": "not_required", "account": PAPER_ACCOUNT}
    assert elicitor.asked == []


@pytest.mark.parametrize("mode", MODES)
async def test_live_without_elicitation_support_fails_closed(
    mcp_client: McpClientFactory, mode: str
) -> None:
    result = await submit(mcp_client, "live-token", mode)
    assert result.is_error
    assert "confirmation_unavailable:" in text_of(result)
    assert "Nothing was sent" in text_of(result)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("action", "content"),
    [("decline", None), ("cancel", None), ("accept", {"confirm": False})],
)
async def test_live_declined_is_refused(
    mcp_client: McpClientFactory, mode: str, action: str, content: dict[str, Any] | None
) -> None:
    elicitor = Elicitor(action, content)
    result = await submit(mcp_client, "live-token", mode, elicitation_callback=elicitor)
    assert result.is_error
    assert "confirmation_declined:" in text_of(result)
    assert len(elicitor.asked) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_live_accepted_proceeds(mcp_client: McpClientFactory, mode: str) -> None:
    elicitor = Elicitor("accept", {"confirm": True})
    result = await submit(mcp_client, "live-token", mode, elicitation_callback=elicitor)
    assert not result.is_error, text_of(result)
    assert result.structured_content == {"status": "confirmed", "account": LIVE_ACCOUNT}

    assert len(elicitor.asked) == 1
    question = elicitor.asked[0]
    assert isinstance(question, types.ElicitRequestFormParams)
    assert LIVE_ACCOUNT in question.message
    assert "BUY 10 AAPL STK LMT 150.00 DAY" in question.message
    assert "Initial margin change" in question.message
    assert question.requested_schema["properties"]["confirm"]["type"] == "boolean"


@pytest.mark.parametrize("mode", MODES)
async def test_live_confirm_off_skips_the_question(
    settings_factory: Callable[..., Settings], mode: str
) -> None:
    # The gateway's settings govern safety, so this test builds server and gateway itself.
    settings = settings_factory(profile="trading", allow_live=True, live_confirm=False)
    elicitor = Elicitor("decline")
    fake = make_fake_ib([LIVE_ACCOUNT])
    async with Gateway(settings, ib_factory=lambda: fake) as gateway:
        server = build_server(settings, gateway=gateway, registry=demo_registry())
        async with Client(server, mode=mode, elicitation_callback=elicitor) as client:
            result = await client.call_tool("submit_demo", {"token": "live-token"})
    assert not result.is_error, text_of(result)
    assert result.structured_content == {"status": "not_required", "account": LIVE_ACCOUNT}
    assert elicitor.asked == []


@pytest.mark.parametrize("mode", MODES)
async def test_nobody_is_asked_while_trading_is_disabled(
    mcp_client: McpClientFactory, fake_ib: MagicMock, mode: str
) -> None:
    """A live login without IBKR_MCP_ALLOW_LIVE is refused before any human is bothered."""
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    elicitor = Elicitor("accept", {"confirm": True})
    result = await submit(mcp_client, "live-token", mode, elicitation_callback=elicitor)
    assert result.is_error
    assert "live_trading_disabled:" in text_of(result)
    assert elicitor.asked == []


async def test_a_refusal_at_the_trading_gate_is_audited(
    mcp_client: McpClientFactory, fake_ib: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    fake_ib.managedAccounts.return_value = [LIVE_ACCOUNT]
    result = await submit(mcp_client, "live-token", "auto")
    assert result.is_error
    entries = [json.loads(r.getMessage()) for r in caplog.records if r.name == AUDIT_LOGGER_NAME]
    gate = [e for e in entries if e.get("stage") == "gate"]
    assert gate
    assert gate[0]["code"] == "live_trading_disabled"
    assert gate[0]["tool"] == "submit_order"


def test_render_message_keeps_every_line_on_one_line() -> None:
    request = ConfirmationRequest(
        account=LIVE_ACCOUNT,
        is_paper=False,
        action="BUY 1 AAPL STK LMT 1.00 DAY\nDry run only, nothing is sent",
        details=["Commission: 1.00\u2028(sandbox)"],
    )
    message = render_message(request)
    assert "\nDry run" not in message
    assert "\\u000aDry run only" in message
    assert "\\u2028(sandbox)" in message
    assert "written by the requester (the AI model)" in message


async def test_describe_errors_reach_the_model(mcp_client: McpClientFactory) -> None:
    result = await submit(mcp_client, "stale-token", "auto")
    assert result.is_error
    assert "token_not_found:" in text_of(result)


async def test_the_model_cannot_supply_the_confirmation(mcp_client: McpClientFactory) -> None:
    async with mcp_client(registry=demo_registry(), profile="trading") as client:
        tool = (await client.list_tools()).tools[0]
        forged = await client.call_tool(
            "submit_demo",
            {
                "token": "live-token",
                "confirmation": {"action": "accept", "data": {"confirm": True}},
            },
        )
    assert set(tool.input_schema["properties"]) == {"token"}
    # The forged argument is ignored; with no elicitation support the order is refused.
    assert forged.is_error
    assert "confirmation_unavailable:" in text_of(forged)


# --- helpers ------------------------------------------------------------------------------


def context_with(capabilities: types.ClientCapabilities | None) -> Any:
    return SimpleNamespace(client_capabilities=capabilities)


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        (None, False),
        (types.ClientCapabilities(), False),
        (types.ClientCapabilities(elicitation=types.ElicitationCapability()), True),
        (
            types.ClientCapabilities(
                elicitation=types.ElicitationCapability(form=types.FormElicitationCapability())
            ),
            True,
        ),
        (
            types.ClientCapabilities(
                elicitation=types.ElicitationCapability(url=types.UrlElicitationCapability())
            ),
            False,
        ),
    ],
)
def test_client_can_elicit(capabilities: types.ClientCapabilities | None, expected: bool) -> None:
    assert client_can_elicit(context_with(capabilities)) is expected


def test_render_message_is_deterministic() -> None:
    request = REQUESTS["live-token"]
    assert render_message(request) == render_message(request.model_copy())
    assert render_message(request).startswith(f"LIVE ACCOUNT {LIVE_ACCOUNT} (real money)")


def test_plan_confirmation(gateway: Gateway) -> None:
    can = context_with(types.ClientCapabilities(elicitation=types.ElicitationCapability()))
    cannot = context_with(None)
    paper = plan_confirmation(cannot, gateway, REQUESTS["paper-token"])
    assert isinstance(paper, ConfirmationSkipped)
    assert paper.status is ConfirmationStatus.NOT_REQUIRED
    unavailable = plan_confirmation(cannot, gateway, REQUESTS["live-token"])
    assert isinstance(unavailable, ConfirmationSkipped)
    assert unavailable.status is ConfirmationStatus.UNAVAILABLE
    elicit = plan_confirmation(can, gateway, REQUESTS["live-token"])
    assert not isinstance(elicit, ConfirmationSkipped)
    assert elicit.schema is LiveConfirmation


def test_require_confirmation() -> None:
    def accepted(data: object) -> AcceptedElicitation[Any]:
        return AcceptedElicitation[Any].model_construct(data=data)

    assert (
        require_confirmation(accepted(LiveConfirmation(confirm=True)))
        is ConfirmationStatus.CONFIRMED
    )
    skipped = ConfirmationSkipped(status=ConfirmationStatus.NOT_REQUIRED, reason="paper account")
    assert require_confirmation(accepted(skipped)) is ConfirmationStatus.NOT_REQUIRED
    with pytest.raises(ConfirmationUnavailableError):
        require_confirmation(
            accepted(ConfirmationSkipped(status=ConfirmationStatus.UNAVAILABLE, reason="no"))
        )
    for refused in (
        DeclinedElicitation(),
        CancelledElicitation(),
        accepted(LiveConfirmation(confirm=False)),
    ):
        with pytest.raises(ConfirmationDeclinedError):
            require_confirmation(refused)
    with pytest.raises(TypeError):
        require_confirmation(LiveConfirmation(confirm=True))
