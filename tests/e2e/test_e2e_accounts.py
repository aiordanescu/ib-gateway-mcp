"""Account scoping end to end, on a login that manages several accounts."""

from __future__ import annotations

import pytest

from ib_gateway_mcp.errors import AccountNotAllowedError, ConfigurationError
from ib_gateway_mcp.models.ops import ConnectionState
from tests.e2e.conftest import GatewayFactory
from tests.e2e.fake_tws import PAPER_ACCOUNT, FakeTws

OTHER_PAPER = "DU7654321"
"""A second placeholder paper account on the same login."""

_REQ_ACCOUNT_UPDATES_MULTI = 76


@pytest.fixture(autouse=True)
def _two_accounts(fake_tws: FakeTws) -> None:
    fake_tws.accounts = [PAPER_ACCOUNT, OTHER_PAPER]


async def test_multi_account_login_with_an_allowlist(
    gateway_factory: GatewayFactory, fake_tws: FakeTws
) -> None:
    gw = gateway_factory(accounts_allowlist=[OTHER_PAPER])
    await gw.start()
    await gw.wait_connected(timeout=3)
    health = gw.health()
    assert health.state is ConnectionState.CONNECTED
    assert health.accounts == [OTHER_PAPER]
    listed = gw.ops.list_accounts()
    assert listed.default_account is None
    assert listed.other_managed_accounts == 1
    # ib_async opens one account-update subscription per managed account at connect.
    synced = {fields[3] for fields in fake_tws.current.requests(_REQ_ACCOUNT_UPDATES_MULTI)}
    assert synced == {PAPER_ACCOUNT, OTHER_PAPER}

    with pytest.raises(ConfigurationError, match="IB_ACCOUNT is not set"):
        gw.accounts.resolve()
    assert gw.accounts.resolve(OTHER_PAPER) == OTHER_PAPER
    with pytest.raises(AccountNotAllowedError):
        gw.accounts.resolve(PAPER_ACCOUNT)


async def test_missing_default_account_error_gives_advice_that_works(
    gateway_factory: GatewayFactory,
) -> None:
    gw = gateway_factory()
    await gw.start()
    await gw.wait_connected(timeout=3)
    with pytest.raises(ConfigurationError) as caught:
        await gw.account.account_values()
    advice = str(caught.value)
    followed = "IBKR_MCP_ACCOUNTS" in advice
    if not followed and "account explicitly" in advice:
        try:
            await gw.account.account_values(OTHER_PAPER)
        except AccountNotAllowedError:
            pass
        else:
            followed = True
    assert followed, advice


async def test_trading_is_not_reported_enabled_without_an_allowed_account(
    gateway_factory: GatewayFactory,
) -> None:
    gw = gateway_factory(profile="trading")
    await gw.start()
    await gw.wait_connected(timeout=3)
    health = gw.health()
    assert health.accounts == []
    assert health.trading_enabled is False
