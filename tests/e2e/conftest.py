"""Fixtures for end-to-end tests: a fake gateway on a local port and real ib_async clients.

Unlike the unit and MCP suites, nothing here mocks ``ib_async.IB``: the gateways built
by :func:`gateway_factory` use the real client, so ib_async's handshake, framing,
decoder and event wiring run against :class:`~tests.e2e.fake_tws.FakeTws`. Everything
stays on 127.0.0.1 and finishes in a few seconds; reconnect backoffs are shortened here
so drop-and-reconnect scenarios do not wait out the production delays, and so is the
gateway's one-second ``reqCurrentTime`` window (together with the client's spacing).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from ib_gateway_mcp import connection as connection_module
from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.gateway import Gateway
from tests.e2e.fake_tws import FakeTws

GatewayFactory = Callable[..., Gateway]

FAST_CURRENT_TIME_WINDOW = 0.1
"""The fake's ``reqCurrentTime`` window here (IB Gateway's is 1 s)."""
FAST_CURRENT_TIME_INTERVAL = 0.15
"""The client's ``reqCurrentTime`` spacing here; like production, above the window."""


async def eventually(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    """Wait until ``predicate()`` holds, polling the event loop every few milliseconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


@pytest.fixture(autouse=True)
def _fast_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry within tens of milliseconds instead of the production 1 s doubling to 60 s.

    Also spaces ``reqCurrentTime`` requests to match the fake's shortened window.
    """
    monkeypatch.setattr(connection_module, "INITIAL_BACKOFF", 0.05)
    monkeypatch.setattr(connection_module, "MAX_BACKOFF", 0.1)
    monkeypatch.setattr(connection_module, "CURRENT_TIME_INTERVAL", FAST_CURRENT_TIME_INTERVAL)


@pytest.fixture
async def fake_tws() -> AsyncIterator[FakeTws]:
    """A started fake gateway with one paper account and one stock (AAPL)."""
    tws = FakeTws(current_time_window=FAST_CURRENT_TIME_WINDOW)
    await tws.start()
    try:
        yield tws
    finally:
        await tws.stop()


@pytest.fixture
def e2e_settings(
    settings_factory: Callable[..., Settings], fake_tws: FakeTws
) -> Callable[..., Settings]:
    """Build Settings aimed at ``fake_tws`` (test defaults: 1 s connect and request timeouts)."""

    def make(**overrides: Any) -> Settings:
        return settings_factory(**{"ib_port": fake_tws.port, **overrides})

    return make


@pytest.fixture
async def gateway_factory(
    e2e_settings: Callable[..., Settings],
) -> AsyncIterator[GatewayFactory]:
    """Build (not start) gateways on the real ``ib_async.IB``; all are stopped at teardown.

    Keyword arguments are Settings fields, e.g. ``gateway_factory(profile="trading")``.
    """
    built: list[Gateway] = []

    def make(**overrides: Any) -> Gateway:
        gateway = Gateway(e2e_settings(**overrides))
        built.append(gateway)
        return gateway

    try:
        yield make
    finally:
        for gateway in reversed(built):
            await gateway.stop()


@pytest.fixture
async def gateway(gateway_factory: GatewayFactory, fake_tws: FakeTws) -> Gateway:
    """A started gateway connected to ``fake_tws`` (readonly profile)."""
    gw = gateway_factory()
    await gw.start()
    await gw.wait_connected(timeout=3)
    await fake_tws.wait_for_ready()
    return gw
