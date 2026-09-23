"""News providers, historical headlines, articles and news bulletins.

Live headlines come from a market data request with generic tick 292 and the
``mdoff`` flag (headlines only, no prices): for one instrument with
``"mdoff,292:BRFG+DJNL"``, or for a provider's whole feed (broad tape) with the
contract ``symbol="BRF:BRF_ALL", secType="NEWS", exchange="BRF"`` and ``"mdoff,292"``.

These streams are sent with ``ib.client.reqMktData`` and their own request ids rather
than ``IB.reqMktData``, for three reasons ib_async 2.1.0 forces:

* ``IB.reqMktData`` keeps one ``Ticker`` per contract, so a news stream would take over
  the request id of a quote stream on the same instrument and orphan it;
* it hashes the contract by ``conId``, which a broad-tape ``NEWS`` contract lacks
  (``ValueError``);
* ``Wrapper.tickNews`` drops the request id, so headlines from two streams cannot be
  told apart. :class:`TickNewsRouter` wraps that callback on the wrapper instance
  to route headlines by request id (the decoder looks the method up per message).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

from ib_async import IB, Contract, HistoricalNews, NewsArticle, NewsBulletin, NewsTick

from ib_gateway_mcp._util import (
    clamp_limit,
    clean_str,
    contract_to_out,
    ensure_utc,
    is_informational,
    utc_now,
)
from ib_gateway_mcp.errors import (
    IbApiError,
    InvalidRequestError,
    NotFoundError,
    RequestTimeoutError,
)
from ib_gateway_mcp.models.common import ContractOut, ContractSpec, SubscriptionOut
from ib_gateway_mcp.models.news import (
    MAX_HISTORICAL_HEADLINES,
    BulletinType,
    NewsArticleOut,
    NewsBulletinOut,
    NewsBulletinSnapshot,
    NewsHeadline,
    NewsHeadlineList,
    NewsProviderList,
    NewsProviderOut,
    NewsSnapshot,
)
from ib_gateway_mcp.services._hooks import TickNewsRouter
from ib_gateway_mcp.services.base import BaseService, describe_spec
from ib_gateway_mcp.subscriptions import Stream

if TYPE_CHECKING:
    from ib_gateway_mcp.gateway import Gateway

__all__ = [
    "DEFAULT_ARTICLE_CHARS",
    "DEFAULT_HEADLINES",
    "MAX_ARTICLE_CHARS",
    "NEWS_BUFFER_SIZE",
    "NewsService",
]

logger = logging.getLogger(__name__)

DEFAULT_HEADLINES = 50
DEFAULT_ARTICLE_CHARS = 20_000
MAX_ARTICLE_CHARS = 200_000
NEWS_BUFFER_SIZE = 200
"""Headlines (or bulletins) a subscription keeps; older ones are dropped."""

_OPEN_CHECK_SECONDS = 1.0
"""How long a new headline stream waits for IBKR to reject it before it is handed out."""
_NON_FATAL_CODES = frozenset({10167, 10090})
_BINARY_ARTICLE = 1
_PROVIDER_CODE = re.compile(r"^[A-Z0-9][A-Z0-9_.-]*$")
_HEADLINE_TAGS = re.compile(r"^\s*\{([^{}]*)\}\s*(.*)$", re.DOTALL)
_HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(\s[^<>]*)?/?>")
_BULLETIN_TYPES: dict[int, BulletinType] = {
    1: "news",
    2: "exchange_unavailable",
    3: "exchange_available",
}
_NO_PROVIDERS = (
    "This login has no news providers available through the API. Enable the free API "
    "news feeds (for example Briefing.com General Market Columns, BRFG, and Dow Jones "
    "Newsletters, DJNL) or a paid one in IBKR's market data subscriptions."
)


# --- conversions ---------------------------------------------------------------------------


def _split_headline(text: str) -> tuple[str, str | None]:
    """Separate IBKR's ``{...}`` tag prefix from a headline."""
    match = _HEADLINE_TAGS.match(text or "")
    if match is None:
        return (text or "").strip(), None
    return match.group(2).strip(), clean_str(match.group(1))


def _tick_time(stamp: int) -> datetime | None:
    """A live headline's timestamp (epoch milliseconds; seconds are accepted too)."""
    if not stamp or stamp < 0:
        return None
    seconds = stamp / 1000 if stamp > 10**11 else stamp
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _historical_headline(item: HistoricalNews) -> NewsHeadline:
    headline, tags = _split_headline(item.headline)
    return NewsHeadline(
        time=ensure_utc(item.time) if item.time else None,
        provider_code=item.providerCode,
        article_id=item.articleId,
        headline=headline,
        metadata=tags,
    )


def _tick_headline(tick: NewsTick) -> NewsHeadline:
    headline, tags = _split_headline(tick.headline)
    return NewsHeadline(
        time=_tick_time(tick.timeStamp) or utc_now(),
        provider_code=tick.providerCode,
        article_id=tick.articleId,
        headline=headline,
        metadata=tags,
        extra_data=clean_str(tick.extraData),
    )


def _bulletin(bulletin: NewsBulletin) -> NewsBulletinOut:
    return NewsBulletinOut(
        msg_id=bulletin.msgId,
        type=_BULLETIN_TYPES.get(bulletin.msgType, "other"),
        message=(bulletin.message or "").strip(),
        exchange=clean_str(bulletin.origExchange),
        received_at=utc_now(),
    )


class _TextExtractor(HTMLParser):
    """Collects the readable text of an HTML document."""

    _BLOCKS = frozenset(
        {"p", "br", "div", "li", "tr", "table", "ul", "ol", "pre", "blockquote", "hr"}
        | {f"h{level}" for level in range(1, 7)}
    )
    _HIDDEN = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._HIDDEN:
            self._hidden += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif tag in self._BLOCKS and tag != "li":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    """Plain text from HTML: tags dropped, blocks on their own lines, spaces collapsed."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _binary_size(text: str) -> int:
    """Decoded size of a base64 payload, without decoding it."""
    payload = "".join(text.split())
    return max(0, len(payload) * 3 // 4 - payload[-2:].count("="))


def _is_fatal(code: int) -> bool:
    return code not in _NON_FATAL_CODES and not is_informational(code)


class _HeadlineStream:
    """One live headline request at IBKR (per contract or broad tape) and its buffer."""

    def __init__(
        self,
        ib: IB,
        router: TickNewsRouter,
        contract: Contract,
        generic_ticks: str,
        *,
        out: ContractOut | None,
        provider_codes: Sequence[str],
        session: Callable[[], int],
    ) -> None:
        self._ib = ib
        self._router = router
        self._session = session
        self._opened_in: int | None = None
        self._contract = contract
        self._generic_ticks = generic_ticks
        self._out = out
        self._provider_codes = list(provider_codes)
        self.headlines: deque[NewsHeadline] = deque(maxlen=NEWS_BUFFER_SIZE)
        self.received = 0
        self.error: IbApiError | None = None
        self.req_id: int | None = None
        self._rejected: asyncio.Future[IbApiError] | None = None
        self._listening = False

    def open(self) -> None:
        """Send the request (sync; raises ``ConnectionError`` when down)."""
        client = self._ib.client
        req_id = client.getReqId()
        self.req_id = req_id
        self._opened_in = self._session()
        self.error = None
        self._router.add(req_id, self._on_headline)
        if not self._listening:
            self._ib.errorEvent += self._on_error
            self._listening = True
        client.reqMktData(req_id, self._contract, self._generic_ticks, False, False, [])

    async def open_and_check(self, grace: float) -> None:
        """Send the request and give IBKR ``grace`` seconds to reject it.

        Raises:
            IbApiError: IBKR rejected the request (no news permission, bad provider...).
        """
        rejected: asyncio.Future[IbApiError] = asyncio.get_running_loop().create_future()
        self._rejected = rejected
        try:
            self.open()
            await asyncio.wait({rejected}, timeout=grace)
        finally:
            self._rejected = None
        if rejected.done():
            raise rejected.result()

    def close(self) -> None:
        """Stop the stream at IBKR (best effort) and stop listening."""
        if self._listening:
            self._ib.errorEvent -= self._on_error
            self._listening = False
        req_id, self.req_id = self.req_id, None
        if req_id is None:
            return
        self._router.remove(req_id, self._on_headline)
        if self._opened_in != self._session():
            # Sent in an earlier session: the request died with it, and ib_async reuses
            # request ids, so cancelling this id could stop another request now.
            return
        try:
            self._ib.client.cancelMktData(req_id)
        except (ConnectionError, OSError):
            logger.debug("News request %s not cancelled: disconnected", req_id)

    def reopen(self) -> None:
        """Re-request the stream after IBKR lost it (error 1101) or after a reconnect."""
        self.close()
        self.open()

    def _on_headline(self, tick: NewsTick) -> None:
        self.received += 1
        self.headlines.append(_tick_headline(tick))

    def _on_error(self, req_id: int, code: int, message: str, _contract: object) -> None:
        if req_id != self.req_id or not _is_fatal(code):
            return
        self.error = IbApiError(code, message, req_id)
        if self._rejected is not None and not self._rejected.done():
            self._rejected.set_result(self.error)

    def snapshot(self) -> NewsSnapshot:
        return NewsSnapshot(
            contract=self._out if self._out and self._out.con_id else None,
            provider_codes=self._provider_codes,
            broad_tape=self._contract.secType == "NEWS",
            headlines=list(self.headlines),
            received=self.received,
            buffer_size=NEWS_BUFFER_SIZE,
            error=str(self.error) if self.error else None,
        )


class _BulletinStream:
    """The IBKR bulletin subscription (one per connection) and its buffer."""

    def __init__(self, ib: IB, all_messages: bool) -> None:
        self._ib = ib
        self._all_messages = all_messages
        self.bulletins: deque[NewsBulletinOut] = deque(maxlen=NEWS_BUFFER_SIZE)
        self.received = 0
        self._listening = False

    def open(self) -> None:
        if not self._listening:
            self._ib.newsBulletinEvent += self._on_bulletin
            self._listening = True
        self._ib.reqNewsBulletins(self._all_messages)

    async def open_async(self) -> None:
        self.open()

    def close(self) -> None:
        if self._listening:
            self._ib.newsBulletinEvent -= self._on_bulletin
            self._listening = False
        try:
            self._ib.cancelNewsBulletins()
        except (ConnectionError, OSError):
            logger.debug("News bulletins not cancelled: disconnected")

    def _on_bulletin(self, bulletin: NewsBulletin) -> None:
        if any(seen.msg_id == bulletin.msgId for seen in self.bulletins):
            return  # IBKR repeats the day's bulletins after a re-request
        self.received += 1
        self.bulletins.append(_bulletin(bulletin))

    def snapshot(self) -> NewsBulletinSnapshot:
        return NewsBulletinSnapshot(bulletins=list(self.bulletins), received=self.received)


# --- the service ---------------------------------------------------------------------------


class NewsService(BaseService):
    """News providers, historical headlines, articles and news bulletins."""

    def __init__(self, gateway: Gateway) -> None:
        super().__init__(gateway)
        self._providers: list[NewsProviderOut] | None = None
        self._providers_session: datetime | None = None
        self._router: TickNewsRouter | None = None

    # --- providers ---------------------------------------------------------------------

    async def _fetch_providers(self) -> list[NewsProviderOut]:
        ib = self.ib
        session = self.connection.connected_since
        rows = await self._call(
            ib.reqNewsProvidersAsync, what="the news providers", exclusive="newsProviders"
        )
        providers = sorted(
            (
                NewsProviderOut(code=code, name=clean_str(row.name))
                for row in rows or []
                if (code := clean_str(row.code))
            ),
            key=lambda provider: provider.code,
        )
        self._providers, self._providers_session = providers, session
        return providers

    async def _subscribed_providers(self) -> list[NewsProviderOut]:
        """The provider list, cached for the current gateway session."""
        if self._providers is not None and self._providers_session == (
            self.connection.connected_since
        ):
            return self._providers
        return await self._fetch_providers()

    async def providers(self) -> NewsProviderList:
        """List the news providers this login can use through the API.

        Raises:
            NotFoundError: The login has no news provider subscriptions.
        """
        providers = await self._fetch_providers()
        if not providers:
            raise NotFoundError(_NO_PROVIDERS)
        return NewsProviderList(providers=providers)

    async def _resolve_provider_codes(self, codes: Sequence[str] | None) -> list[str]:
        """Check ``codes`` against the subscribed providers; None means all of them.

        Raises:
            NotFoundError: The login has no news providers.
            InvalidRequestError: A code is not one of the subscribed providers.
        """
        available = await self._subscribed_providers()
        if not available:
            raise NotFoundError(_NO_PROVIDERS)
        if codes is None:
            return [provider.code for provider in available]
        known = {provider.code.upper(): provider.code for provider in available}
        wanted = list(dict.fromkeys(code.strip().upper() for code in codes if code.strip()))
        if not wanted:
            raise InvalidRequestError(
                "provider_codes is empty; omit it to use every subscribed provider."
            )
        unknown = [code for code in wanted if code not in known]
        if unknown:
            raise InvalidRequestError(
                f"Not a subscribed news provider: {', '.join(unknown)}. This login has: "
                f"{self._describe_providers(available)}."
            )
        return [known[code] for code in wanted]

    @staticmethod
    def _describe_providers(providers: Sequence[NewsProviderOut]) -> str:
        return ", ".join(
            f"{provider.code} ({provider.name})" if provider.name else provider.code
            for provider in providers
        )

    # --- headlines and articles ------------------------------------------------------------

    async def historical_news(
        self,
        contract: ContractSpec,
        *,
        provider_codes: Sequence[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> NewsHeadlineList:
        """Return past headlines for one instrument, newest first.

        Args:
            contract: The instrument (qualified to get its conId).
            provider_codes: Providers to search; None means every subscribed one.
            start: Exclusive start of the range (naive means UTC); None for no bound.
            end: Inclusive end of the range (naive means UTC); None means now.
            limit: Headlines to return (default 50, at most 300, IBKR's cap).

        Raises:
            InvalidRequestError: start is not before end, or a provider is not subscribed.
            NotFoundError: Unknown contract, no providers, or no headlines in the range.
            RequestTimeoutError: IBKR did not answer (ib_async gives up after 4 s).
        """
        start_utc = ensure_utc(start) if start is not None else None
        end_utc = ensure_utc(end) if end is not None else None
        if start_utc and end_utc and start_utc >= end_utc:
            raise InvalidRequestError("start must be before end.")
        limit = clamp_limit(limit, default=DEFAULT_HEADLINES, maximum=MAX_HISTORICAL_HEADLINES)
        codes = await self._resolve_provider_codes(provider_codes)
        qualified = await self.qualify(contract)
        ib = self.ib
        fetch = min(limit + 1, MAX_HISTORICAL_HEADLINES)
        what = f"headlines for {describe_spec(contract)}"
        try:
            rows: Any = await self._call(
                ib.reqHistoricalNewsAsync(
                    qualified.conId, "+".join(codes), start_utc or "", end_utc or "", fetch
                ),
                what=what,
            )
        except IbApiError as exc:
            # The list fetched a moment ago; asking again could fail and hide this error.
            providers = self._describe_providers(self._providers or [])
            raise exc.with_hint(f"Subscribed providers: {providers}.") from exc
        if rows is None:
            raise RequestTimeoutError(
                f"IBKR did not send {what} within ib_async's 4-second limit. Retry, or "
                "narrow the time range or the providers."
            )
        headlines = sorted(
            (_historical_headline(row) for row in rows),
            key=lambda item: item.time or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        out = contract_to_out(qualified)
        if not headlines:
            raise NotFoundError(
                f"No headlines for {out.local_symbol or out.symbol} from "
                f"{'+'.join(codes)} in that time range. Widen the range or add providers "
                "(get_news_providers)."
            )
        truncated = len(headlines) > limit or len(headlines) >= MAX_HISTORICAL_HEADLINES
        return NewsHeadlineList(
            contract=out,
            provider_codes=codes,
            start=start_utc,
            end=end_utc,
            headlines=headlines[:limit],
            truncated=truncated,
        )

    async def article(
        self,
        provider_code: str,
        article_id: str,
        *,
        max_chars: int | None = None,
        plain_text: bool = True,
        include_binary: bool = False,
    ) -> NewsArticleOut:
        """Return the body of one article.

        Args:
            provider_code: The provider, from a headline (e.g. BRFG).
            article_id: The article id, from a headline (e.g. BRFG$12345).
            max_chars: Characters of text to return (default 20000, at most 200000).
            plain_text: Convert HTML articles to plain text.
            include_binary: For a binary article (usually a PDF), return the document
                base64-encoded in ``binary_base64`` (``base64.b64decode`` gives the
                bytes). ``max_chars`` does not apply to it. The MCP tool never sets
                this: a PDF is no use to a model as base64 text.

        Raises:
            InvalidRequestError: A blank provider code or article id.
            NotFoundError: IBKR sent an empty article.
            IbApiError: IBKR refused the request (unknown article, no subscription).
        """
        provider = provider_code.strip().upper()
        article_key = article_id.strip()
        if not provider or not article_key:
            raise InvalidRequestError("provider_code and article_id come from a headline.")
        cap = clamp_limit(max_chars, default=DEFAULT_ARTICLE_CHARS, maximum=MAX_ARTICLE_CHARS)
        ib = self.ib
        result = await self._call(
            lambda: ib.reqNewsArticleAsync(provider, article_key),
            what=f"news article {article_key}",
        )
        if not isinstance(result, NewsArticle) or not (result.articleText or "").strip():
            raise NotFoundError(
                f"IBKR returned no text for article {article_key} from {provider}. Check both "
                "come from the same headline."
            )
        if result.articleType == _BINARY_ARTICLE:
            if include_binary:
                return NewsArticleOut(
                    provider_code=provider,
                    article_id=article_key,
                    article_type="binary",
                    binary_bytes=_binary_size(result.articleText),
                    binary_base64="".join(result.articleText.split()),
                    note="A binary document (usually a PDF), base64-encoded in binary_base64.",
                )
            return NewsArticleOut(
                provider_code=provider,
                article_id=article_key,
                article_type="binary",
                binary_bytes=_binary_size(result.articleText),
                note=(
                    "This article is a binary document (usually a PDF), which is not returned; "
                    "open it in TWS or the provider's site."
                ),
            )
        text = result.articleText
        is_html = bool(_HTML_TAG.search(text))
        note = None
        if is_html and plain_text:
            text = _html_to_text(text)
            is_html = False
            note = "Converted from HTML to plain text."
        return NewsArticleOut(
            provider_code=provider,
            article_id=article_key,
            article_type="text",
            format="html" if is_html else "text",
            text=text[:cap],
            total_chars=len(text),
            truncated=len(text) > cap,
            note=note,
        )

    # --- streams -----------------------------------------------------------------------

    def _tick_news_router(self, ib: IB) -> TickNewsRouter:
        if self._router is None or self._router.wrapper is not ib.wrapper:
            self._router = TickNewsRouter(ib.wrapper)
        return self._router

    async def subscribe_news(
        self, contract: ContractSpec | None = None, *, provider_code: str | None = None
    ) -> SubscriptionOut:
        """Stream live headlines for one instrument, or a provider's whole feed.

        With ``contract``, headlines about that instrument from ``provider_code`` (or
        every subscribed provider). With only ``provider_code``, the provider's broad
        tape (contract ``<CODE>:<CODE>_ALL``, sec type NEWS). Either way the stream
        uses a market data line.

        Raises:
            InvalidRequestError: Neither argument, or a malformed provider code.
            NotFoundError: Unknown contract, or no news providers.
            IbApiError: IBKR rejected the stream within the first second.
            SubscriptionLimitError: No subscription slot is free.
        """
        code = provider_code.strip().upper() if provider_code else None
        if code is not None and not _PROVIDER_CODE.match(code):
            raise InvalidRequestError(f"{provider_code!r} is not a news provider code.")
        if contract is not None:
            codes = await self._resolve_provider_codes([code] if code else None)
            request = await self.qualify(contract)
            out: ContractOut | None = contract_to_out(request)
            generic_ticks = "mdoff,292:" + "+".join(codes)
            key = f"{request.conId}:{'+'.join(codes)}"
            what = f"live headlines for {describe_spec(contract)}"
        elif code is not None:
            codes = [code]
            request = Contract(symbol=f"{code}:{code}_ALL", secType="NEWS", exchange=code)
            out = contract_to_out(request)
            generic_ticks = "mdoff,292"
            key = f"{code}:{code}_ALL"
            what = f"the {code} news feed"
        else:
            raise InvalidRequestError(
                "Give a contract (headlines about one instrument) or a provider_code (that "
                "provider's whole feed)."
            )
        ib = self.ib
        router = self._tick_news_router(ib)

        async def opener() -> Stream:
            stream = _HeadlineStream(
                ib,
                router,
                request,
                generic_ticks,
                out=out,
                provider_codes=codes,
                session=self._session,
            )
            try:
                await self._call(stream.open_and_check(_OPEN_CHECK_SECONDS), what=what)
            except BaseException:
                stream.close()
                raise
            return Stream(cancel=stream.close, snapshot=stream.snapshot, resubscribe=stream.reopen)

        return await self._subscribe(
            "news",
            key,
            opener=opener,
            contract=out,
            meta={"provider_codes": codes, "broad_tape": contract is None},
        )

    async def subscribe_bulletins(self, *, all_messages: bool = False) -> SubscriptionOut:
        """Stream IBKR bulletins: system messages and exchanges going down or coming back.

        IBKR has one bulletin stream per connection, so a second call returns the same
        subscription; ``all_messages`` (resend the day's earlier bulletins) only applies
        when the stream is opened.

        Raises:
            SubscriptionLimitError: No subscription slot is free.
        """
        ib = self.ib

        async def opener() -> Stream:
            stream = _BulletinStream(ib, all_messages)
            try:
                await self._call(stream.open_async(), what="news bulletins")
            except BaseException:
                stream.close()
                raise
            return Stream(cancel=stream.close, snapshot=stream.snapshot, resubscribe=stream.open)

        return await self._subscribe(
            "news_bulletins", "bulletins", opener=opener, meta={"all_messages": all_messages}
        )
