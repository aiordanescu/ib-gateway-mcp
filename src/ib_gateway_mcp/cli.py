"""Command line entry point: ``ib-gateway-mcp`` (or ``python -m ib_gateway_mcp``).

Every option has an environment-variable equivalent (see :mod:`ib_gateway_mcp.config`);
command line flags win. Logs go to stderr, because the stdio transport owns stdout.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from typing import Any, get_args

from pydantic import ValidationError

from ib_gateway_mcp import __version__
from ib_gateway_mcp.config import PROFILES, TOOLSETS, LogLevel, Settings, Transport
from ib_gateway_mcp.errors import ConfigurationError
from ib_gateway_mcp.safety.audit import AUDIT_LOGGER_NAME

__all__ = ["build_parser", "main"]

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="ib-gateway-mcp",
        description=(
            "MCP server for the Interactive Brokers TWS API. Connects to an IB Gateway "
            "(IB_HOST, IB_PORT, IB_CLIENT_ID) and serves MCP tools over stdio or HTTP."
        ),
        epilog="Environment variables configure everything else; see the README.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--transport",
        choices=get_args(Transport),
        help="MCP transport (env IBKR_MCP_TRANSPORT, default stdio). http serves /mcp.",
    )
    parser.add_argument(
        "--host", dest="http_host", help="HTTP listen address (env IBKR_MCP_HTTP_HOST)."
    )
    parser.add_argument(
        "--port", dest="http_port", type=int, help="HTTP listen port (env IBKR_MCP_HTTP_PORT)."
    )
    parser.add_argument(
        "--profile",
        choices=list(PROFILES),
        help="Toolset bundle (env IBKR_MCP_PROFILE, default readonly).",
    )
    parser.add_argument(
        "--toolsets",
        metavar="NAMES",
        help=(
            "Comma-separated toolsets, overriding the profile (env IBKR_MCP_TOOLSETS). "
            "ops is always on; scanners, news and admin also turn on market_data (its "
            f"subscription tools). Choices: {', '.join(sorted(TOOLSETS))}."
        ),
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=get_args(LogLevel),
        help="Log level (env IBKR_MCP_LOG_LEVEL, default INFO).",
    )
    return parser


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    names = ("transport", "http_host", "http_port", "profile", "toolsets", "log_level")
    return {name: getattr(args, name) for name in names if getattr(args, name) is not None}


def configure_logging(level: str) -> None:
    """Send logs to stderr at ``level``; keep ib_async's chatter at WARNING unless debugging.

    The audit logger keeps INFO whatever ``level`` is: its lines are the audit trail.
    """
    logging.basicConfig(level=level, format=_LOG_FORMAT, stream=sys.stderr)
    logging.getLogger(AUDIT_LOGGER_NAME).setLevel(logging.INFO)
    if level != "DEBUG":
        logging.getLogger("ib_async").setLevel(logging.WARNING)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, load settings and run the server. Returns the exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings(**_overrides(args))
    except ValidationError as exc:
        parser.exit(2, f"ib-gateway-mcp: invalid configuration:\n{exc}\n")

    configure_logging(settings.log_level)

    from ib_gateway_mcp.mcp.server import run  # noqa: PLC0415  (keeps --help/--version fast)

    try:
        run(settings)
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0
