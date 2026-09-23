"""Tool registry: the ``@ib_tool`` decorator, toolsets, profiles and tiers.

Domain modules in :mod:`ib_gateway_mcp.mcp.tools` declare tools with ``@ib_tool`` at
import time; :func:`~ib_gateway_mcp.mcp.server.build_server` then registers only the
tools whose toolset is enabled. A tool function looks like this (parameter types from
:mod:`ib_gateway_mcp.mcp.params` carry the descriptions the model sees)::

    @ib_tool("account", Tier.READ, "Positions")
    async def get_positions(
        ctx: ToolContext, account: AccountArg = None, limit: LimitArg = None
    ) -> PositionList:
        \"\"\"LLM-facing description: what it does, key parameters, limits.\"\"\"
        return await gateway_from(ctx).account.positions(account=account, limit=limit)

Rules the decorator enforces:

* the function is ``async``, has a docstring, and its annotations are real objects (no
  ``from __future__ import annotations`` in tool modules; the SDK introspects them);
* two tools never share a name, even across modules;
* ``idempotent=True`` is only for tools that are not read-only (MCP defines
  ``idempotentHint`` only for those): WRITE and ADMIN tools, and READ-tier tools
  declared with ``read_only=False``, which change this server's session (such as the
  market data type) but nothing at IBKR, so they need no trading gate;
* WRITE and ADMIN tools take a ``ctx: ToolContext`` parameter, and before their body
  runs the gateway's trading gate (``ConnectionManager.require_trading``) must pass.
  The session's ``readonly`` flag does not stop orders, so this gate (with the service
  layer's own call) is what does.

Errors from :mod:`ib_gateway_mcp.errors` raised inside a tool reach the model as tool
errors with their message, prefixed by the error's ``code``; anything else is reported
by the SDK as an internal error.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.context_injection import find_context_parameter
from mcp_types import ToolAnnotations

from ib_gateway_mcp.config import PROFILES, TOOLSETS, enabled_toolsets
from ib_gateway_mcp.errors import IbGatewayMcpError
from ib_gateway_mcp.mcp.context import gateway_from, require_trading

__all__ = [
    "PROFILES",
    "REGISTRY",
    "TOOLSETS",
    "Tier",
    "ToolRegistry",
    "ToolSpec",
    "annotations_for",
    "enabled_toolsets",
    "ib_tool",
    "tool_errors",
    "with_tool_errors",
]


class Tier(StrEnum):
    """How much a tool can change at IBKR."""

    READ = "read"
    """Reads only. Safe to call freely."""
    WRITE = "write"
    """Changes account state: orders, cancellations, FA configuration."""
    ADMIN = "admin"
    """Changes gateway or session settings (display groups, server log level)."""


def annotations_for(
    tier: Tier, title: str, *, idempotent: bool = False, read_only: bool = True
) -> ToolAnnotations:
    """Build the MCP behaviour hints for a tool of the given tier.

    ``read_only=False`` marks a READ-tier tool that changes this server's session (not
    the account), so clients do not auto-approve it as a pure read.

    Raises:
        ValueError: ``idempotent`` on a read-only tool (the hint only applies to tools
            that change state), or ``read_only=False`` on a WRITE or ADMIN tool.
    """
    if tier is Tier.READ:
        if read_only:
            if idempotent:
                raise ValueError("idempotent only applies to tools that are not read-only")
            return ToolAnnotations(title=title, read_only_hint=True, open_world_hint=True)
        return ToolAnnotations(
            title=title,
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=idempotent,
            open_world_hint=True,
        )
    if not read_only:
        raise ValueError("read_only=False is implied for WRITE and ADMIN tools")
    return ToolAnnotations(
        title=title,
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=idempotent,
        open_world_hint=True,
    )


@contextmanager
def tool_errors() -> Iterator[None]:
    """Turn :class:`IbGatewayMcpError` into the SDK's ``ToolError``.

    The SDK hides the text of unexpected exceptions from the model; our own errors are
    anticipated and actionable, so they are re-raised as ``ToolError`` with the
    error's ``code`` as a prefix (``"not_connected: ..."``).
    """
    try:
        yield
    except IbGatewayMcpError as exc:
        raise ToolError(f"{exc.code}: {exc}") from exc


def with_tool_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Wrap an async tool (or resolver) so library errors become tool errors.

    The wrapper keeps the original's name, docstring, annotations and signature
    (``functools.wraps``), which is what the SDK introspects.
    """

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with tool_errors():
            return await fn(*args, **kwargs)

    return wrapper


def with_trading_gate[**P, R](
    fn: Callable[P, Awaitable[R]], context_param: str
) -> Callable[P, Awaitable[R]]:
    """Wrap a WRITE/ADMIN tool so ``require_trading()`` passes before its body runs.

    A refusal is audited (stage ``gate``) with the tool's name.
    """

    @functools.wraps(fn)
    async def gated(*args: P.args, **kwargs: P.kwargs) -> R:
        ctx = kwargs.get(context_param)
        if ctx is None:
            raise RuntimeError(f"tool {fn.__name__} was called without its context")
        require_trading(gateway_from(cast("Context[Any, Any]", ctx)), fn.__name__)
        return await fn(*args, **kwargs)

    return gated


@dataclass(frozen=True, eq=False)
class ToolSpec:
    """A registered tool: its callable plus the metadata used to expose it.

    Specs compare and hash by identity, so a server build can cache the SDK tool made
    from each one (see :func:`~ib_gateway_mcp.mcp.server.build_server`).
    """

    name: str
    fn: Callable[..., Awaitable[Any]]
    """The callable handed to the SDK (errors mapped; WRITE/ADMIN tools gated)."""
    toolset: str
    tier: Tier
    title: str
    annotations: ToolAnnotations
    context_param: str | None = None
    """Name of the parameter that receives the MCP context, if any."""


class ToolRegistry:
    """Collects :class:`ToolSpec` objects; the module-level :data:`REGISTRY` is the default."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def tool[F: Callable[..., Awaitable[Any]]](
        self,
        toolset: str,
        tier: Tier,
        title: str,
        *,
        idempotent: bool = False,
        read_only: bool = True,
    ) -> Callable[[F], F]:
        """Decorator that registers an async tool function; returns it unchanged."""
        if toolset not in TOOLSETS:
            raise ValueError(f"unknown toolset {toolset!r}; valid: {', '.join(sorted(TOOLSETS))}")
        annotations = annotations_for(tier, title, idempotent=idempotent, read_only=read_only)

        def decorator(fn: F) -> F:
            _check_tool_function(fn)
            context_param = find_context_parameter(fn)
            body: Callable[..., Awaitable[Any]] = fn
            if tier is not Tier.READ:
                if context_param is None:
                    raise TypeError(
                        f"{tier.value} tool {fn.__qualname__} needs a 'ctx: ToolContext' "
                        "parameter: the trading gate runs on it"
                    )
                body = with_trading_gate(fn, context_param)
            spec = ToolSpec(
                name=fn.__name__,
                fn=with_tool_errors(body),
                toolset=toolset,
                tier=tier,
                title=title,
                annotations=annotations,
                context_param=context_param,
            )
            self.add(spec)
            return fn

        return decorator

    def add(self, spec: ToolSpec) -> None:
        """Register a spec; another function under the same name is an error.

        Registering the same function again (same module and qualified name, e.g. after
        a reload) replaces the old spec.
        """
        existing = self._specs.get(spec.name)
        if existing is not None and _origin(existing.fn) != _origin(spec.fn):
            module, qualname = _origin(existing.fn)
            raise ValueError(
                f"tool name {spec.name!r} is already registered by {module}.{qualname}"
            )
        self._specs[spec.name] = spec

    def specs(self) -> list[ToolSpec]:
        """Every registered tool, sorted by toolset then name."""
        return sorted(self._specs.values(), key=lambda s: (s.toolset, s.name))

    def specs_for(self, toolsets: Iterable[str]) -> list[ToolSpec]:
        """The registered tools whose toolset is in ``toolsets``."""
        wanted = set(toolsets)
        return [spec for spec in self.specs() if spec.toolset in wanted]

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)


def _origin(fn: Callable[..., Any]) -> tuple[str, str]:
    """Where the undecorated function was defined: ``(module, qualified name)``."""
    original = inspect.unwrap(fn)
    return (
        getattr(original, "__module__", "?") or "?",
        getattr(original, "__qualname__", repr(original)),
    )


def _check_tool_function(fn: Callable[..., Any]) -> None:
    name = getattr(fn, "__qualname__", repr(fn))
    if not inspect.iscoroutinefunction(fn):
        raise TypeError(f"tool {name} must be an async function")
    if not inspect.getdoc(fn):
        raise TypeError(f"tool {name} needs a docstring: it is the description the model reads")
    stringified = [
        key for key, value in inspect.get_annotations(fn).items() if isinstance(value, str)
    ]
    if stringified:
        raise TypeError(
            f"tool {name} has string annotations ({', '.join(stringified)}); do not use "
            "'from __future__ import annotations' in tool modules"
        )


REGISTRY = ToolRegistry()
"""The registry the ``@ib_tool`` decorator writes to."""


def ib_tool[F: Callable[..., Awaitable[Any]]](
    toolset: str,
    tier: Tier,
    title: str,
    *,
    idempotent: bool = False,
    read_only: bool = True,
) -> Callable[[F], F]:
    """Register an async function as an MCP tool in ``toolset`` (see the module docstring).

    Args:
        toolset: One of :data:`TOOLSETS`; decides when the tool is exposed.
        tier: READ, WRITE or ADMIN; decides the MCP behaviour hints.
        title: Short human-readable name shown by clients.
        idempotent: True when repeating the call with the same arguments has no
            additional effect (sets ``idempotentHint``). Not for read-only tools.
        read_only: False for a READ-tier tool that changes this server's session (no
            trading gate, but no ``readOnlyHint`` either).
    """
    return REGISTRY.tool(toolset, tier, title, idempotent=idempotent, read_only=read_only)
