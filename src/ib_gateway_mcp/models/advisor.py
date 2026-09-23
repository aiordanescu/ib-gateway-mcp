"""Models for financial-advisor (FA) configuration, soft dollar tiers and family codes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, Field

from ib_gateway_mcp.models.common import Truncatable

__all__ = [
    "FA_DATA_TYPE_CODES",
    "FaAccountOut",
    "FaAliasOut",
    "FaApplyResult",
    "FaConfig",
    "FaDataType",
    "FaGroupChange",
    "FaGroupOut",
    "FaReplaceDataType",
    "FaReplacePreview",
    "FamilyCodeList",
    "FamilyCodeOut",
    "SoftDollarTierList",
    "SoftDollarTierOut",
]

FaDataType = Literal["groups", "aliases"]
"""FA configuration kinds that can be read.

IBKR's third kind, profiles (``faDataType`` 2), was merged into groups and is refused
by servers at API version 177 and later, which is every server ib_async 2.1.0 talks to.
"""

FaReplaceDataType = Literal["groups"]
"""FA configuration kinds that can be replaced."""

FA_DATA_TYPE_CODES: Mapping[FaDataType, int] = MappingProxyType({"groups": 1, "aliases": 3})
"""TWS API ``faDataType`` codes for :data:`FaDataType`."""


class FaAccountOut(BaseModel):
    """One account in an FA group."""

    account: str
    amount: float | None = Field(
        None,
        description=(
            "Allocation value for methods that take one (shares, ratio, percent or "
            "monetary amount); null otherwise."
        ),
    )


class FaGroupOut(BaseModel):
    """One FA group: accounts that orders placed for the group are allocated across."""

    name: str
    method: str | None = Field(
        None,
        description=(
            "Default allocation method, e.g. AvailableEquity, Equal, NetLiq, "
            "ContractsOrShares, Ratio, Percent or MonetaryAmount."
        ),
    )
    accounts: list[FaAccountOut] = Field(
        default_factory=list, description="Members that are in this server's account allowlist."
    )
    other_accounts: int = Field(
        0, description="Members outside the allowlist; counted, never named."
    )


class FaAliasOut(BaseModel):
    """A display name the advisor gave an account."""

    account: str
    alias: str


class FaConfig(Truncatable):
    """The FA configuration of the login, parsed from IBKR's XML.

    ``truncated`` refers to ``xml``: true when the raw XML was cut to ``max_chars``.
    """

    truncated: bool = Field(False, description="True when the raw xml was cut to max_chars.")
    data_type: FaDataType
    groups: list[FaGroupOut] = Field(
        default_factory=list, description="FA groups (data_type groups)."
    )
    aliases: list[FaAliasOut] = Field(
        default_factory=list, description="Account aliases (data_type aliases)."
    )
    other_accounts: int = Field(
        0, description="Aliased accounts outside the allowlist; counted, never named."
    )
    xml: str | None = Field(None, description="The raw XML, when requested with include_xml.")
    xml_chars: int = Field(description="Length of the full XML in characters.")


class FaGroupChange(BaseModel):
    """How one FA group differs between the current and the proposed configuration."""

    name: str
    change: Literal["added", "removed", "changed"]
    method_before: str | None = None
    method_after: str | None = None
    accounts_added: list[str] = Field(default_factory=list)
    accounts_removed: list[str] = Field(default_factory=list)
    amounts_changed: list[str] = Field(
        default_factory=list, description="Accounts whose allocation value changed."
    )


class FaReplacePreview(BaseModel):
    """A validated FA replacement, ready for ``apply_fa_config(token)``."""

    token: str = Field(description="Pass to apply_fa_config; single-use.")
    expires_at: datetime = Field(description="When the token stops working (UTC).")
    data_type: FaReplaceDataType
    accounts: list[str] = Field(description="Every account of the login; the change affects all.")
    is_paper: bool = Field(description="True when every account of the login is a paper account.")
    groups_before: int
    groups_after: int
    changes: list[FaGroupChange]
    unchanged_groups: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(
        default_factory=list, description="Things IBKR may reject or that look unintended."
    )
    summary: list[str] = Field(description="The lines a human sees when asked to confirm.")


class FaApplyResult(BaseModel):
    """The outcome of ``apply_fa_config``."""

    data_type: FaReplaceDataType
    accounts: list[str]
    is_paper: bool
    human_confirmed: bool = Field(description="True when a human approved it through the client.")
    message: str | None = Field(None, description="IBKR's confirmation text (replaceFAEnd).")
    groups_after: int
    changes: list[FaGroupChange]


class SoftDollarTierOut(BaseModel):
    """A soft dollar tier that orders can reference."""

    name: str
    value: str
    display_name: str | None = None


class SoftDollarTierList(BaseModel):
    """Soft dollar tiers available to this login."""

    tiers: list[SoftDollarTierOut]


class FamilyCodeOut(BaseModel):
    """The family code IBKR assigned to an account (linked-account grouping)."""

    account: str
    family_code: str | None = Field(None, description="Null when the account has none.")


class FamilyCodeList(BaseModel):
    """Family codes of the accounts in this server's allowlist."""

    codes: list[FamilyCodeOut]
    other_accounts: int = Field(
        0, description="Rows for accounts outside the allowlist; counted, never named."
    )
