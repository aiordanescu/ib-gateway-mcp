"""AccountScope: default account, allowlist, record attribution and filtering."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from ib_async import AccountValue, Order, Trade

from ib_gateway_mcp.accounts import AccountScope, account_of, is_paper_account
from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import AccountNotAllowedError, ConfigurationError, NotConnectedError
from tests.fakes import LIVE_ACCOUNT, PAPER_ACCOUNT, fill, portfolio_item, position, trade

OTHER_PAPER = "DU7654321"
THIRD_PAPER = "DU1111111"


def scope_for(
    settings_factory: Callable[..., Settings], managed: list[str], **settings: object
) -> AccountScope:
    scope = AccountScope(settings_factory(**settings))
    scope.refresh(managed)
    return scope


def test_single_managed_account_is_default_and_allowed(
    settings_factory: Callable[..., Settings],
) -> None:
    scope = scope_for(settings_factory, [PAPER_ACCOUNT])
    assert scope.ready
    assert scope.default == PAPER_ACCOUNT
    assert scope.allowed == {PAPER_ACCOUNT}
    assert scope.resolve() == PAPER_ACCOUNT
    assert scope.resolve(f" {PAPER_ACCOUNT} ") == PAPER_ACCOUNT
    assert scope.all_paper is True
    assert scope.live_accounts_in_scope == ()


def test_resolve_before_connecting_raises(settings_factory: Callable[..., Settings]) -> None:
    scope = AccountScope(settings_factory())
    assert not scope.ready
    assert scope.all_paper is None
    with pytest.raises(NotConnectedError, match="unknown until"):
        scope.resolve()


def test_allowlist_adds_accounts_and_drops_unknown_ones(
    settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> None:
    scope = scope_for(
        settings_factory,
        [PAPER_ACCOUNT, OTHER_PAPER, THIRD_PAPER],
        ib_account=PAPER_ACCOUNT,
        accounts_allowlist=[OTHER_PAPER, "DU9999999"],
    )
    assert scope.default == PAPER_ACCOUNT
    assert scope.allowed == {PAPER_ACCOUNT, OTHER_PAPER}
    assert "DU9999999" in caplog.text
    assert scope.resolve(OTHER_PAPER) == OTHER_PAPER
    with pytest.raises(AccountNotAllowedError, match=THIRD_PAPER):
        scope.resolve(THIRD_PAPER)


def test_several_accounts_without_default_need_an_explicit_account(
    settings_factory: Callable[..., Settings],
) -> None:
    scope = scope_for(
        settings_factory, [PAPER_ACCOUNT, OTHER_PAPER], accounts_allowlist=[OTHER_PAPER]
    )
    assert scope.default is None
    with pytest.raises(ConfigurationError, match="IB_ACCOUNT is not set"):
        scope.resolve()
    assert scope.resolve(OTHER_PAPER) == OTHER_PAPER


def test_configured_default_that_is_not_managed(settings_factory: Callable[..., Settings]) -> None:
    scope = scope_for(settings_factory, [PAPER_ACCOUNT, OTHER_PAPER], ib_account="DU9999999")
    assert scope.default is None
    with pytest.raises(ConfigurationError, match="not managed") as info:
        scope.resolve()
    # The error must not reveal other managed accounts to the model.
    assert PAPER_ACCOUNT not in str(info.value)


def test_live_accounts_in_scope(settings_factory: Callable[..., Settings]) -> None:
    mixed = scope_for(settings_factory, [PAPER_ACCOUNT, LIVE_ACCOUNT], ib_account=PAPER_ACCOUNT)
    assert mixed.live_accounts_in_scope == ()
    assert mixed.all_paper is False

    live_allowed = scope_for(
        settings_factory,
        [PAPER_ACCOUNT, LIVE_ACCOUNT],
        ib_account=PAPER_ACCOUNT,
        accounts_allowlist=[LIVE_ACCOUNT],
    )
    assert live_allowed.live_accounts_in_scope == (LIVE_ACCOUNT,)

    # Nothing allowed yet: treat every managed account as in scope.
    unresolved = scope_for(settings_factory, [PAPER_ACCOUNT, LIVE_ACCOUNT])
    assert unresolved.live_accounts_in_scope == (LIVE_ACCOUNT,)


def test_is_paper_account() -> None:
    assert is_paper_account("DU1234567")
    assert is_paper_account("df1234567")
    assert not is_paper_account("U1234567")


def test_account_of_handles_every_record_shape() -> None:
    assert account_of(position(OTHER_PAPER)) == OTHER_PAPER
    assert account_of(portfolio_item(OTHER_PAPER)) == OTHER_PAPER
    assert account_of(AccountValue(OTHER_PAPER, "NetLiquidation", "1", "USD", "")) == OTHER_PAPER
    assert account_of(fill(OTHER_PAPER)) == OTHER_PAPER
    assert account_of(trade(OTHER_PAPER)) == OTHER_PAPER
    assert account_of(Trade(order=Order())) is None
    assert account_of(object()) is None


def test_filter_keeps_only_the_resolved_account(settings_factory: Callable[..., Settings]) -> None:
    scope = scope_for(
        settings_factory,
        [PAPER_ACCOUNT, OTHER_PAPER, THIRD_PAPER],
        ib_account=PAPER_ACCOUNT,
        accounts_allowlist=[OTHER_PAPER],
    )
    records = [
        position(PAPER_ACCOUNT),
        position(OTHER_PAPER),
        fill(THIRD_PAPER),
        trade(PAPER_ACCOUNT),
        Trade(order=Order()),  # no account evidence: never matches
    ]
    assert scope.filter(records) == [records[0], records[3]]
    assert scope.filter(records, OTHER_PAPER) == [records[1]]
    assert scope.is_allowed(OTHER_PAPER)
    assert not scope.is_allowed(THIRD_PAPER)
    assert not scope.is_allowed(None)


def test_describe_lists_allowed_accounts(settings_factory: Callable[..., Settings]) -> None:
    scope = scope_for(
        settings_factory,
        [PAPER_ACCOUNT, LIVE_ACCOUNT],
        ib_account=PAPER_ACCOUNT,
        accounts_allowlist=[LIVE_ACCOUNT],
    )
    assert list(scope.describe()) == [(PAPER_ACCOUNT, True, True), (LIVE_ACCOUNT, False, False)]
