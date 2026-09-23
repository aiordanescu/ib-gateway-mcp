"""Financial-advisor (FA) configuration, soft dollar tiers and family codes.

ib_async 2.1.0 behaviours this service works around:

* **``requestFAAsync`` is keyed by the fixed string "requestFA"** and gives up after 4 s,
  returning None. IBKR answers a login that is not an FA master (or IBroker) account
  with an error that carries no request id, or not at all. Requests run one at a time
  under the "requestFA" lock; an FA-related error during the request fails it at once,
  and None becomes a timeout that explains FA data needs an FA login.
* **``IB.replaceFA`` hides its request id and ib_async drops ``replaceFAEnd``**, so the
  service sends ``client.replaceFA`` itself and hooks ``replaceFAEnd`` to learn the
  outcome. Replacing is destructive (the whole group configuration of the login), so it
  goes through a preview token, a live-login human confirmation and the audit log.
* **``softDollarTiers`` and ``familyCodes`` are stubs** in ib_async's wrapper: the
  callbacks are hooked for the duration of one request. ``reqFamilyCodes`` has no
  request id, so one runs at a time.

FA configuration covers every account of the login, so reading the raw XML, previewing
and applying a replacement need the account allowlist to cover every managed account;
parsed reads name only allowed accounts and count the others.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final, TypedDict, cast

from ib_async import IB, FamilyCode, SoftDollarTier

from ib_gateway_mcp._util import clamp_limit, clean_str, is_informational
from ib_gateway_mcp.errors import (
    AccountNotAllowedError,
    IbGatewayMcpError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RequestTimeoutError,
)
from ib_gateway_mcp.models.advisor import (
    FA_DATA_TYPE_CODES,
    FaAccountOut,
    FaAliasOut,
    FaApplyResult,
    FaConfig,
    FaDataType,
    FaGroupChange,
    FaGroupOut,
    FamilyCodeList,
    FamilyCodeOut,
    FaReplaceDataType,
    FaReplacePreview,
    SoftDollarTierList,
    SoftDollarTierOut,
)
from ib_gateway_mcp.models.common import ConfirmationRequest
from ib_gateway_mcp.safety import AuditEvent, PreviewRecord
from ib_gateway_mcp.services._fa_xml import (
    KNOWN_FA_METHODS,
    _aliases_from,
    _diff,
    _digest,
    _Group,
    _groups_from,
    _problems,
    _root_fits,
    _summary,
    _warnings,
    _XmlError,
)
from ib_gateway_mcp.services.base import SHARED_REQUEST_KEYS, BaseService

__all__ = [
    "FA_XML_CHARS_DEFAULT",
    "FA_XML_CHARS_MAX",
    "KNOWN_FA_METHODS",
    "MAX_FA_XML_CHARS",
    "REPLACE_FA_KIND",
    "AdvisorService",
]

logger = logging.getLogger(__name__)

REPLACE_FA_KIND: Final = "replace_fa"
"""Preview-token kind of an FA replacement."""

MAX_FA_XML_CHARS: Final = 500_000
"""Largest FA XML accepted for a replacement."""

FA_XML_CHARS_DEFAULT: Final = 20_000
"""Default ``max_chars`` for the raw XML returned by :meth:`AdvisorService.fa_config`."""

FA_XML_CHARS_MAX: Final = 200_000
"""Cap on ``max_chars`` for the raw XML."""


_REQUEST_FA_KEY: Final = SHARED_REQUEST_KEYS["requestFAAsync"]
_REPLACE_FA_KEY: Final = "replaceFA"
_SOFT_DOLLAR_KEY: Final = "softDollarTiers"
_FAMILY_CODES_KEY: Final = "familyCodes"

# ib_async wrapper callbacks hooked here (names, so mypy does not see unknown attributes).
_REPLACE_FA_END: Final = "replaceFAEnd"
_SOFT_DOLLAR_TIERS: Final = "softDollarTiers"
_FAMILY_CODES: Final = "familyCodes"

_FA_TEXT = re.compile(r"\bFA\b|financial advisor|advisor", re.IGNORECASE)

_NO_FA_ANSWER: Final = (
    "The gateway sent no FA configuration within ib_async's fixed 4 s wait. FA "
    "configuration exists only on financial-advisor (FA) master and IBroker logins; for "
    "other accounts IBKR sends nothing. If this login is an FA account, retry, and check "
    "get_health."
)


# --- helpers ------------------------------------------------------------------------------


def _fa_refusal(
    req_id: int, code: int, message: str, _contract: object
) -> InvalidRequestError | None:
    """The error to raise when an error without a request id, during requestFA, refuses
    that request (FA data needs an FA login); None for any other error."""
    if req_id > 0 or is_informational(code):  # another request's, or a connection notice
        return None
    if code != 321 and not _FA_TEXT.search(message or ""):
        return None
    return InvalidRequestError(
        f"IBKR refused the FA request (IB error {code}: {message}). FA configuration is only "
        "available on financial-advisor (FA) master and IBroker logins; this login does not "
        "appear to be one."
    )


class _FaPayload(TypedDict):
    """What an FA replacement token stores (plain JSON, digested by the token store)."""

    data_type: FaReplaceDataType
    fa_data_type: int
    xml: str
    accounts: list[str]
    current_digest: str
    action: str
    details: list[str]
    warnings: list[str]
    groups_before: int
    groups_after: int
    changes: list[dict[str, Any]]


# --- the service --------------------------------------------------------------------------


class AdvisorService(BaseService):
    """Financial-advisor (FA) configuration, soft dollar tiers and family codes."""

    _fa_answer_pending: bool = False
    """A ``requestFA`` timed out; IBKR's answer to it may still arrive."""

    # --- FA configuration: reads ---------------------------------------------------------

    async def fa_config(
        self,
        data_type: FaDataType = "groups",
        *,
        include_xml: bool = False,
        max_chars: int | None = None,
    ) -> FaConfig:
        """Return the login's FA groups or account aliases, parsed from IBKR's XML.

        Only accounts in the allowlist are named; the others are counted.

        Args:
            data_type: ``"groups"`` or ``"aliases"``. IBKR merged profiles into groups.
            include_xml: Also return the raw XML (cut to ``max_chars``). It names every
                account, so the allowlist must cover every managed account.
            max_chars: Longest XML to return (default :data:`FA_XML_CHARS_DEFAULT`, cap
                :data:`FA_XML_CHARS_MAX`).

        Raises:
            InvalidRequestError: Unknown ``data_type``, or IBKR refused the request (the
                login is not an FA master or IBroker account).
            AccountNotAllowedError: ``include_xml`` with accounts outside the allowlist.
            NotFoundError: No groups (or aliases) are defined.
            RequestTimeoutError: No answer (typical for logins that are not FA accounts).
        """
        if data_type not in FA_DATA_TYPE_CODES:
            raise InvalidRequestError(
                f"Unknown FA data_type {data_type!r}; use groups or aliases (IBKR merged "
                "profiles into groups)."
            )
        if include_xml:
            self._whole_login("return the raw FA XML")
        xml = await self._fetch_fa_xml(data_type)
        if data_type == "groups":
            groups = self._gateway_groups(xml)
            if not groups:
                raise NotFoundError(
                    "This login has no FA groups defined. preview_replace_fa_config creates them."
                )
            result = FaConfig(
                data_type=data_type,
                groups=[self._group_out(group) for group in groups],
                xml_chars=len(xml),
            )
        else:
            try:
                pairs = _aliases_from(xml)
            except _XmlError as exc:
                raise IbGatewayMcpError(f"The gateway's FA alias XML is unusable: {exc}.") from None
            if not pairs:
                raise NotFoundError("This login has no account aliases defined.")
            visible = [(a, alias) for a, alias in pairs if self.accounts.is_allowed(a)]
            result = FaConfig(
                data_type=data_type,
                aliases=[FaAliasOut(account=a, alias=alias) for a, alias in visible],
                other_accounts=len(pairs) - len(visible),
                xml_chars=len(xml),
            )
        if not include_xml:
            return result
        limit = clamp_limit(max_chars, default=FA_XML_CHARS_DEFAULT, maximum=FA_XML_CHARS_MAX)
        return result.model_copy(update={"xml": xml[:limit], "truncated": len(xml) > limit})

    # --- FA configuration: replacement -----------------------------------------------------

    async def preview_replace_fa_config(
        self, xml: str, data_type: FaReplaceDataType = "groups"
    ) -> FaReplacePreview:
        """Validate a new FA group XML, diff it against the current one, and issue a token.

        Nothing changes at IBKR; :meth:`apply_fa_config` applies the token. Like an order
        preview it needs the trading gate open (a token is useless otherwise) and takes a
        slot of the preview rate limit.

        Raises:
            NotConnectedError, ConfigurationError, LiveTradingDisabledError: Writes are
                not allowed right now (``require_trading``).
            RateLimitError: Too many previews in the last minute.
            InvalidRequestError: The XML is empty, too long, not well-formed, has a DTD,
                is not a ``<ListOfGroups>``, repeats a group or an account, or matches
                the current configuration; or IBKR refused to send the current one.
            AccountNotAllowedError: The allowlist does not cover every managed account.
        """
        if data_type != "groups":
            raise InvalidRequestError("Only the FA groups configuration can be replaced.")
        try:
            self.connection.require_trading()
            self.safety.preview_limiter.acquire()
        except IbGatewayMcpError as exc:
            self._audit_rejected("", str(exc), stage="preview", code=exc.code)
            raise
        accounts = self._whole_login("replace the FA configuration")
        text = xml.strip()
        if not text:
            raise InvalidRequestError("xml is empty; pass the complete <ListOfGroups> document.")
        if len(text) > MAX_FA_XML_CHARS:
            raise InvalidRequestError(
                f"xml has {len(text):,} characters; the limit is {MAX_FA_XML_CHARS:,}."
            )
        try:
            proposed = _groups_from(text)
        except _XmlError as exc:
            raise InvalidRequestError(f"The new FA groups XML is unusable: {exc}.") from None
        problems = _problems(proposed)
        if problems:
            raise InvalidRequestError(
                "The new FA groups XML is unusable: " + "; ".join(problems) + "."
            )
        current = self._gateway_groups(await self._fetch_fa_xml("groups"))
        changes, unchanged = _diff(current, proposed)
        if not changes:
            raise InvalidRequestError(
                "The new XML defines the same groups as the current configuration; there "
                "is nothing to apply."
            )
        action, details = _summary(accounts, len(current), len(proposed), changes)
        warnings = _warnings(proposed, frozenset(accounts))
        is_paper = self.accounts.all_paper is True
        login = ",".join(accounts)
        payload: _FaPayload = {
            "data_type": data_type,
            "fa_data_type": FA_DATA_TYPE_CODES[data_type],
            "xml": text,
            "accounts": accounts,
            "current_digest": _digest(current),
            "action": action,
            "details": details,
            "warnings": warnings,
            "groups_before": len(current),
            "groups_after": len(proposed),
            "changes": [change.model_dump(mode="json") for change in changes],
        }
        issued = self.safety.previews.issue(payload, login, REPLACE_FA_KIND)
        self.safety.audit.record(
            AuditEvent.PREVIEW,
            kind=REPLACE_FA_KIND,
            account=login,
            token=issued.token,
            summary=[action, *details],
        )
        return FaReplacePreview(
            token=issued.token,
            expires_at=issued.expires_at,
            data_type=data_type,
            accounts=accounts,
            is_paper=is_paper,
            groups_before=len(current),
            groups_after=len(proposed),
            changes=changes,
            unchanged_groups=unchanged,
            warnings=warnings,
            summary=[action, *details],
        )

    def confirmation_request(self, token: str) -> ConfirmationRequest:
        """Describe what an FA replacement token would do, without consuming it.

        Used by the MCP layer to ask a human before a live login's configuration changes.

        Raises:
            TokenError: The token is unknown, used, expired or tampered with.
            InvalidRequestError: The token is not for an FA replacement, or the login's
                accounts changed since the preview.
        """
        record = self.safety.previews.peek(token)
        payload = self._fa_payload(record)
        self._same_login(payload)
        return ConfirmationRequest(
            account=record.account,
            is_paper=self.accounts.all_paper is True,
            action=payload["action"],
            details=[*payload["details"], *(f"Warning: {line}" for line in payload["warnings"])],
        )

    async def apply_fa_config(self, token: str, *, human_confirmed: bool = False) -> FaApplyResult:
        """Apply a previewed FA replacement (``replaceFA``). Destructive; audited.

        Args:
            token: From :meth:`preview_replace_fa_config`; single-use.
            human_confirmed: A human approved it. Required for live logins while
                ``IBKR_MCP_LIVE_CONFIRM`` is on.

        Raises:
            NotConnectedError, ConfigurationError, LiveTradingDisabledError: Writes are
                not allowed right now (``require_trading``).
            TokenError: The token is unknown, used, expired or tampered with.
            InvalidRequestError: Not an FA token; the login's accounts or the FA
                configuration at IBKR changed since the preview.
            ConfirmationUnavailableError: A live login without human confirmation.
            IbApiError: IBKR rejected the new configuration.
            RequestTimeoutError: IBKR did not confirm in time (it may still apply).
        """
        try:
            self.connection.require_trading()
        except IbGatewayMcpError as exc:
            self._audit_rejected("", str(exc), stage="gate", code=exc.code)
            raise
        payload = self._fa_payload(self.safety.previews.peek(token))
        accounts = self._same_login(payload)
        is_paper = self.accounts.all_paper is True
        self._require_human(
            paper=is_paper,
            human_confirmed=human_confirmed,
            refusal=(
                "Replacing the FA configuration of a live login needs a human's confirmation "
                "(IBKR_MCP_LIVE_CONFIRM is on); nothing was changed."
            ),
        )
        record = self.safety.previews.consume_record(token)
        payload = self._fa_payload(record)
        ib = self.ib
        async with self.connection.request_lock(_REPLACE_FA_KEY):
            # The token is spent from here on; every failure is audited.
            try:
                previous_xml = await self._fetch_fa_xml("groups")
                current = self._gateway_groups(previous_xml)
                if _digest(current) != payload["current_digest"]:
                    raise InvalidRequestError(
                        "The FA configuration at IBKR changed since the preview; nothing was "
                        "applied. Run preview_replace_fa_config again."
                    )
                message = await self._replace_fa(ib, payload["fa_data_type"], payload["xml"])
            except IbGatewayMcpError as exc:
                self._audit_rejected(record.account, str(exc))
                raise
        changes = [FaGroupChange.model_validate(c) for c in payload["changes"]]
        self.safety.audit.record(
            AuditEvent.REPLACE_FA,
            account=record.account,
            data_type=payload["data_type"],
            is_paper=is_paper,
            human_confirmed=human_confirmed,
            summary=[payload["action"], *payload["details"]],
            message=message,
            # Replacing is destructive: keep both documents so a human can restore.
            previous_xml=previous_xml,
            xml=payload["xml"],
        )
        return FaApplyResult(
            data_type="groups",
            accounts=accounts,
            is_paper=is_paper,
            human_confirmed=human_confirmed,
            message=message,
            groups_after=payload["groups_after"],
            changes=changes,
        )

    # --- soft dollar tiers and family codes -----------------------------------------------

    async def soft_dollar_tiers(self) -> SoftDollarTierList:
        """Return the soft dollar tiers orders can reference.

        Raises:
            NotFoundError: The login has none.
            IbApiError, RequestTimeoutError, NotConnectedError: As for ``_call``.
        """
        ib = self.ib
        async with self.connection.request_lock(_SOFT_DOLLAR_KEY):
            req_id = self._new_req_id(ib, "soft dollar tiers")

            def on_tiers(
                tiers_req_id: int, tiers: list[SoftDollarTier]
            ) -> list[SoftDollarTier] | None:
                return list(tiers) if tiers_req_id == req_id else None

            tiers = await self._hooked_request(
                req_id,
                callback=_SOFT_DOLLAR_TIERS,
                on_callback=on_tiers,
                send=lambda: ib.client.reqSoftDollarTiers(req_id),
                what="soft dollar tiers",
            )
        rows = [
            SoftDollarTierOut(
                name=tier.name, value=tier.val, display_name=clean_str(tier.displayName)
            )
            for tier in tiers
            if tier
        ]
        if not rows:
            raise NotFoundError(
                "This login has no soft dollar tiers. They are set up with IBKR for advisors "
                "and institutions; individual accounts normally have none."
            )
        return SoftDollarTierList(tiers=rows)

    async def family_codes(self) -> FamilyCodeList:
        """Return the family codes of the allowed accounts (linked-account grouping).

        Raises:
            NotFoundError: IBKR reported none for the allowed accounts.
            RequestTimeoutError, NotConnectedError: As for ``_call``.
        """
        ib = self.ib

        def on_codes(codes: list[FamilyCode]) -> list[FamilyCode]:
            return list(codes)

        async with self.connection.request_lock(_FAMILY_CODES_KEY):
            codes = await self._hooked_request(
                _FAMILY_CODES_KEY,
                callback=_FAMILY_CODES,
                on_callback=on_codes,
                send=ib.client.reqFamilyCodes,
                what="family codes",
            )
        rows: list[FamilyCodeOut] = []
        others = 0
        for code in codes:
            account = clean_str(code.accountID)
            if account is not None and self.accounts.is_allowed(account):
                rows.append(
                    FamilyCodeOut(account=account, family_code=clean_str(code.familyCodeStr))
                )
            else:
                others += 1
        if not rows:
            extra = f" ({others} rows were for other accounts)" if others else ""
            raise NotFoundError(
                f"IBKR reported no family codes for the accounts this server may use{extra}."
            )
        return FamilyCodeList(codes=rows, other_accounts=others)

    # --- internals ------------------------------------------------------------------------

    def _whole_login(self, what: str) -> list[str]:
        """The login's managed accounts, sorted; refuse unless all are allowed."""
        managed = sorted(self.accounts.managed)
        if not managed:
            raise NotConnectedError(
                f"Cannot {what}: the login's accounts are not known yet. Check get_health."
            )
        outside = [account for account in managed if not self.accounts.is_allowed(account)]
        if outside:
            raise AccountNotAllowedError(
                f"Cannot {what}: FA configuration covers every account of the login, and "
                f"{len(outside)} of its {len(managed)} accounts are outside this server's "
                "allowlist (IBKR_MCP_ACCOUNTS). Allow them all to use this tool."
            )
        return managed

    def _same_login(self, payload: _FaPayload) -> list[str]:
        """Check the login still has the accounts the preview saw."""
        accounts = self._whole_login("apply an FA configuration")
        if accounts != payload["accounts"]:
            raise InvalidRequestError(
                "The login's accounts changed since the preview; run "
                "preview_replace_fa_config again."
            )
        return accounts

    @staticmethod
    def _fa_payload(record: PreviewRecord) -> _FaPayload:
        """The payload of an FA replacement token (plain JSON from the token store)."""
        if record.kind != REPLACE_FA_KIND:
            raise InvalidRequestError(
                f"This token is for a {record.kind} preview, not an FA replacement; "
                "pass it to submit_order instead. Nothing was changed."
            )
        return cast("_FaPayload", record.payload)

    def _group_out(self, group: _Group) -> FaGroupOut:
        visible = [m for m in group.members if self.accounts.is_allowed(m.account)]
        return FaGroupOut(
            name=group.name,
            method=group.method,
            accounts=[FaAccountOut(account=m.account, amount=m.amount) for m in visible],
            other_accounts=len(group.members) - len(visible),
        )

    @staticmethod
    def _gateway_groups(xml: str) -> list[_Group]:
        try:
            return _groups_from(xml)
        except _XmlError as exc:
            raise IbGatewayMcpError(f"The gateway's FA groups XML is unusable: {exc}.") from None

    def _audit_rejected(
        self, account: str, reason: str, *, stage: str = "apply", code: str | None = None
    ) -> None:
        self.safety.audit.record(
            AuditEvent.REJECTED,
            stage=stage,
            kind=REPLACE_FA_KIND,
            account=account or None,
            code=code,
            reason=reason,
        )

    async def _fetch_fa_xml(self, data_type: FaDataType) -> str:
        """``requestFA`` for one data type, failing fast when IBKR refuses it.

        ib_async keys every ``requestFA`` by the same string and gives up after 4 s
        while the request stays open at IBKR, so a late answer to a timed-out request
        resolves the next one, possibly for another data type. After a timeout, an
        answer whose root element does not fit the requested type is dropped and the
        request sent once more.
        """
        ib = self.ib
        code = FA_DATA_TYPE_CODES[data_type]

        async def request() -> object:
            return await self._await_or_reject(lambda: ib.requestFAAsync(code), _fa_refusal)

        what = f"the FA {data_type} configuration"
        for attempt in range(2):
            result = await self._call(request, what=what, exclusive=_REQUEST_FA_KEY)
            if result is None:
                self._fa_answer_pending = True
                raise RequestTimeoutError(
                    _NO_FA_ANSWER + " A late answer to this request is recognised and "
                    "dropped by the next one."
                )
            xml = result if isinstance(result, str) else str(result)
            if self._fa_answer_pending and attempt == 0 and not _root_fits(xml, data_type):
                logger.info("Dropped a late requestFA answer; asking for the %s again", data_type)
                self._fa_answer_pending = False
                continue
            self._fa_answer_pending = False
            return xml
        raise RequestTimeoutError(_NO_FA_ANSWER)  # pragma: no cover - the loop returns

    async def _replace_fa(self, ib: IB, fa_data_type: int, xml: str) -> str | None:
        """Send ``replaceFA`` and wait for ``replaceFAEnd``; return IBKR's text."""
        req_id = self._new_req_id(ib, "the FA replacement")

        def on_end(end_req_id: int, text: str) -> str | None:
            return text if end_req_id == req_id else None

        try:
            text = await self._hooked_request(
                req_id,
                callback=_REPLACE_FA_END,
                on_callback=on_end,
                send=lambda: ib.client.replaceFA(req_id, fa_data_type, xml),
                what="the FA replacement",
            )
        except RequestTimeoutError as exc:
            raise RequestTimeoutError(
                f"{exc} IBKR may still have applied it: check get_fa_config before trying again."
            ) from exc
        return clean_str(text)
