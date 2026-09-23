"""News tools: providers, historical headlines, articles and bulletins.

Tools here follow :mod:`ib_gateway_mcp.mcp.tools.ops` and use the parameter types in
:mod:`ib_gateway_mcp.mcp.params`.
"""

from datetime import datetime
from typing import Annotated

from pydantic import Field

from ib_gateway_mcp.mcp.context import ToolContext, gateway_from
from ib_gateway_mcp.mcp.params import ContractArg, LimitArg
from ib_gateway_mcp.mcp.registry import Tier, ib_tool
from ib_gateway_mcp.models.common import ContractSpec, SubscriptionOut
from ib_gateway_mcp.models.news import (
    NewsArticleOut,
    NewsHeadlineList,
    NewsProviderList,
)
from ib_gateway_mcp.services.news import DEFAULT_ARTICLE_CHARS, MAX_ARTICLE_CHARS

ProviderCodesArg = Annotated[
    list[str] | None,
    Field(
        description=(
            'Provider codes to search, e.g. ["BRFG", "DJNL"] (from get_news_providers). '
            "Omit for every subscribed provider."
        )
    ),
]
StartArg = Annotated[
    datetime | None,
    Field(description="Start of the range, ISO 8601 (no time zone means UTC). Omit for none."),
]
EndArg = Annotated[
    datetime | None,
    Field(description="End of the range, ISO 8601 (no time zone means UTC). Omit for now."),
]
ProviderCodeArg = Annotated[
    str, Field(min_length=1, description="Provider code from the headline, e.g. BRFG.")
]
ArticleIdArg = Annotated[
    str, Field(min_length=1, description="Article id from the headline, e.g. BRFG$12345.")
]
MaxCharsArg = Annotated[
    int,
    Field(
        ge=100,
        le=MAX_ARTICLE_CHARS,
        description=(
            f"Longest article text to return, in characters (at most {MAX_ARTICLE_CHARS:,}); "
            "a longer article is cut and truncated is true."
        ),
    ),
]
PlainTextArg = Annotated[
    bool, Field(description="Convert HTML articles to plain text (fewer characters).")
]
AllMessagesArg = Annotated[
    bool,
    Field(description="Also send the bulletins already issued today, not only new ones."),
]
OptionalContractArg = Annotated[
    ContractSpec | None,
    Field(
        description=(
            "The instrument whose headlines to stream (a con_id alone is unambiguous). "
            "Omit to stream a provider's whole feed instead."
        )
    ),
]
StreamProviderArg = Annotated[
    str | None,
    Field(
        description=(
            "Provider code. With a contract: only this provider's headlines (default: every "
            "subscribed provider). Without a contract: that provider's whole feed "
            "(broad tape), e.g. BRF, BZ or FLY."
        )
    ),
]


@ib_tool("news", Tier.READ, "News providers")
async def get_news_providers(ctx: ToolContext) -> NewsProviderList:
    """List the news providers this IBKR login can use through the API (code and name).

    Use the codes with get_historical_news, get_news_article and subscribe_news. Only
    subscribed providers are listed; the free API feeds (BRFG Briefing.com General
    Market Columns, BRFUPDN Briefing.com Analyst Actions, DJNL Dow Jones Newsletters)
    must still be enabled in IBKR's market data subscriptions. Errors: not_found when
    the login has none.
    """
    return await gateway_from(ctx).news.providers()


@ib_tool("news", Tier.READ, "Historical headlines")
async def get_historical_news(
    ctx: ToolContext,
    contract: ContractArg,
    *,
    provider_codes: ProviderCodesArg = None,
    start: StartArg = None,
    end: EndArg = None,
    limit: LimitArg = None,
) -> NewsHeadlineList:
    """Return past news headlines about one instrument, newest first.

    Each headline has provider_code and article_id for get_news_article, plus the
    publication time (UTC). Works for stocks and other instruments IBKR tags news
    with. Default limit 50, at most 300 (IBKR's cap per request); for more, move `end`
    back to the oldest time returned. Needs a subscription to each provider searched
    (see get_news_providers).
    Errors: invalid_request for a provider the login is not subscribed to; not_found
    when there are no headlines in the range (widen it); request_timeout when IBKR
    does not answer within 4 seconds (retry with a narrower range).
    """
    return await gateway_from(ctx).news.historical_news(
        contract, provider_codes=provider_codes, start=start, end=end, limit=limit
    )


@ib_tool("news", Tier.READ, "News article")
async def get_news_article(
    ctx: ToolContext,
    provider_code: ProviderCodeArg,
    article_id: ArticleIdArg,
    max_chars: MaxCharsArg = DEFAULT_ARTICLE_CHARS,
    plain_text: PlainTextArg = True,
) -> NewsArticleOut:
    """Return the full text of a news article, given its provider code and article id.

    Take both from a headline (get_historical_news or a news subscription). HTML
    articles are converted to plain text unless plain_text is false. Text longer than
    max_chars (default 20000) is cut and truncated is true; total_chars gives the full
    length. Binary articles (PDFs) are reported with article_type=binary and their
    size, not returned. Needs a subscription to the provider.
    Errors: ib_api_error with IBKR's message when the article is unknown or not
    permitted; not_found when IBKR sends an empty article.
    """
    return await gateway_from(ctx).news.article(
        provider_code, article_id, max_chars=max_chars, plain_text=plain_text
    )


@ib_tool("news", Tier.READ, "Stream IBKR bulletins")
async def subscribe_news_bulletins(
    ctx: ToolContext, all_messages: AllMessagesArg = False
) -> SubscriptionOut:
    """Stream IBKR's system bulletins: notices and exchanges becoming unavailable or available.

    Returns a handle; read the bulletins with get_subscription_data(subscription_id)
    (each has type news, exchange_unavailable or exchange_available, the message and the
    exchange). There is one bulletin stream per gateway connection, so calling this
    again returns the same handle, and all_messages only counts the first time. Stop it
    with unsubscribe; it is cancelled after idle_ttl_s seconds without a read.
    """
    return await gateway_from(ctx).news.subscribe_bulletins(all_messages=all_messages)


@ib_tool("news", Tier.READ, "Stream news headlines")
async def subscribe_news(
    ctx: ToolContext,
    contract: OptionalContractArg = None,
    provider_code: StreamProviderArg = None,
) -> SubscriptionOut:
    """Stream live news headlines for one instrument, or a provider's whole feed.

    Give a contract for headlines about that instrument (from provider_code, or every
    subscribed provider), or only a provider_code for that provider's broad tape (all
    its headlines, e.g. BRF for Briefing Trader, BZ for Benzinga, FLY for The Fly;
    each needs its own subscription). Returns a handle; read the headlines (oldest
    first, the newest 200 kept) with get_subscription_data(subscription_id) and fetch
    a full story with get_news_article. Uses one market data line. Stop it with
    unsubscribe; it is cancelled after idle_ttl_s seconds without a read.
    Errors: invalid_request without a contract or provider_code, or when a contract is
    given with a provider the login is not subscribed to (see get_news_providers);
    ib_api_error when IBKR refuses the stream (no news permission for that provider).
    """
    return await gateway_from(ctx).news.subscribe_news(contract, provider_code=provider_code)
