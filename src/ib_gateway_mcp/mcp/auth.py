"""Static bearer-token authentication for the streamable HTTP transport.

The SDK's resource-server auth takes a :class:`~mcp.server.auth.provider.TokenVerifier`
and :class:`~mcp.server.auth.settings.AuthSettings`. ``AuthSettings.issuer_url`` is a
required field, but the SDK only *uses* it to mount OAuth endpoints (when an
authorization-server provider is configured) and to publish protected-resource
metadata (when ``resource_server_url`` is set). With neither, a static token needs no
issuer, so a fixed placeholder is passed and nothing about it is ever served.

What this gives:

* ``/mcp`` requires ``Authorization: Bearer <IBKR_MCP_AUTH_TOKEN>``; anything else gets
  ``401`` with a JSON body and a ``WWW-Authenticate`` header (the SDK's middleware).
* The token is compared in constant time (``hmac.compare_digest``) and must be at least
  :data:`MIN_TOKEN_LENGTH` characters long.
* On a loopback host, Host and Origin headers are checked (:func:`transport_security`),
  so a web page cannot reach the server through DNS rebinding, whatever spelling of
  loopback the host uses. This matters most with ``IBKR_MCP_ALLOW_NO_AUTH``.
* Custom routes (``/healthz``, ``/readyz``) stay open, as the SDK intends for health checks.
* The authenticated principal is bound to the MCP session by the SDK, like any other
  bearer token.
"""

from __future__ import annotations

import hmac
import logging

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import SecretStr

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import ConfigurationError

__all__ = [
    "CLIENT_ID",
    "MIN_TOKEN_LENGTH",
    "StaticTokenVerifier",
    "auth_settings",
    "require_http_auth",
    "transport_security",
]

logger = logging.getLogger(__name__)

CLIENT_ID = "ib-gateway-mcp-client"
"""The client id recorded for requests authenticated with the static token."""

_PLACEHOLDER_ISSUER = "http://localhost"
"""Satisfies ``AuthSettings.issuer_url``; unused without an OAuth provider (see above)."""

MIN_TOKEN_LENGTH = 32
"""Shortest bearer token accepted; the endpoint can place real-money orders."""

_KEYGEN_ADVICE = "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class StaticTokenVerifier:
    """Accepts exactly one bearer token, compared in constant time."""

    def __init__(self, token: SecretStr | str) -> None:
        secret = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not secret:
            raise ValueError("the bearer token must not be empty")
        self._expected = secret.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an access token for a matching bearer token, or None."""
        if hmac.compare_digest(token.encode(), self._expected):
            return AccessToken(token=token, client_id=CLIENT_ID, scopes=[])
        return None


def auth_settings() -> AuthSettings:
    """SDK auth settings for a resource server that only checks a static token."""
    return AuthSettings.model_validate(
        {"issuer_url": _PLACEHOLDER_ISSUER, "resource_server_url": None}
    )


def require_http_auth(settings: Settings) -> None:
    """Refuse to serve HTTP without a token, except on loopback with an explicit opt-out.

    Raises:
        ConfigurationError: HTTP transport, no ``IBKR_MCP_AUTH_TOKEN``, and either the
            host is not loopback or ``IBKR_MCP_ALLOW_NO_AUTH`` is not set.
    """
    if settings.transport != "http":
        return
    if settings.auth_token is not None:
        length = len(settings.auth_token.get_secret_value())
        if length < MIN_TOKEN_LENGTH:
            raise ConfigurationError(
                f"IBKR_MCP_AUTH_TOKEN is {length} characters long; use at least "
                f"{MIN_TOKEN_LENGTH} random characters. {_KEYGEN_ADVICE}"
            )
        return
    if settings.allow_no_auth and settings.http_host_is_loopback:
        logger.warning(
            "Serving HTTP on %s without authentication (IBKR_MCP_ALLOW_NO_AUTH=true).",
            settings.http_host,
        )
        return
    if settings.allow_no_auth:
        raise ConfigurationError(
            f"IBKR_MCP_ALLOW_NO_AUTH only applies to loopback hosts; {settings.http_host} is not "
            "one. Set IBKR_MCP_AUTH_TOKEN (or IBKR_MCP_AUTH_TOKEN_FILE)."
        )
    raise ConfigurationError(
        "The HTTP transport needs a bearer token: set IBKR_MCP_AUTH_TOKEN (or "
        f"IBKR_MCP_AUTH_TOKEN_FILE). {_KEYGEN_ADVICE}. For local testing on 127.0.0.1 only, "
        "IBKR_MCP_ALLOW_NO_AUTH=true disables authentication."
    )


def transport_security(settings: Settings) -> TransportSecuritySettings | None:
    """Host/Origin validation (DNS-rebinding protection) for a loopback HTTP host.

    The SDK switches this on by itself only for the literal hosts ``127.0.0.1``,
    ``localhost`` and ``::1``; this covers every loopback spelling the settings accept
    (``127.0.0.2``, ``[::1]``, ``LOCALHOST``...). Non-loopback hosts return None: they
    always require the bearer token, which a rebinding page does not have, and their
    Host header (a container or DNS name) is not known in advance.
    """
    if not settings.http_host_is_loopback:
        return None
    host = settings.http_host.strip().lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # an IPv6 literal, as it appears in Host and Origin headers
    hosts = list(dict.fromkeys((host, *_LOOPBACK_HOSTS)))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*hosts, *(f"{name}:*" for name in hosts)],
        allowed_origins=[
            *(f"http://{name}" for name in hosts),
            *(f"http://{name}:*" for name in hosts),
        ],
    )
