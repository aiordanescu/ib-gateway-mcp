"""AdvisorService against the autospecced fake IB: FA configuration, soft dollar tiers,
family codes."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from ib_async import FamilyCode, SoftDollarTier
from ib_async.wrapper import RequestError

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    AccountNotAllowedError,
    ConfigurationError,
    ConfirmationUnavailableError,
    IbApiError,
    IbGatewayMcpError,
    InvalidRequestError,
    NotFoundError,
    RequestTimeoutError,
    TokenExpiredError,
    TokenNotFoundError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.safety import AUDIT_LOGGER_NAME, SafetyRails
from ib_gateway_mcp.services.advisor import MAX_FA_XML_CHARS, REPLACE_FA_KIND
from tests.fakes import LIVE_ACCOUNT, PAPER_ACCOUNT, FakeClock, emit_error, returns

OTHER_PAPER = "DU7654321"
"""A second placeholder paper account."""
UNMANAGED = "DU9999999"
"""A placeholder account the login does not manage."""

CURRENT_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<ListOfGroups>
  <Group>
    <name>Core</name>
    <defaultMethod>NetLiq</defaultMethod>
    <ListOfAccts varName="list">
      <Account><acct>{PAPER_ACCOUNT}</acct></Account>
      <Account><acct>{OTHER_PAPER}</acct></Account>
    </ListOfAccts>
  </Group>
  <Group>
    <name>Fixed</name>
    <defaultMethod>ContractsOrShares</defaultMethod>
    <ListOfAccts varName="list">
      <Account><acct>{PAPER_ACCOUNT}</acct><amount>100.0</amount></Account>
    </ListOfAccts>
  </Group>
</ListOfGroups>
"""

NEW_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<ListOfGroups>
  <Group>
    <name>Core</name>
    <defaultMethod>Equal</defaultMethod>
    <ListOfAccts varName="list">
      <Account><acct>{PAPER_ACCOUNT}</acct></Account>
    </ListOfAccts>
  </Group>
  <Group>
    <name>Growth</name>
    <defaultMethod>Mystery</defaultMethod>
    <ListOfAccts varName="list">
      <Account><acct>{OTHER_PAPER}</acct><amount>2</amount></Account>
      <Account><acct>{UNMANAGED}</acct><amount>1</amount></Account>
    </ListOfAccts>
  </Group>
</ListOfGroups>
"""

LEGACY_XML = f"""<ListOfGroups>
  <Group>
    <name>Old</name>
    <ListOfAccts varName="list"><String>{PAPER_ACCOUNT}</String></ListOfAccts>
    <defaultMethod>AvailableEquity</defaultMethod>
  </Group>
</ListOfGroups>"""

ALIASES_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<ListOfAccountAliases>
  <AccountAlias><account>{PAPER_ACCOUNT}</account><alias>Main</alias></AccountAlias>
  <AccountAlias><account>{OTHER_PAPER}</account><alias>Side</alias></AccountAlias>
</ListOfAccountAliases>
"""

BOTH = (PAPER_ACCOUNT, OTHER_PAPER)


# --- fixtures and helpers -----------------------------------------------------------------


class RequestFutures:
    """Stands in for ib_async's per-request futures (``wrapper.startReq``/``_endReq``)."""

    def __init__(self, ib: MagicMock, first_id: int = 42) -> None:
        self.futures: dict[Any, asyncio.Future[Any]] = {}
        ids = itertools.count(first_id)
        ib.client.getReqId.side_effect = lambda: next(ids)
        ib.wrapper.startReq.side_effect = self.start
        ib.wrapper._endReq.side_effect = self.end

    def start(self, key: Any, contract: Any = None, container: Any = None) -> asyncio.Future[Any]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.futures[key] = future
        return future

    def end(self, key: Any, result: Any = None, success: bool = True) -> None:
        future = self.futures.pop(key, None)
        if future is None or future.done():
            return
        if success:
            future.set_result([] if result is None else result)
        else:
            future.set_exception(result)


def soon(action: Callable[[], object]) -> None:
    """Run ``action`` on the next loop iteration, like a gateway answer arriving."""
    asyncio.get_running_loop().call_soon(action)


StartGateway = Callable[..., Awaitable[Gateway]]


@pytest.fixture
async def start_gateway(
    settings_factory: Callable[..., Settings], fake_ib: MagicMock
) -> AsyncIterator[StartGateway]:
    """Start a gateway on ``fake_ib`` with the given managed accounts and settings."""
    started: list[Gateway] = []

    async def start(
        *, accounts: tuple[str, ...] = BOTH, safety: SafetyRails | None = None, **overrides: Any
    ) -> Gateway:
        fake_ib.managedAccounts.return_value = list(accounts)
        overrides.setdefault("profile", "full")
        if len(accounts) > 1:
            overrides.setdefault("accounts_allowlist", list(accounts))
        gateway = Gateway(settings_factory(**overrides), ib_factory=lambda: fake_ib, safety=safety)
        await gateway.start()
        started.append(gateway)
        return gateway

    yield start
    for gateway in started:
        await gateway.stop()


@pytest.fixture
def futures(fake_ib: MagicMock) -> RequestFutures:
    return RequestFutures(fake_ib)


def answer_fa(fake_ib: MagicMock, xml: str | None) -> None:
    fake_ib.requestFAAsync.side_effect = returns(xml)


def answer_replace(fake_ib: MagicMock, text: str = "FA data replaced") -> None:
    def send(req_id: int, _fa_data: int, _xml: str) -> None:
        soon(lambda: fake_ib.wrapper.replaceFAEnd(req_id, text))

    fake_ib.client.replaceFA.side_effect = send


def audit_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == AUDIT_LOGGER_NAME]


# --- get_fa_config ------------------------------------------------------------------------


async def test_fa_groups_are_parsed(start_gateway: StartGateway, fake_ib: MagicMock) -> None:
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)
    config = await gateway.advisor.fa_config("groups")
    fake_ib.requestFAAsync.assert_called_once_with(1)
    assert [g.name for g in config.groups] == ["Core", "Fixed"]
    core, fixed = config.groups
    assert core.method == "NetLiq"
    assert [a.account for a in core.accounts] == [PAPER_ACCOUNT, OTHER_PAPER]
    assert core.accounts[0].amount is None
    assert fixed.accounts[0].amount == 100.0
    assert config.xml is None
    assert config.xml_chars == len(CURRENT_XML)
    assert config.truncated is False


async def test_fa_groups_in_the_legacy_format(
    start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    gateway = await start_gateway()
    answer_fa(fake_ib, LEGACY_XML)
    config = await gateway.advisor.fa_config()
    assert config.groups[0].name == "Old"
    assert config.groups[0].method == "AvailableEquity"
    assert [a.account for a in config.groups[0].accounts] == [PAPER_ACCOUNT]


async def test_fa_groups_only_name_allowed_accounts(
    start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    gateway = await start_gateway(ib_account=PAPER_ACCOUNT, accounts_allowlist=[])
    answer_fa(fake_ib, CURRENT_XML)
    config = await gateway.advisor.fa_config()
    core = config.groups[0]
    assert [a.account for a in core.accounts] == [PAPER_ACCOUNT]
    assert core.other_accounts == 1
    assert OTHER_PAPER not in config.model_dump_json()


async def test_fa_aliases(start_gateway: StartGateway, fake_ib: MagicMock) -> None:
    gateway = await start_gateway(ib_account=PAPER_ACCOUNT, accounts_allowlist=[])
    answer_fa(fake_ib, ALIASES_XML)
    config = await gateway.advisor.fa_config("aliases")
    fake_ib.requestFAAsync.assert_called_once_with(3)
    assert [(a.account, a.alias) for a in config.aliases] == [(PAPER_ACCOUNT, "Main")]
    assert config.other_accounts == 1
    assert config.groups == []


async def test_fa_config_with_raw_xml_is_cut_to_max_chars(
    start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)
    config = await gateway.advisor.fa_config(include_xml=True, max_chars=50)
    assert config.xml == CURRENT_XML[:50]
    assert config.truncated is True
    full = await gateway.advisor.fa_config(include_xml=True)
    assert full.xml == CURRENT_XML
    assert full.truncated is False


async def test_raw_xml_needs_every_account_allowed(
    start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    gateway = await start_gateway(ib_account=PAPER_ACCOUNT, accounts_allowlist=[])
    answer_fa(fake_ib, CURRENT_XML)
    with pytest.raises(AccountNotAllowedError, match="1 of its 2 accounts"):
        await gateway.advisor.fa_config(include_xml=True)
    fake_ib.requestFAAsync.assert_not_called()


@pytest.mark.parametrize("xml", ["", "<ListOfGroups/>"])
async def test_no_fa_groups_is_not_found(
    start_gateway: StartGateway, fake_ib: MagicMock, xml: str
) -> None:
    gateway = await start_gateway()
    answer_fa(fake_ib, xml)
    with pytest.raises(NotFoundError, match="no FA groups"):
        await gateway.advisor.fa_config()


async def test_no_aliases_is_not_found(start_gateway: StartGateway, fake_ib: MagicMock) -> None:
    gateway = await start_gateway()
    answer_fa(fake_ib, "<ListOfAccountAliases/>")
    with pytest.raises(NotFoundError, match="no account aliases"):
        await gateway.advisor.fa_config("aliases")


async def test_no_answer_explains_fa_logins(
    start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    gateway = await start_gateway()
    answer_fa(fake_ib, None)  # ib_async's 4 s internal timeout
    with pytest.raises(RequestTimeoutError, match="financial-advisor"):
        await gateway.advisor.fa_config()


async def test_fa_refusal_fails_at_once(start_gateway: StartGateway, fake_ib: MagicMock) -> None:
    gateway = await start_gateway()

    async def refuse(*_args: Any) -> Any:
        emit_error(fake_ib, 2104, "Market data farm connection is OK:usfarm")
        emit_error(fake_ib, 321, "Error validating request: FA data operations ignored for non FA")
        return await asyncio.get_running_loop().create_future()

    fake_ib.requestFAAsync.side_effect = refuse
    with pytest.raises(InvalidRequestError, match=r"IB error 321.*not appear to be one"):
        await gateway.advisor.fa_config()


async def test_errors_of_other_requests_are_ignored(
    start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    gateway = await start_gateway()

    async def answer(*_args: Any) -> str:
        emit_error(fake_ib, 321, "FA something", req_id=7)  # tied to another request
        await asyncio.sleep(0)
        return CURRENT_XML

    fake_ib.requestFAAsync.side_effect = answer
    config = await gateway.advisor.fa_config()
    assert len(config.groups) == 2


async def test_a_late_answer_after_a_timeout_is_dropped(
    start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    """ib_async keys requestFA by one string: a timed-out request's answer can arrive late."""
    gateway = await start_gateway(ib_account=PAPER_ACCOUNT, accounts_allowlist=[])
    answer_fa(fake_ib, None)  # ib_async's 4 s wait ran out
    with pytest.raises(RequestTimeoutError, match="late answer"):
        await gateway.advisor.fa_config("groups")
    answers = iter([CURRENT_XML, ALIASES_XML])  # the late groups answer, then the aliases

    async def answer(*_args: Any) -> str:
        return next(answers)

    fake_ib.requestFAAsync.side_effect = answer
    config = await gateway.advisor.fa_config("aliases")
    assert [(a.account, a.alias) for a in config.aliases] == [(PAPER_ACCOUNT, "Main")]
    assert fake_ib.requestFAAsync.call_count == 3


async def test_unparseable_gateway_xml(start_gateway: StartGateway, fake_ib: MagicMock) -> None:
    gateway = await start_gateway()
    answer_fa(fake_ib, "<ListOfGroups><Group>")
    with pytest.raises(IbGatewayMcpError, match="gateway's FA groups XML is unusable"):
        await gateway.advisor.fa_config()


async def test_unknown_data_type(start_gateway: StartGateway) -> None:
    gateway = await start_gateway()
    with pytest.raises(InvalidRequestError, match="profiles"):
        await gateway.advisor.fa_config("profiles")  # type: ignore[arg-type]


# --- preview_replace_fa_config ------------------------------------------------------------


async def test_preview_diffs_and_issues_a_token(
    start_gateway: StartGateway, fake_ib: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger=AUDIT_LOGGER_NAME)
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)
    preview = await gateway.advisor.preview_replace_fa_config(NEW_XML)

    assert preview.accounts == sorted(BOTH)
    assert preview.is_paper is True
    assert (preview.groups_before, preview.groups_after) == (2, 2)
    changes = {c.name: c for c in preview.changes}
    assert changes["Core"].change == "changed"
    assert (changes["Core"].method_before, changes["Core"].method_after) == ("NetLiq", "Equal")
    assert changes["Core"].accounts_removed == [OTHER_PAPER]
    assert changes["Growth"].change == "added"
    assert changes["Growth"].accounts_added == [OTHER_PAPER, UNMANAGED]
    assert changes["Fixed"].change == "removed"
    assert preview.unchanged_groups == []
    assert any("Mystery" in w for w in preview.warnings)
    assert any(UNMANAGED in w for w in preview.warnings)
    assert preview.summary[0].startswith("Replace the FA group configuration of 2 account(s)")
    assert any(line.startswith('- remove group "Fixed"') for line in preview.summary)

    record = gateway.safety.previews.peek(preview.token)
    assert record.kind == REPLACE_FA_KIND
    assert record.account == ",".join(sorted(BOTH))
    assert record.payload["xml"] == NEW_XML.strip()
    assert record.payload["fa_data_type"] == 1
    fake_ib.client.replaceFA.assert_not_called()
    [event] = audit_events(caplog)
    assert event["event"] == "preview"
    assert event["kind"] == REPLACE_FA_KIND


async def test_preview_reports_unchanged_groups_and_amount_changes(
    start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)
    new = CURRENT_XML.replace("<amount>100.0</amount>", "<amount>250</amount>")
    preview = await gateway.advisor.preview_replace_fa_config(new)
    assert preview.unchanged_groups == ["Core"]
    [change] = preview.changes
    assert change.name == "Fixed"
    assert change.amounts_changed == [PAPER_ACCOUNT]
    assert preview.warnings == []


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        ("   ", "xml is empty"),
        ("<ListOfGroups><Group>", "not well-formed"),
        ('<!DOCTYPE x [<!ENTITY a "b">]><ListOfGroups/>', "DOCTYPE"),
        ("<ListOfAccountAliases/>", "must be <ListOfGroups>"),
        ("<ListOfGroups/>", "defines no <Group>"),
        (
            "<ListOfGroups><Group><name>A</name></Group><Group><name>A</name></Group>"
            "</ListOfGroups>",
            "defined twice",
        ),
        (
            "<ListOfGroups><Group><defaultMethod>Equal</defaultMethod></Group></ListOfGroups>",
            "has no <name>",
        ),
        (
            f"<ListOfGroups><Group><name>A</name><ListOfAccts><String>{PAPER_ACCOUNT}</String>"
            f"<String>{PAPER_ACCOUNT}</String></ListOfAccts></Group></ListOfGroups>",
            "twice",
        ),
        (CURRENT_XML, "nothing to apply"),
    ],
)
async def test_preview_refuses_unusable_xml(
    start_gateway: StartGateway, fake_ib: MagicMock, xml: str, message: str
) -> None:
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)
    with pytest.raises(InvalidRequestError, match=message):
        await gateway.advisor.preview_replace_fa_config(xml)
    assert len(gateway.safety.previews) == 0


async def test_preview_refuses_oversized_xml(start_gateway: StartGateway) -> None:
    gateway = await start_gateway()
    with pytest.raises(InvalidRequestError, match="the limit is"):
        await gateway.advisor.preview_replace_fa_config("<a/>" + " " * MAX_FA_XML_CHARS + "<b/>")


async def test_preview_needs_every_account_allowed(
    start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    gateway = await start_gateway(ib_account=PAPER_ACCOUNT, accounts_allowlist=[])
    answer_fa(fake_ib, CURRENT_XML)
    with pytest.raises(AccountNotAllowedError, match="IBKR_MCP_ACCOUNTS"):
        await gateway.advisor.preview_replace_fa_config(NEW_XML)


@pytest.mark.parametrize(
    ("group", "problem"),
    [
        ("<name>Growth\n(dry run only)</name>", "line breaks or invisible characters"),
        ("<name>Growth</name><defaultMethod>Equal\tx</defaultMethod>", "allocation method"),
        (
            "<name>Growth</name><ListOfAccts><Account><acct>DU1 approve</acct></Account>"
            "</ListOfAccts>",
            "malformed account ids",
        ),
    ],
)
async def test_fa_text_shown_to_a_human_must_be_plain(
    start_gateway: StartGateway, fake_ib: MagicMock, group: str, problem: str
) -> None:
    """Names, methods and accounts reach the confirmation prompt; none may fake a line."""
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)
    with pytest.raises(InvalidRequestError, match=problem):
        await gateway.advisor.preview_replace_fa_config(
            f"<ListOfGroups><Group>{group}</Group></ListOfGroups>"
        )


async def test_preview_only_replaces_groups(start_gateway: StartGateway) -> None:
    gateway = await start_gateway()
    with pytest.raises(InvalidRequestError, match="Only the FA groups"):
        await gateway.advisor.preview_replace_fa_config(ALIASES_XML, "aliases")  # type: ignore[arg-type]


# --- confirmation_request and apply_fa_config ---------------------------------------------


async def test_confirmation_request_describes_without_consuming(
    start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)
    preview = await gateway.advisor.preview_replace_fa_config(NEW_XML)
    request = gateway.advisor.confirmation_request(preview.token)
    again = gateway.advisor.confirmation_request(preview.token)
    assert request == again
    assert request.is_paper is True
    assert request.action == preview.summary[0]
    assert request.details == [
        *preview.summary[1:],
        *(f"Warning: {warning}" for warning in preview.warnings),
    ]


async def test_other_token_kinds_are_refused_without_consuming(
    start_gateway: StartGateway,
) -> None:
    gateway = await start_gateway()
    order = gateway.safety.previews.issue({"x": 1}, PAPER_ACCOUNT, "order")
    with pytest.raises(InvalidRequestError, match="submit_order"):
        gateway.advisor.confirmation_request(order.token)
    with pytest.raises(InvalidRequestError, match="submit_order"):
        await gateway.advisor.apply_fa_config(order.token)
    assert gateway.safety.previews.peek(order.token).kind == "order"


async def test_apply_replaces_the_configuration(
    start_gateway: StartGateway,
    fake_ib: MagicMock,
    futures: RequestFutures,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger=AUDIT_LOGGER_NAME)
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)
    answer_replace(fake_ib, " FA data replaced ")
    preview = await gateway.advisor.preview_replace_fa_config(NEW_XML)

    result = await gateway.advisor.apply_fa_config(preview.token)

    fake_ib.client.replaceFA.assert_called_once_with(42, 1, NEW_XML.strip())
    assert result.message == "FA data replaced"
    assert result.is_paper is True
    assert result.human_confirmed is False
    assert result.groups_after == 2
    assert {c.name for c in result.changes} == {"Core", "Growth", "Fixed"}
    assert "replaceFAEnd" not in vars(fake_ib.wrapper)  # the hook is gone
    events = audit_events(caplog)
    assert [e["event"] for e in events] == ["preview", "replace_fa"]
    applied = events[-1]
    assert applied["previous_xml"] == CURRENT_XML  # what a human needs to restore
    assert applied["xml"] == NEW_XML.strip()
    assert applied["account"] == ",".join(sorted(BOTH))
    with pytest.raises(TokenNotFoundError, match="already used"):
        await gateway.advisor.apply_fa_config(preview.token)


async def test_apply_needs_a_write_profile(start_gateway: StartGateway, fake_ib: MagicMock) -> None:
    gateway = await start_gateway(profile="readonly")
    answer_fa(fake_ib, CURRENT_XML)
    with pytest.raises(ConfigurationError, match="read-only session"):
        await gateway.advisor.preview_replace_fa_config(NEW_XML)
    token = gateway.safety.previews.issue({"xml": NEW_XML}, PAPER_ACCOUNT, REPLACE_FA_KIND).token
    with pytest.raises(ConfigurationError, match="read-only session"):
        await gateway.advisor.apply_fa_config(token)
    fake_ib.client.replaceFA.assert_not_called()
    assert gateway.safety.previews.peek(token)  # still usable


async def test_apply_refuses_when_the_configuration_changed(
    start_gateway: StartGateway,
    fake_ib: MagicMock,
    futures: RequestFutures,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger=AUDIT_LOGGER_NAME)
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)
    preview = await gateway.advisor.preview_replace_fa_config(NEW_XML)
    answer_fa(fake_ib, LEGACY_XML)
    with pytest.raises(InvalidRequestError, match="changed since the preview"):
        await gateway.advisor.apply_fa_config(preview.token)
    fake_ib.client.replaceFA.assert_not_called()
    assert audit_events(caplog)[-1]["event"] == "rejected"
    with pytest.raises(TokenNotFoundError):
        gateway.safety.previews.peek(preview.token)


async def test_apply_audits_a_failed_refetch(
    start_gateway: StartGateway,
    fake_ib: MagicMock,
    futures: RequestFutures,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger=AUDIT_LOGGER_NAME)
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)
    preview = await gateway.advisor.preview_replace_fa_config(NEW_XML)
    answer_fa(fake_ib, None)  # the re-check before replacing gets no answer
    with pytest.raises(RequestTimeoutError):
        await gateway.advisor.apply_fa_config(preview.token)
    fake_ib.client.replaceFA.assert_not_called()
    last = audit_events(caplog)[-1]
    assert (last["event"], last["kind"]) == ("rejected", REPLACE_FA_KIND)
    with pytest.raises(TokenNotFoundError):  # spent: preview again
        gateway.safety.previews.peek(preview.token)


async def test_apply_refuses_when_the_login_changed(
    start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)
    preview = await gateway.advisor.preview_replace_fa_config(NEW_XML)
    gateway.accounts.refresh([PAPER_ACCOUNT])
    with pytest.raises(InvalidRequestError, match="accounts changed"):
        await gateway.advisor.apply_fa_config(preview.token)


async def test_live_apply_needs_a_human(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(accounts=(LIVE_ACCOUNT,), allow_live=True)
    answer_fa(fake_ib, CURRENT_XML.replace(OTHER_PAPER, LIVE_ACCOUNT))
    answer_replace(fake_ib)
    preview = await gateway.advisor.preview_replace_fa_config(
        NEW_XML.replace(OTHER_PAPER, LIVE_ACCOUNT)
    )
    assert preview.is_paper is False
    assert gateway.advisor.confirmation_request(preview.token).is_paper is False
    with pytest.raises(ConfirmationUnavailableError, match="nothing was changed"):
        await gateway.advisor.apply_fa_config(preview.token)
    fake_ib.client.replaceFA.assert_not_called()

    result = await gateway.advisor.apply_fa_config(preview.token, human_confirmed=True)
    assert result.human_confirmed is True
    assert result.accounts == [LIVE_ACCOUNT]


async def test_live_apply_without_confirmation_when_switched_off(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(accounts=(LIVE_ACCOUNT,), allow_live=True, live_confirm=False)
    answer_fa(fake_ib, CURRENT_XML)
    answer_replace(fake_ib)
    preview = await gateway.advisor.preview_replace_fa_config(NEW_XML)
    result = await gateway.advisor.apply_fa_config(preview.token)
    assert result.is_paper is False


async def test_apply_rejected_by_ibkr(
    start_gateway: StartGateway,
    fake_ib: MagicMock,
    futures: RequestFutures,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger=AUDIT_LOGGER_NAME)
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)

    def reject(req_id: int, _fa_data: int, _xml: str) -> None:
        error = RequestError(req_id, 555, "Invalid FA group configuration")
        soon(lambda: fake_ib.wrapper._endReq(req_id, error, False))

    fake_ib.client.replaceFA.side_effect = reject
    preview = await gateway.advisor.preview_replace_fa_config(NEW_XML)
    with pytest.raises(IbApiError, match="555") as caught:
        await gateway.advisor.apply_fa_config(preview.token)
    assert caught.value.req_id == 42
    last = audit_events(caplog)[-1]
    assert (last["event"], last["kind"]) == ("rejected", REPLACE_FA_KIND)
    assert "555" in last["reason"]


async def test_apply_fails_fast_on_a_validation_warning(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway()
    answer_fa(fake_ib, CURRENT_XML)

    def warn(req_id: int, _fa_data: int, _xml: str) -> None:
        soon(lambda: emit_error(fake_ib, 321, "Error validating request", req_id=req_id))

    fake_ib.client.replaceFA.side_effect = warn
    preview = await gateway.advisor.preview_replace_fa_config(NEW_XML)
    with pytest.raises(IbApiError, match="321"):
        await gateway.advisor.apply_fa_config(preview.token)


async def test_apply_without_an_answer_says_it_may_have_applied(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(request_timeout=0.05)
    answer_fa(fake_ib, CURRENT_XML)
    preview = await gateway.advisor.preview_replace_fa_config(NEW_XML)
    with pytest.raises(RequestTimeoutError, match="may still have applied"):
        await gateway.advisor.apply_fa_config(preview.token)
    fake_ib.client.replaceFA.assert_called_once()


async def test_expired_token(
    settings_factory: Callable[..., Settings], start_gateway: StartGateway, fake_ib: MagicMock
) -> None:
    clock = FakeClock()
    rails = SafetyRails.from_settings(
        settings_factory(), clock=clock.time, monotonic=clock.monotonic
    )
    gateway = await start_gateway(safety=rails)
    answer_fa(fake_ib, CURRENT_XML)
    preview = await gateway.advisor.preview_replace_fa_config(NEW_XML)
    clock.advance(rails.previews.ttl + 1)
    with pytest.raises(TokenExpiredError):
        await gateway.advisor.apply_fa_config(preview.token)


# --- soft dollar tiers --------------------------------------------------------------------


async def test_soft_dollar_tiers(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway()
    tiers = [
        SoftDollarTier(name="Research", val="0.5", displayName="Research 50%"),
        SoftDollarTier(),  # blank rows are skipped
    ]

    def send(req_id: int) -> None:
        soon(lambda: fake_ib.wrapper.softDollarTiers(req_id + 1, [SoftDollarTier("x", "y", "")]))
        soon(lambda: fake_ib.wrapper.softDollarTiers(req_id, tiers))

    fake_ib.client.reqSoftDollarTiers.side_effect = send
    original = fake_ib.wrapper.softDollarTiers
    result = await gateway.advisor.soft_dollar_tiers()
    fake_ib.client.reqSoftDollarTiers.assert_called_once_with(42)
    assert [(t.name, t.value, t.display_name) for t in result.tiers] == [
        ("Research", "0.5", "Research 50%")
    ]
    assert fake_ib.wrapper.softDollarTiers is original  # the hook is gone


async def test_no_soft_dollar_tiers(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway()
    fake_ib.client.reqSoftDollarTiers.side_effect = lambda req_id: soon(
        lambda: fake_ib.wrapper.softDollarTiers(req_id, [])
    )
    with pytest.raises(NotFoundError, match="no soft dollar tiers"):
        await gateway.advisor.soft_dollar_tiers()


async def test_soft_dollar_tiers_error(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway()

    def fail(req_id: int) -> None:
        error = RequestError(req_id, 10, "Not supported")
        soon(lambda: fake_ib.wrapper._endReq(req_id, error, False))

    fake_ib.client.reqSoftDollarTiers.side_effect = fail
    with pytest.raises(IbApiError, match="IB error 10"):
        await gateway.advisor.soft_dollar_tiers()


# --- family codes -------------------------------------------------------------------------


async def test_family_codes_are_filtered_by_allowlist(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(ib_account=PAPER_ACCOUNT, accounts_allowlist=[])
    codes = [
        FamilyCode(accountID=PAPER_ACCOUNT, familyCodeStr="F1"),
        FamilyCode(accountID=OTHER_PAPER, familyCodeStr="F1"),
        FamilyCode(accountID="", familyCodeStr=""),
    ]
    fake_ib.client.reqFamilyCodes.side_effect = lambda: soon(
        lambda: fake_ib.wrapper.familyCodes(codes)
    )
    original = fake_ib.wrapper.familyCodes
    result = await gateway.advisor.family_codes()
    assert [(c.account, c.family_code) for c in result.codes] == [(PAPER_ACCOUNT, "F1")]
    assert result.other_accounts == 2
    assert OTHER_PAPER not in result.model_dump_json()
    assert fake_ib.wrapper.familyCodes is original


async def test_family_code_blank_is_none(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(accounts=(PAPER_ACCOUNT,))
    fake_ib.client.reqFamilyCodes.side_effect = lambda: soon(
        lambda: fake_ib.wrapper.familyCodes([FamilyCode(PAPER_ACCOUNT, " ")])
    )
    result = await gateway.advisor.family_codes()
    assert result.codes[0].family_code is None


async def test_no_family_codes_for_allowed_accounts(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(ib_account=PAPER_ACCOUNT, accounts_allowlist=[])
    fake_ib.client.reqFamilyCodes.side_effect = lambda: soon(
        lambda: fake_ib.wrapper.familyCodes([FamilyCode(OTHER_PAPER, "F1")])
    )
    with pytest.raises(NotFoundError, match="1 rows were for other accounts"):
        await gateway.advisor.family_codes()


async def test_family_codes_timeout(
    start_gateway: StartGateway, fake_ib: MagicMock, futures: RequestFutures
) -> None:
    gateway = await start_gateway(request_timeout=0.05)
    with pytest.raises(RequestTimeoutError, match="family codes"):
        await gateway.advisor.family_codes()
    fake_ib.client.reqFamilyCodes.assert_called_once_with()
