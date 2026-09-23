"""Exception hierarchy for ib-gateway-mcp.

Every error the library raises on purpose derives from :class:`IbGatewayMcpError`.
Each class carries a short, stable ``code`` (``not_connected``, ``order_limit``...) so
the MCP layer and library users can tell failures apart without parsing messages.
Messages are written to be actionable: they say what went wrong and, where possible,
what to change. They are worded for the MCP tools and name them where they point to a
next step ("check get_health"); ``docs/tools.md`` gives the library method behind each
tool.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ib_gateway_mcp.models.common import ContractOut

__all__ = [
    "AccountNotAllowedError",
    "AmbiguousContractError",
    "CircuitOpenError",
    "ConfigurationError",
    "ConfirmationDeclinedError",
    "ConfirmationUnavailableError",
    "IbApiError",
    "IbGatewayMcpError",
    "InvalidRequestError",
    "LiveTradingDisabledError",
    "NotConnectedError",
    "NotFoundError",
    "OrderLimitError",
    "RateLimitError",
    "RequestTimeoutError",
    "SafetyError",
    "SubscriptionLimitError",
    "SubscriptionNotFoundError",
    "TokenError",
    "TokenExpiredError",
    "TokenMismatchError",
    "TokenNotFoundError",
]


class IbGatewayMcpError(Exception):
    """Base class for every error raised deliberately by ib-gateway-mcp."""

    code: str = "error"


# --- connection and requests --------------------------------------------------


class NotConnectedError(IbGatewayMcpError):
    """The gateway connection is down, still starting, or was never made."""

    code = "not_connected"


class RequestTimeoutError(IbGatewayMcpError):
    """A request to the gateway did not complete within ``IB_REQUEST_TIMEOUT``."""

    code = "request_timeout"


class IbApiError(IbGatewayMcpError):
    """The TWS API reported an error tied to one request.

    Attributes:
        error_code: The TWS API error code (see IBKR's message code reference).
        error_message: The error text as reported by the gateway, unchanged.
        req_id: The request (or order) id the error was reported for, if any.
        hint: What to do about it, when the library knows (also part of ``str()``).
    """

    code = "ib_api_error"

    def __init__(self, error_code: int, error_message: str, req_id: int | None = None) -> None:
        self.error_code = error_code
        self.error_message = error_message
        self.req_id = req_id
        self.hint: str | None = None
        super().__init__(f"IB error {error_code}: {error_message}")

    def with_hint(self, hint: str | None, *, context: str | None = None) -> IbApiError:
        """A copy of this error whose message also says where it happened and what to do.

        ``error_code``, ``req_id`` and the raw ``error_message`` are kept (``hint`` is
        stored on the copy); only ``str()`` changes, to
        ``IB error <code>: <message> (<context>). <hint>``, with the gateway's trailing
        period dropped so the sentences join cleanly.
        """
        error = IbApiError(self.error_code, self.error_message, self.req_id)
        error.hint = hint
        text = self.error_message.strip().rstrip(". ")
        if context:
            text += f" ({context})"
        text += "."
        if hint:
            hint = hint.strip()
            text += f" {hint}" if hint.endswith((".", "!", "?")) else f" {hint}."
        error.args = (f"IB error {self.error_code}: {text}",)
        return error


# --- request outcomes -----------------------------------------------------------------


class NotFoundError(IbGatewayMcpError):
    """The gateway knows nothing matching the request: an unknown contract (IB error 200),
    order, article or report.

    Services raise it instead of returning an empty success, with a message that says
    what was looked up and how to find valid values.
    """

    code = "not_found"


class AmbiguousContractError(IbGatewayMcpError):
    """A contract description matches more than one instrument.

    Attributes:
        candidates: The matching contracts, as
            :class:`~ib_gateway_mcp.models.common.ContractOut` models. The message lists
            them too, so the caller can retry with a ``con_id`` or more fields.
    """

    code = "ambiguous_contract"

    def __init__(self, message: str, candidates: Sequence[ContractOut] = ()) -> None:
        self.candidates: tuple[ContractOut, ...] = tuple(candidates)
        super().__init__(message)


class InvalidRequestError(IbGatewayMcpError):
    """The arguments are well-formed but cannot be used: out of range, contradictory, or
    not supported for this instrument. The message says which argument and why.
    """

    code = "invalid_request"


# --- configuration, accounts, subscriptions -------------------------------------


class ConfigurationError(IbGatewayMcpError):
    """The server or library is configured in a way that cannot work."""

    code = "configuration_error"


class AccountNotAllowedError(IbGatewayMcpError):
    """The requested account is not in the allowlist (``IBKR_MCP_ACCOUNTS``)."""

    code = "account_not_allowed"


class SubscriptionLimitError(IbGatewayMcpError):
    """A stream cannot be opened because a cap is reached.

    Either this server's own cap (``IBKR_MCP_MAX_SUBSCRIPTIONS``) or one of IBKR's:
    simultaneous market data lines (101), market depth streams (309), tick-by-tick
    streams (10190) or scanner subscriptions. The message says which, and how to free
    a slot.
    """

    code = "subscription_limit"


class SubscriptionNotFoundError(IbGatewayMcpError):
    """No live subscription has the given id (it expired, was cancelled, or never existed)."""

    code = "subscription_not_found"


# --- safety -----------------------------------------------------------------------


class SafetyError(IbGatewayMcpError):
    """Base class for refusals by the order safety rails."""

    code = "safety_error"


class LiveTradingDisabledError(SafetyError):
    """A live account is in scope and ``IBKR_MCP_ALLOW_LIVE`` is not set."""

    code = "live_trading_disabled"


class OrderLimitError(SafetyError):
    """The order breaks a configured limit (notional, quantity, symbol or security type)."""

    code = "order_limit"


class TokenError(SafetyError):
    """Base class for preview-token failures."""

    code = "token_error"


class TokenExpiredError(TokenError):
    """The preview token is older than ``IBKR_MCP_TOKEN_TTL``; preview the order again."""

    code = "token_expired"


class TokenNotFoundError(TokenError):
    """The preview token is unknown or was already used (tokens are single-use)."""

    code = "token_not_found"


class TokenMismatchError(TokenError):
    """The stored order no longer matches its token binding (tampering or a bug)."""

    code = "token_mismatch"


class ConfirmationDeclinedError(SafetyError):
    """The human declined or cancelled the live-order confirmation."""

    code = "confirmation_declined"


class ConfirmationUnavailableError(SafetyError):
    """A live order needs human confirmation, but the MCP client cannot ask (no elicitation)."""

    code = "confirmation_unavailable"


class RateLimitError(SafetyError):
    """More orders (or previews) than their per-minute limit in the last minute."""

    code = "rate_limit"


class CircuitOpenError(SafetyError):
    """Trading is halted after repeated rejections until a human resets it.

    Without an audit file (where the open state is kept) a restart also closes it.
    """

    code = "circuit_open"
