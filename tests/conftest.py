"""Shared fixtures: settings, a fake IB, a started Gateway, and an in-memory MCP client.

Unit and MCP tests never touch the network: the gateway fixture runs on
:func:`tests.fakes.make_fake_ib`, and the environment is scrubbed of ``IB_*`` and
``IBKR_MCP_*`` variables so a developer's shell cannot leak into a test.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp import Client

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.mcp.registry import ToolRegistry
from ib_gateway_mcp.mcp.server import build_server
from tests.fakes import make_fake_ib

_ENV_PREFIXES = ("IB_", "IBKR_MCP_")
_INTEGRATION_MARKERS = ("live_readonly", "paper")

TEST_DEFAULTS: dict[str, Any] = {
    "ib_host": "127.0.0.1",
    "ib_port": 4004,
    "ib_client_id": 80,
    "connect_timeout": 1.0,
    "request_timeout": 1.0,
}


@pytest.fixture(autouse=True)
def _clean_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's IB_* / IBKR_MCP_* variables from unit and MCP tests."""
    if not any(request.node.get_closest_marker(m) for m in _INTEGRATION_MARKERS):
        for name in list(os.environ):
            if name.startswith(_ENV_PREFIXES):
                monkeypatch.delenv(name)


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    """Build Settings from test defaults plus overrides (field names, not env names)."""

    def make(**overrides: Any) -> Settings:
        return Settings(**{**TEST_DEFAULTS, **overrides})

    return make


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    """Default test settings: readonly profile, paper port, short timeouts."""
    return settings_factory()


@pytest.fixture
def fake_ib() -> MagicMock:
    """An autospecced ib_async.IB with one paper account (see tests.fakes)."""
    return make_fake_ib()


@pytest.fixture
async def gateway(settings: Settings, fake_ib: MagicMock) -> AsyncIterator[Gateway]:
    """A started Gateway connected to ``fake_ib``."""
    gw = Gateway(settings, ib_factory=lambda: fake_ib)
    await gw.start()
    try:
        yield gw
    finally:
        await gw.stop()


class McpClientFactory:
    """Opens in-memory MCP clients; see the ``mcp_client`` fixture."""

    def __init__(self, fake_ib: MagicMock, settings_factory: Callable[..., Settings]) -> None:
        self._fake_ib = fake_ib
        self._settings_factory = settings_factory
        self.gateway: Gateway | None = None
        """The gateway behind the client that is open right now."""

    def __call__(
        self, *, registry: ToolRegistry | None = None, **kwargs: Any
    ) -> AbstractAsyncContextManager[Client]:
        return self._connect(registry, kwargs)

    @asynccontextmanager
    async def _connect(
        self, registry: ToolRegistry | None, kwargs: dict[str, Any]
    ) -> AsyncIterator[Client]:
        setting_names = set(Settings.model_fields)
        settings = self._settings_factory(**{k: v for k, v in kwargs.items() if k in setting_names})
        client_kwargs = {k: v for k, v in kwargs.items() if k not in setting_names}
        # Server and gateway share one Settings: the gateway's settings govern safety.
        gateway = Gateway(settings, ib_factory=lambda: self._fake_ib)
        # The fake answers an order at once or never; do not wait out the real 3 s.
        gateway.orders.status_wait = 0.05
        await gateway.start()
        self.gateway = gateway
        try:
            server = build_server(settings, gateway=gateway, registry=registry)
            async with Client(server, **client_kwargs) as client:
                yield client
        finally:
            self.gateway = None
            await gateway.stop()


@pytest.fixture
def mcp_client(fake_ib: MagicMock, settings_factory: Callable[..., Settings]) -> McpClientFactory:
    """Open an in-memory MCP client on a fresh server and gateway around ``fake_ib``.

    Usage: ``async with mcp_client(profile="trading") as client: ...``. Keyword
    arguments that are Settings fields configure both the server and its gateway (one
    Settings object, so safety settings such as ``profile`` or ``max_notional`` really
    apply); ``registry`` swaps the tool registry; anything else goes to ``mcp.Client``
    (``elicitation_callback``, ``mode``...). ``mcp_client.gateway`` is the gateway
    behind the open client. Do not combine with the ``gateway`` fixture: both would
    drive the same ``fake_ib``.
    """
    return McpClientFactory(fake_ib, settings_factory)
