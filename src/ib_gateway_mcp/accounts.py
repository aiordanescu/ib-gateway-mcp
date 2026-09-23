"""Which accounts this server may touch, and how records are matched to them.

A gateway login can manage several accounts (advisor logins, or a login shared between
paper and live). The server works with a **default** account and an **allowlist**:

* ``default``: ``IB_ACCOUNT``, or the only managed account. With several managed accounts
  and no ``IB_ACCOUNT`` there is no default, and account-scoped calls must name one.
* ``allowed``: ``IBKR_MCP_ACCOUNTS`` plus the default, restricted to accounts the login
  actually manages. With no allowlist, only the default is allowed.

Records from ib_async are attributed to an account the way the TWS API exposes it:
``record.account`` (positions, portfolio items, account values), then
``record.execution.acctNumber`` (fills), then ``record.order.account`` (trades).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import AccountNotAllowedError, ConfigurationError, NotConnectedError

__all__ = ["AccountScope", "account_of", "is_paper_account"]

logger = logging.getLogger(__name__)


_PAPER_PREFIX = "D"


def is_paper_account(account: str) -> bool:
    """Return True for paper-trading account ids (they start with ``D``: DU..., DF...)."""
    return account.strip().upper().startswith(_PAPER_PREFIX)


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def account_of(record: object) -> str | None:
    """Return the account a record belongs to, or None when it carries no account.

    Handles ``Position``, ``PortfolioItem``, ``AccountValue`` and ``PnL`` (``account``),
    ``Fill`` (``execution.acctNumber``) and ``Trade`` (``order.account``).
    """
    direct = _clean(getattr(record, "account", None))
    if direct:
        return direct
    execution = getattr(record, "execution", None)
    if execution is not None:
        from_execution = _clean(getattr(execution, "acctNumber", None))
        if from_execution:
            return from_execution
    order = getattr(record, "order", None)
    if order is not None:
        return _clean(getattr(order, "account", None))
    return None


class AccountScope:
    """The default account and allowlist, resolved against the login's managed accounts.

    The scope is empty until :meth:`refresh` runs with the managed accounts reported at
    connect time; before that, :meth:`resolve` raises :class:`NotConnectedError`.
    """

    def __init__(self, settings: Settings) -> None:
        self._configured_default = settings.ib_account
        self._configured_allowlist = tuple(settings.accounts_allowlist)
        self._managed: tuple[str, ...] = ()
        self._default: str | None = None
        self._allowed: frozenset[str] = frozenset()
        self._problem: str | None = None

    # --- state ------------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """True once the managed accounts are known."""
        return bool(self._managed)

    @property
    def managed(self) -> tuple[str, ...]:
        """Every account the gateway login manages, allowed or not."""
        return self._managed

    @property
    def default(self) -> str | None:
        """The account used when a call names none, or None when one must be named."""
        return self._default

    @property
    def allowed(self) -> frozenset[str]:
        """Accounts this server may read and trade."""
        return self._allowed

    @property
    def all_paper(self) -> bool | None:
        """True when every managed account is a paper account; None before connecting."""
        if not self._managed:
            return None
        return all(is_paper_account(account) for account in self._managed)

    @property
    def live_accounts_in_scope(self) -> tuple[str, ...]:
        """Live (non-paper) accounts among the allowed ones.

        When nothing is allowed (several managed accounts, no ``IB_ACCOUNT``), every
        managed account counts: the check errs on the side of treating the login as live.
        """
        pool = self._allowed or frozenset(self._managed)
        return tuple(sorted(a for a in pool if not is_paper_account(a)))

    def refresh(self, managed: Iterable[str]) -> None:
        """Recompute the default and the allowlist from the login's managed accounts."""
        self._managed = tuple(dict.fromkeys(a.strip() for a in managed if a.strip()))
        self._problem = None
        self._default = self._pick_default()
        self._allowed = self._pick_allowed()
        logger.info(
            "Account scope: %d managed, %d allowed, default %s",
            len(self._managed),
            len(self._allowed),
            "set" if self._default else "unset",
        )

    def _pick_default(self) -> str | None:
        configured = self._configured_default
        if configured:
            if configured in self._managed:
                return configured
            self._problem = (
                f"IB_ACCOUNT={configured} is not managed by this gateway login "
                f"(it manages {len(self._managed)} other account(s)). Fix IB_ACCOUNT."
            )
            logger.error("%s", self._problem)
            return None
        if len(self._managed) == 1:
            return self._managed[0]
        return None

    def _pick_allowed(self) -> frozenset[str]:
        requested = set(self._configured_allowlist)
        unknown = sorted(requested - set(self._managed))
        if unknown:
            logger.warning(
                "Ignoring %d account(s) in IBKR_MCP_ACCOUNTS that this login does not manage: %s",
                len(unknown),
                ", ".join(unknown),
            )
        allowed = requested & set(self._managed)
        if self._default:
            allowed.add(self._default)
        return frozenset(allowed)

    # --- resolution ---------------------------------------------------------------------

    def resolve(self, account: str | None = None) -> str:
        """Return the account to act on: ``account`` if given, else the default.

        Raises:
            NotConnectedError: The managed accounts are not known yet.
            ConfigurationError: No account was given and there is no default.
            AccountNotAllowedError: The account is outside the allowlist.
        """
        if not self._managed:
            raise NotConnectedError(
                "Accounts are unknown until the gateway connection is up; check get_health."
            )
        chosen = _clean(account)
        if chosen is None:
            if self._default is None:
                raise ConfigurationError(self._problem or self._missing_default_message())
            return self._default
        if chosen not in self._allowed:
            allowed = ", ".join(sorted(self._allowed)) or "none"
            raise AccountNotAllowedError(
                f"Account {chosen} is not allowed (allowed: {allowed}). "
                "Add it to IBKR_MCP_ACCOUNTS to use it."
            )
        return chosen

    def _missing_default_message(self) -> str:
        base = (
            f"This gateway login manages {len(self._managed)} accounts and IB_ACCOUNT is not "
            "set, so there is no default account."
        )
        if self._allowed:
            allowed = ", ".join(sorted(self._allowed))
            return f"{base} Pass one of the allowed accounts ({allowed}), or set IB_ACCOUNT."
        return (
            f"{base} No account is allowed either: set IB_ACCOUNT (the default account) or "
            "IBKR_MCP_ACCOUNTS (the comma-separated allowlist)."
        )

    def is_allowed(self, account: str | None) -> bool:
        """Return True when ``account`` is in the allowlist."""
        return account is not None and account in self._allowed

    def filter[T](self, records: Iterable[T], account: str | None = None) -> list[T]:
        """Keep the records that belong to ``account`` (default: the default account).

        Records without any account information are dropped: when scoping matters,
        unattributable data must not leak across accounts.
        """
        target = self.resolve(account)
        return [record for record in records if account_of(record) == target]

    def describe(self) -> Sequence[tuple[str, bool, bool]]:
        """Return ``(account, is_paper, is_default)`` for each allowed account, sorted."""
        return [
            (account, is_paper_account(account), account == self._default)
            for account in sorted(self._allowed)
        ]
