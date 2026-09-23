"""The advisor toolset through an in-memory MCP client, with structured output validated."""

from typing import Any
from unittest.mock import MagicMock

import mcp_types as types
import pytest
from ib_async import FamilyCode, SoftDollarTier

from ib_gateway_mcp.models.advisor import (
    FaApplyResult,
    FaConfig,
    FamilyCodeList,
    FaReplacePreview,
    SoftDollarTierList,
)
from tests.conftest import McpClientFactory
from tests.fakes import LIVE_ACCOUNT, PAPER_ACCOUNT, returns
from tests.unit.test_advisor_service import (
    CURRENT_XML,
    NEW_XML,
    OTHER_PAPER,
    RequestFutures,
    soon,
)

ADVISOR_TOOLS = {
    "get_fa_config",
    "preview_replace_fa_config",
    "apply_fa_config",
    "get_soft_dollar_tiers",
    "get_family_codes",
}
MODES = ["auto", "legacy"]  # 2026-07-28 input_required rounds, and mid-call elicitation

# The login in these tests has one account, so the XML names only that one.
SINGLE_CURRENT = CURRENT_XML.replace(f"<Account><acct>{OTHER_PAPER}</acct></Account>", "")
SINGLE_NEW = NEW_XML.replace(OTHER_PAPER, PAPER_ACCOUNT)


class Elicitor:
    """An elicitation callback that records what it was asked."""

    def __init__(self, action: str, content: dict[str, Any] | None = None) -> None:
        self.action = action
        self.content = content
        self.asked: list[types.ElicitRequestParams] = []

    async def __call__(
        self, _context: Any, params: types.ElicitRequestParams
    ) -> types.ElicitResult:
        self.asked.append(params)
        return types.ElicitResult(action=self.action, content=self.content)  # type: ignore[arg-type]


def text_of(result: types.CallToolResult) -> str:
    return result.content[0].text  # type: ignore[union-attr]


def fake_fa(fake_ib: MagicMock, *, live: bool = False) -> RequestFutures:
    """A login whose FA config is SINGLE_CURRENT and that confirms every replacement."""
    account = LIVE_ACCOUNT if live else PAPER_ACCOUNT
    fake_ib.managedAccounts.return_value = [account]
    fake_ib.requestFAAsync.side_effect = returns(SINGLE_CURRENT.replace(PAPER_ACCOUNT, account))
    futures = RequestFutures(fake_ib)

    def send(req_id: int, _fa_data: int, _xml: str) -> None:
        soon(lambda: fake_ib.wrapper.replaceFAEnd(req_id, "FA data replaced"))

    fake_ib.client.replaceFA.side_effect = send
    return futures


async def test_advisor_tools_are_listed_with_schemas(mcp_client: McpClientFactory) -> None:
    async with mcp_client(profile="full") as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= ADVISOR_TOOLS
    for name in ADVISOR_TOOLS:
        tool = tools[name]
        assert tool.description
        assert tool.output_schema is not None
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is (name != "apply_fa_config")
    apply = tools["apply_fa_config"]
    assert set(apply.input_schema["properties"]) == {"token"}  # confirmation is not an input
    assert apply.annotations is not None
    assert apply.annotations.destructive_hint is True
    get = tools["get_fa_config"].input_schema["properties"]
    assert set(get) == {"data_type", "include_xml", "max_chars"}
    assert get["data_type"]["enum"] == ["groups", "aliases"]


async def test_advisor_tools_are_not_in_the_readonly_profile(
    mcp_client: McpClientFactory,
) -> None:
    async with mcp_client() as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert not names & ADVISOR_TOOLS


async def test_get_fa_config(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_fa(fake_ib)
    async with mcp_client(profile="full") as client:
        result = await client.call_tool("get_fa_config", {"include_xml": True, "max_chars": 20})
    assert not result.is_error, text_of(result)
    config = FaConfig.model_validate(result.structured_content)
    assert [g.name for g in config.groups] == ["Core", "Fixed"]
    assert config.groups[0].accounts[0].account == PAPER_ACCOUNT
    assert config.xml is not None
    assert len(config.xml) == 20
    assert config.truncated is True


async def test_get_fa_config_on_a_login_without_fa(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_ib.requestFAAsync.side_effect = returns(None)
    async with mcp_client(profile="full") as client:
        result = await client.call_tool("get_fa_config", {"data_type": "aliases"})
    assert result.is_error
    assert "request_timeout:" in text_of(result)
    assert "financial-advisor" in text_of(result)


async def test_preview_and_apply_on_paper(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_fa(fake_ib)
    elicitor = Elicitor("accept", {"confirm": True})
    async with mcp_client(profile="full", elicitation_callback=elicitor) as client:
        previewed = await client.call_tool("preview_replace_fa_config", {"xml": SINGLE_NEW})
        assert not previewed.is_error, text_of(previewed)
        preview = FaReplacePreview.model_validate(previewed.structured_content)
        applied = await client.call_tool("apply_fa_config", {"token": preview.token})
        again = await client.call_tool("apply_fa_config", {"token": preview.token})
    assert not applied.is_error, text_of(applied)
    result = FaApplyResult.model_validate(applied.structured_content)
    assert result.message == "FA data replaced"
    assert result.human_confirmed is False
    assert elicitor.asked == []  # paper logins are never asked
    fake_ib.client.replaceFA.assert_called_once_with(42, 1, SINGLE_NEW.strip())
    assert again.is_error
    assert "token_not_found:" in text_of(again)


async def test_preview_refuses_bad_xml(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    fake_fa(fake_ib)
    async with mcp_client(profile="full") as client:
        result = await client.call_tool("preview_replace_fa_config", {"xml": "<ListOfGroups>"})
    assert result.is_error
    assert "invalid_request:" in text_of(result)
    assert "not well-formed" in text_of(result)


@pytest.mark.parametrize("mode", MODES)
async def test_live_apply_asks_the_human(
    mcp_client: McpClientFactory, fake_ib: MagicMock, mode: str
) -> None:
    fake_fa(fake_ib, live=True)
    elicitor = Elicitor("accept", {"confirm": True})
    async with mcp_client(
        profile="full", allow_live=True, mode=mode, elicitation_callback=elicitor
    ) as client:
        previewed = await client.call_tool(
            "preview_replace_fa_config", {"xml": SINGLE_NEW.replace(PAPER_ACCOUNT, LIVE_ACCOUNT)}
        )
        token = FaReplacePreview.model_validate(previewed.structured_content).token
        applied = await client.call_tool("apply_fa_config", {"token": token})
    assert not applied.is_error, text_of(applied)
    result = FaApplyResult.model_validate(applied.structured_content)
    assert result.human_confirmed is True
    assert result.is_paper is False
    [question] = elicitor.asked
    assert isinstance(question, types.ElicitRequestFormParams)
    assert LIVE_ACCOUNT in question.message
    assert "Replace the FA group configuration" in question.message
    assert '- remove group "Fixed"' in question.message
    assert "written by the requester" in question.message
    assert "LIVE FA CONFIGURATION CHANGE" in question.message
    assert "ORDER" not in question.message  # not the orders wording
    confirm = question.requested_schema["properties"]["confirm"]
    assert confirm["title"] == "Replace the live FA group configuration"
    fake_ib.client.replaceFA.assert_called_once()


@pytest.mark.parametrize(
    ("action", "content"), [("decline", None), ("cancel", None), ("accept", {"confirm": False})]
)
async def test_live_apply_declined(
    mcp_client: McpClientFactory,
    fake_ib: MagicMock,
    action: str,
    content: dict[str, Any] | None,
) -> None:
    fake_fa(fake_ib, live=True)
    elicitor = Elicitor(action, content)
    async with mcp_client(profile="full", allow_live=True, elicitation_callback=elicitor) as client:
        previewed = await client.call_tool(
            "preview_replace_fa_config", {"xml": SINGLE_NEW.replace(PAPER_ACCOUNT, LIVE_ACCOUNT)}
        )
        token = FaReplacePreview.model_validate(previewed.structured_content).token
        applied = await client.call_tool("apply_fa_config", {"token": token})
    assert applied.is_error
    assert "confirmation_declined:" in text_of(applied)
    assert "FA configuration change was not confirmed" in text_of(applied)
    assert "order" not in text_of(applied)
    fake_ib.client.replaceFA.assert_not_called()


async def test_live_apply_without_elicitation_fails_closed(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_fa(fake_ib, live=True)
    async with mcp_client(profile="full", allow_live=True) as client:
        previewed = await client.call_tool(
            "preview_replace_fa_config", {"xml": SINGLE_NEW.replace(PAPER_ACCOUNT, LIVE_ACCOUNT)}
        )
        token = FaReplacePreview.model_validate(previewed.structured_content).token
        applied = await client.call_tool("apply_fa_config", {"token": token})
    assert applied.is_error
    assert "confirmation_unavailable:" in text_of(applied)
    assert "Replacing the FA configuration of a live login" in text_of(applied)
    fake_ib.client.replaceFA.assert_not_called()


async def test_apply_on_a_live_login_without_allow_live(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    fake_fa(fake_ib, live=True)
    elicitor = Elicitor("accept", {"confirm": True})
    async with mcp_client(profile="full", elicitation_callback=elicitor) as client:
        applied = await client.call_tool("apply_fa_config", {"token": "anything"})
    assert applied.is_error
    assert "live_trading_disabled:" in text_of(applied)
    assert elicitor.asked == []


async def test_apply_unknown_token(mcp_client: McpClientFactory) -> None:
    async with mcp_client(profile="full") as client:
        result = await client.call_tool("apply_fa_config", {"token": "stale"})
    assert result.is_error
    assert "token_not_found:" in text_of(result)


async def test_get_soft_dollar_tiers(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    RequestFutures(fake_ib)
    tiers = [SoftDollarTier(name="Research", val="0.5", displayName="Research 50%")]
    fake_ib.client.reqSoftDollarTiers.side_effect = lambda req_id: soon(
        lambda: fake_ib.wrapper.softDollarTiers(req_id, tiers)
    )
    async with mcp_client(profile="full") as client:
        result = await client.call_tool("get_soft_dollar_tiers", {})
    assert not result.is_error, text_of(result)
    listed = SoftDollarTierList.model_validate(result.structured_content)
    assert listed.tiers[0].display_name == "Research 50%"


async def test_get_soft_dollar_tiers_none(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    RequestFutures(fake_ib)
    fake_ib.client.reqSoftDollarTiers.side_effect = lambda req_id: soon(
        lambda: fake_ib.wrapper.softDollarTiers(req_id, [])
    )
    async with mcp_client(profile="full") as client:
        result = await client.call_tool("get_soft_dollar_tiers", {})
    assert result.is_error
    assert "not_found:" in text_of(result)


async def test_get_family_codes(mcp_client: McpClientFactory, fake_ib: MagicMock) -> None:
    RequestFutures(fake_ib)
    codes = [FamilyCode(PAPER_ACCOUNT, "F1"), FamilyCode(OTHER_PAPER, "F1")]
    fake_ib.client.reqFamilyCodes.side_effect = lambda: soon(
        lambda: fake_ib.wrapper.familyCodes(codes)
    )
    async with mcp_client(profile="full") as client:
        result = await client.call_tool("get_family_codes", {})
    assert not result.is_error, text_of(result)
    listed = FamilyCodeList.model_validate(result.structured_content)
    assert [(c.account, c.family_code) for c in listed.codes] == [(PAPER_ACCOUNT, "F1")]
    assert listed.other_accounts == 1
