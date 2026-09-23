"""Models for news providers, historical headlines, articles and bulletins."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ib_gateway_mcp.models.common import ContractOut, Truncatable

__all__ = [
    "MAX_HISTORICAL_HEADLINES",
    "BulletinType",
    "NewsArticleOut",
    "NewsBulletinOut",
    "NewsBulletinSnapshot",
    "NewsHeadline",
    "NewsHeadlineList",
    "NewsProviderList",
    "NewsProviderOut",
    "NewsSnapshot",
]

MAX_HISTORICAL_HEADLINES = 300
"""IBKR returns at most 300 headlines per historical news request."""

BulletinType = Literal["news", "exchange_unavailable", "exchange_available", "other"]
"""Kinds of IBKR bulletin: a regular news bulletin, or an exchange going down or coming back."""


class NewsProviderOut(BaseModel):
    """A news provider this login is subscribed to."""

    code: str = Field(description="Provider code, e.g. BRFG; pass it as provider_code(s).")
    name: str | None = Field(None, description="Provider name.")


class NewsProviderList(BaseModel):
    """The news providers available to this login through the API."""

    providers: list[NewsProviderOut]


class NewsHeadline(BaseModel):
    """One news headline; ``get_news_article`` returns the article behind it."""

    time: datetime | None = Field(None, description="When it was published (UTC).")
    provider_code: str = Field(description="Provider code, for get_news_article.")
    article_id: str = Field(description="Article id, for get_news_article.")
    headline: str
    metadata: str | None = Field(
        None,
        description=(
            "Tags IBKR puts in braces before some headlines (for example L:en for the "
            "language), without the braces; null when there are none."
        ),
    )
    extra_data: str | None = Field(None, description="Extra data IBKR sent with a live headline.")


class NewsHeadlineList(Truncatable):
    """Historical headlines for one instrument, newest first."""

    contract: ContractOut
    provider_codes: list[str] = Field(description="The providers that were searched.")
    start: datetime | None = Field(None, description="Start of the range asked for (UTC).")
    end: datetime | None = Field(None, description="End of the range asked for (UTC).")
    headlines: list[NewsHeadline]


class NewsArticleOut(Truncatable):
    """The body of one news article, cut at ``max_chars`` when longer."""

    provider_code: str
    article_id: str
    article_type: Literal["text", "binary"] = Field(
        description="text (plain text or HTML) or binary (a PDF, which is not returned)."
    )
    format: Literal["text", "html"] | None = Field(
        None,
        description="Whether text is plain text or HTML; null for binary articles.",
    )
    text: str | None = Field(None, description="The article; null for binary articles.")
    total_chars: int = Field(0, description="Length of the whole text before truncation.")
    binary_bytes: int | None = Field(
        None, description="Approximate size of a binary (PDF) article, in bytes."
    )
    binary_base64: str | None = Field(
        None,
        description=(
            "The binary document, base64-encoded as IBKR sent it. Only for library "
            "callers that ask for it (``include_binary``); the MCP tool never returns it."
        ),
    )
    note: str | None = Field(None, description="Why the text is missing or was converted.")


class NewsBulletinOut(BaseModel):
    """One IBKR bulletin: a system message or an exchange availability change."""

    msg_id: int = Field(description="IBKR's id of the bulletin.")
    type: BulletinType
    message: str
    exchange: str | None = Field(None, description="The exchange it concerns, if any.")
    received_at: datetime = Field(description="When this server received it (UTC).")


class NewsBulletinSnapshot(BaseModel):
    """IBKR bulletins received by a news_bulletins subscription, oldest first."""

    bulletins: list[NewsBulletinOut] = Field(default_factory=list)
    received: int = Field(0, description="How many bulletins arrived since the subscription began.")


class NewsSnapshot(BaseModel):
    """Headlines received by a news subscription, oldest first (the newest are kept)."""

    contract: ContractOut | None = Field(
        None, description="The instrument, for per-contract news; null for a broad tape."
    )
    provider_codes: list[str] = Field(description="Providers the stream covers.")
    broad_tape: bool = Field(description="True for a provider's whole feed.")
    headlines: list[NewsHeadline] = Field(default_factory=list)
    received: int = Field(0, description="How many headlines arrived since the subscription began.")
    buffer_size: int = Field(description="How many of the newest headlines are kept.")
    error: str | None = Field(None, description="The last error IBKR reported for this stream.")
