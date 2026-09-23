"""NewsService: providers, historical headlines, articles, headline streams and bulletins."""

from __future__ import annotations

import asyncio
import base64
import itertools
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from ib_async import Contract, NewsBulletin, NewsProvider
from ib_async.util import parseIBDatetime
from ib_async.wrapper import RequestError

from ib_gateway_mcp.config import Settings
from ib_gateway_mcp.errors import (
    IbApiError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    RequestTimeoutError,
    SubscriptionLimitError,
)
from ib_gateway_mcp.gateway import Gateway
from ib_gateway_mcp.models.common import ContractSpec
from ib_gateway_mcp.models.news import NewsBulletinSnapshot, NewsSnapshot
from ib_gateway_mcp.services import news as news_module
from ib_gateway_mcp.services.news import NEWS_BUFFER_SIZE, NewsService
from tests.fakes import (
    FIXED_TIME,
    contract_details,
    emit_error,
    historical_news,
    news_article,
    pending,
    raises,
    returns,
    stock,
)

PROVIDERS = [
    NewsProvider(code="DJNL", name="Dow Jones Newsletters"),
    NewsProvider(code="BRFG", name="Briefing.com General Market Columns"),
]
AAPL = ContractSpec(symbol="AAPL")


@pytest.fixture(autouse=True)
def _quick_open_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streams wait this long for an immediate rejection; keep tests fast."""
    monkeypatch.setattr(news_module, "_OPEN_CHECK_SECONDS", 0.02)


@pytest.fixture
def ib(fake_ib: MagicMock) -> MagicMock:
    """``fake_ib`` answering the provider list and the AAPL contract lookup."""
    fake_ib.reqNewsProvidersAsync.side_effect = returns(PROVIDERS)
    fake_ib.reqContractDetailsAsync.side_effect = returns([contract_details(stock())])
    fake_ib.client.getReqId.side_effect = itertools.count(9000).__next__
    return fake_ib


@pytest.fixture
def service(gateway: Gateway, ib: MagicMock) -> NewsService:
    return gateway.news


@pytest.fixture
async def fast_gateway(
    settings_factory: Callable[..., Settings], ib: MagicMock
) -> AsyncIterator[Gateway]:
    gw = Gateway(settings_factory(request_timeout=0.1), ib_factory=lambda: ib)
    await gw.start()
    try:
        yield gw
    finally:
        await gw.stop()


# --- providers -----------------------------------------------------------------------------


async def test_providers(service: NewsService, ib: MagicMock) -> None:
    result = await service.providers()
    assert [(p.code, p.name) for p in result.providers] == [
        ("BRFG", "Briefing.com General Market Columns"),
        ("DJNL", "Dow Jones Newsletters"),
    ]
    ib.reqNewsProvidersAsync.assert_called_once_with()


async def test_no_providers(service: NewsService, ib: MagicMock) -> None:
    ib.reqNewsProvidersAsync.side_effect = returns([])
    with pytest.raises(NotFoundError, match="no news providers"):
        await service.providers()


async def test_providers_are_serialized(service: NewsService, ib: MagicMock) -> None:
    # ib_async keys the request by the fixed string "newsProviders": two at once would
    # orphan the first caller's future, so they must run one at a time.
    active = peak = 0

    async def answer() -> list[NewsProvider]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return PROVIDERS

    ib.reqNewsProvidersAsync.side_effect = answer
    results = await asyncio.gather(*(service.providers() for _ in range(3)))
    assert all(len(result.providers) == 2 for result in results)
    assert ib.reqNewsProvidersAsync.call_count == 3
    assert peak == 1


# --- historical news -----------------------------------------------------------------------


async def test_historical_news(service: NewsService, ib: MagicMock) -> None:
    older = historical_news("Older", time=FIXED_TIME - timedelta(hours=1), articleId="BRFG$1")
    newer = historical_news(
        "{A:800015:L:en:K:-0.97:C:0.97}Newer", time=FIXED_TIME, articleId="BRFG$2"
    )
    ib.reqHistoricalNewsAsync.side_effect = returns([older, newer])
    start = datetime(2026, 1, 1)  # naive means UTC
    result = await service.historical_news(AAPL, start=start, limit=10)

    assert [h.headline for h in result.headlines] == ["Newer", "Older"]
    assert result.headlines[0].metadata == "A:800015:L:en:K:-0.97:C:0.97"
    assert result.headlines[1].metadata is None
    assert result.headlines[0].article_id == "BRFG$2"
    assert result.headlines[0].time == FIXED_TIME
    assert result.contract.con_id == 265598
    assert result.provider_codes == ["BRFG", "DJNL"]
    assert result.start == datetime(2026, 1, 1, tzinfo=UTC)
    assert result.end is None
    assert result.truncated is False
    ib.reqHistoricalNewsAsync.assert_called_once_with(
        265598, "BRFG+DJNL", datetime(2026, 1, 1, tzinfo=UTC), "", 11
    )


async def test_historical_news_times_from_ib_async_are_utc(
    service: NewsService, ib: MagicMock
) -> None:
    # ib_async's wrapper parses IBKR's "yyyy-MM-dd HH:mm:ss.0" into a naive datetime.
    naive = parseIBDatetime("2026-01-02 15:30:00.0")
    assert isinstance(naive, datetime)
    assert naive.tzinfo is None
    ib.reqHistoricalNewsAsync.side_effect = returns([historical_news(time=naive)])
    result = await service.historical_news(AAPL)
    assert result.headlines[0].time == FIXED_TIME
    assert result.headlines[0].time is not None
    assert result.headlines[0].time.tzinfo is not None


async def test_historical_news_limit(service: NewsService, ib: MagicMock) -> None:
    rows = [
        historical_news(f"H{i}", time=FIXED_TIME - timedelta(minutes=i), articleId=f"BRFG${i}")
        for i in range(4)
    ]
    ib.reqHistoricalNewsAsync.side_effect = returns(rows)
    result = await service.historical_news(AAPL, limit=3)
    assert [h.headline for h in result.headlines] == ["H0", "H1", "H2"]
    assert result.truncated is True
    assert ib.reqHistoricalNewsAsync.call_args.args[4] == 4


async def test_historical_news_caps_at_300(service: NewsService, ib: MagicMock) -> None:
    ib.reqHistoricalNewsAsync.side_effect = returns([historical_news()] * 300)
    result = await service.historical_news(AAPL, limit=1000)
    assert len(result.headlines) == 300
    assert result.truncated is True  # IBKR's cap reached: there may be more
    assert ib.reqHistoricalNewsAsync.call_args.args[4] == 300


async def test_historical_news_selected_providers(service: NewsService, ib: MagicMock) -> None:
    ib.reqHistoricalNewsAsync.side_effect = returns([historical_news()])
    end = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
    result = await service.historical_news(AAPL, provider_codes=["djnl", "DJNL"], end=end)
    assert result.provider_codes == ["DJNL"]
    assert ib.reqHistoricalNewsAsync.call_args.args[1:4] == ("DJNL", "", end)


async def test_historical_news_unknown_provider(service: NewsService, ib: MagicMock) -> None:
    with pytest.raises(InvalidRequestError, match=r"Not a subscribed news provider: BZ\. .*BRFG"):
        await service.historical_news(AAPL, provider_codes=["BZ"])
    with pytest.raises(InvalidRequestError, match="provider_codes is empty"):
        await service.historical_news(AAPL, provider_codes=[" "])
    ib.reqHistoricalNewsAsync.assert_not_called()


async def test_historical_news_needs_providers(service: NewsService, ib: MagicMock) -> None:
    ib.reqNewsProvidersAsync.side_effect = returns([])
    with pytest.raises(NotFoundError, match="no news providers"):
        await service.historical_news(AAPL)


async def test_historical_news_bad_range(service: NewsService, ib: MagicMock) -> None:
    with pytest.raises(InvalidRequestError, match="start must be before end"):
        await service.historical_news(AAPL, start=FIXED_TIME, end=FIXED_TIME)


async def test_historical_news_none_found(service: NewsService, ib: MagicMock) -> None:
    ib.reqHistoricalNewsAsync.side_effect = returns([])
    with pytest.raises(NotFoundError, match="No headlines for AAPL from BRFG\\+DJNL"):
        await service.historical_news(AAPL)


async def test_historical_news_internal_timeout(service: NewsService, ib: MagicMock) -> None:
    ib.reqHistoricalNewsAsync.side_effect = returns(None)  # ib_async gave up after 4 s
    with pytest.raises(RequestTimeoutError, match="4-second"):
        await service.historical_news(AAPL)


async def test_historical_news_error_lists_providers(service: NewsService, ib: MagicMock) -> None:
    ib.reqHistoricalNewsAsync.side_effect = raises(RequestError(9001, 10276, "News feed denied"))
    with pytest.raises(IbApiError) as info:
        await service.historical_news(AAPL)
    assert info.value.error_code == 10276
    assert "Subscribed providers: BRFG (Briefing.com" in str(info.value)


async def test_historical_news_unknown_contract(service: NewsService, ib: MagicMock) -> None:
    ib.reqContractDetailsAsync.side_effect = raises(RequestError(1, 200, "No security definition"))
    with pytest.raises(NotFoundError, match="No contract matches NOPE"):
        await service.historical_news(ContractSpec(symbol="NOPE"))


async def test_provider_list_is_cached_per_session(
    service: NewsService, gateway: Gateway, ib: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ib.reqHistoricalNewsAsync.side_effect = returns([historical_news()])
    await service.historical_news(AAPL)
    await service.historical_news(AAPL)
    assert ib.reqNewsProvidersAsync.call_count == 1
    monkeypatch.setattr(gateway.connection, "_connected_since", datetime(2030, 1, 1, tzinfo=UTC))
    await service.historical_news(AAPL)
    assert ib.reqNewsProvidersAsync.call_count == 2


# --- articles ------------------------------------------------------------------------------


async def test_article_text(service: NewsService, ib: MagicMock) -> None:
    ib.reqNewsArticleAsync.side_effect = returns(news_article("Plain article body."))
    article = await service.article(" brfg ", " BRFG$12345 ")
    assert article.text == "Plain article body."
    assert (article.provider_code, article.article_id) == ("BRFG", "BRFG$12345")
    assert (article.article_type, article.format) == ("text", "text")
    assert article.total_chars == len("Plain article body.")
    assert article.truncated is False
    assert article.note is None
    ib.reqNewsArticleAsync.assert_called_once_with("BRFG", "BRFG$12345")


HTML = (
    "<html><head><style>p {color: red}</style></head><body><h1>Title</h1>"
    "<p>First &amp; <b>bold</b>   para.</p><ul><li>one</li><li>two</li></ul>"
    "<script>alert(1)</script><p>Last</p></body></html>"
)


async def test_article_html_becomes_text(service: NewsService, ib: MagicMock) -> None:
    ib.reqNewsArticleAsync.side_effect = returns(news_article(HTML))
    article = await service.article("BRFG", "BRFG$1")
    assert article.text == "Title\n\nFirst & bold para.\n\n- one\n- two\n\nLast"
    assert article.format == "text"
    assert article.note == "Converted from HTML to plain text."


async def test_article_html_kept(service: NewsService, ib: MagicMock) -> None:
    ib.reqNewsArticleAsync.side_effect = returns(news_article(HTML))
    article = await service.article("BRFG", "BRFG$1", plain_text=False)
    assert article.text == HTML
    assert article.format == "html"


async def test_article_truncated(service: NewsService, ib: MagicMock) -> None:
    ib.reqNewsArticleAsync.side_effect = returns(news_article("x" * 500))
    article = await service.article("BRFG", "BRFG$1", max_chars=100)
    assert article.text == "x" * 100
    assert article.truncated is True
    assert article.total_chars == 500


async def test_article_binary(service: NewsService, ib: MagicMock) -> None:
    ib.reqNewsArticleAsync.side_effect = returns(news_article("QUJDRA==\n", article_type=1))
    article = await service.article("DJNL", "DJNL$1")
    assert article.article_type == "binary"
    assert article.text is None
    assert article.format is None
    assert article.binary_bytes == 4
    assert article.binary_base64 is None
    assert "PDF" in (article.note or "")


async def test_article_binary_for_library_callers(service: NewsService, ib: MagicMock) -> None:
    ib.reqNewsArticleAsync.side_effect = returns(news_article("QUJD\nRA==\n", article_type=1))
    article = await service.article("DJNL", "DJNL$1", include_binary=True, max_chars=2)
    assert article.article_type == "binary"
    assert article.binary_base64 == "QUJDRA=="
    assert base64.b64decode(article.binary_base64) == b"ABCD"
    assert article.binary_bytes == 4


async def test_article_empty(service: NewsService, ib: MagicMock) -> None:
    ib.reqNewsArticleAsync.side_effect = returns(news_article("  "))
    with pytest.raises(NotFoundError, match="no text for article BRFG\\$1"):
        await service.article("BRFG", "BRFG$1")


async def test_article_error(service: NewsService, ib: MagicMock) -> None:
    ib.reqNewsArticleAsync.side_effect = raises(RequestError(9000, 10172, "Article not found"))
    with pytest.raises(IbApiError, match="10172"):
        await service.article("BRFG", "BRFG$404")


async def test_article_needs_ids(service: NewsService, ib: MagicMock) -> None:
    with pytest.raises(InvalidRequestError):
        await service.article("BRFG", "  ")
    ib.reqNewsArticleAsync.assert_not_called()


async def test_article_timeout(fast_gateway: Gateway, ib: MagicMock) -> None:
    ib.reqNewsArticleAsync.side_effect = pending()
    with pytest.raises(RequestTimeoutError, match="news article BRFG\\$1"):
        await fast_gateway.news.article("BRFG", "BRFG$1")


# --- headline streams ----------------------------------------------------------------------


def headlines(gateway: Gateway, subscription_id: str) -> NewsSnapshot:
    return NewsSnapshot.model_validate(gateway.news._subscription_data(subscription_id).data)


def tick(ib: MagicMock, req_id: int, headline: str, stamp: int = 1_767_367_800_000) -> None:
    """A live headline as the decoder delivers it (the wrapper method is looked up per call)."""
    ib.wrapper.tickNews(req_id, stamp, "BRFG", f"BRFG${headline}", headline, "extra")


async def test_subscribe_news_for_a_contract(
    service: NewsService, gateway: Gateway, ib: MagicMock
) -> None:
    original = ib.wrapper.tickNews
    handle = await service.subscribe_news(AAPL)

    assert (handle.kind, handle.key) == ("news", "265598:BRFG+DJNL")
    assert handle.contract is not None
    assert handle.contract.con_id == 265598
    req_id, contract, generic, snapshot, regulatory, options = ib.client.reqMktData.call_args.args
    assert (req_id, generic, snapshot, regulatory, options) == (
        9000,
        "mdoff,292:BRFG+DJNL",
        False,
        False,
        [],
    )
    assert contract.conId == 265598
    ib.reqMktData.assert_not_called()  # no Ticker: a quote stream keeps its own

    tick(ib, 9000, "{L:en}Apple news")
    tick(ib, 4242, "Someone else's headline")
    assert original.call_count == 2  # ib_async's own handler still runs (tickNewsEvent)
    assert original.call_args_list[0].args[:3] == (9000, 1_767_367_800_000, "BRFG")
    data = headlines(gateway, handle.subscription_id)
    assert [h.headline for h in data.headlines] == ["Apple news"]
    assert data.headlines[0].metadata == "L:en"
    assert data.headlines[0].time == datetime.fromtimestamp(1_767_367_800, UTC)
    assert data.headlines[0].extra_data == "extra"
    assert (data.received, data.broad_tape, data.provider_codes) == (1, False, ["BRFG", "DJNL"])
    assert data.contract is not None
    assert data.error is None

    await gateway.subscriptions.remove(handle.subscription_id)
    ib.client.cancelMktData.assert_called_once_with(9000)
    tick(ib, 9000, "After cancel")  # no longer routed, no error


async def test_subscribe_news_one_provider(service: NewsService, ib: MagicMock) -> None:
    handle = await service.subscribe_news(AAPL, provider_code="djnl")
    assert handle.key == "265598:DJNL"
    assert ib.client.reqMktData.call_args.args[2] == "mdoff,292:DJNL"


async def test_subscribe_news_broad_tape(
    service: NewsService, gateway: Gateway, ib: MagicMock
) -> None:
    handle = await service.subscribe_news(provider_code="brf")
    assert (handle.kind, handle.key) == ("news", "BRF:BRF_ALL")
    contract: Contract = ib.client.reqMktData.call_args.args[1]
    assert (contract.symbol, contract.secType, contract.exchange) == ("BRF:BRF_ALL", "NEWS", "BRF")
    assert ib.client.reqMktData.call_args.args[2] == "mdoff,292"
    ib.reqNewsProvidersAsync.assert_not_called()
    tick(ib, 9000, "Tape headline", stamp=1_767_367_800)  # seconds are accepted too
    data = headlines(gateway, handle.subscription_id)
    assert data.broad_tape is True
    assert data.contract is None
    assert data.headlines[0].time == datetime.fromtimestamp(1_767_367_800, UTC)


async def test_subscribe_news_needs_a_target(service: NewsService, ib: MagicMock) -> None:
    with pytest.raises(InvalidRequestError, match="Give a contract"):
        await service.subscribe_news()
    with pytest.raises(InvalidRequestError, match="not a news provider code"):
        await service.subscribe_news(provider_code="BRF ALL")
    with pytest.raises(InvalidRequestError, match="Not a subscribed news provider"):
        await service.subscribe_news(AAPL, provider_code="BZ")
    ib.client.reqMktData.assert_not_called()


async def test_subscribe_news_deduplicates(service: NewsService, ib: MagicMock) -> None:
    first = await service.subscribe_news(AAPL)
    again = await service.subscribe_news(ContractSpec(con_id=265598))
    assert again.subscription_id == first.subscription_id
    assert again.deduplicated is True
    assert ib.client.reqMktData.call_count == 1


async def test_subscribe_news_rejected(
    service: NewsService, gateway: Gateway, ib: MagicMock
) -> None:
    listeners = len(ib.errorEvent)

    def reject(req_id: int, *_args: object) -> None:
        asyncio.get_running_loop().call_soon(
            lambda: emit_error(ib, 10276, "News feed is not allowed", req_id=req_id)
        )

    ib.client.reqMktData.side_effect = reject
    with pytest.raises(IbApiError, match="10276"):
        await service.subscribe_news(provider_code="BZ")
    ib.client.cancelMktData.assert_called_once_with(9000)
    assert gateway.subscriptions.list() == []
    assert len(ib.errorEvent) == listeners


async def test_subscribe_news_later_errors_and_notices(
    service: NewsService, gateway: Gateway, ib: MagicMock
) -> None:
    handle = await service.subscribe_news(AAPL)
    emit_error(ib, 10167, "Displaying delayed market data", req_id=9000)
    emit_error(ib, 2104, "Market data farm connection is OK", req_id=9000)
    emit_error(ib, 300, "Can't find EId", req_id=1234)
    assert headlines(gateway, handle.subscription_id).error is None
    emit_error(ib, 10197, "No market data during competing live session", req_id=9000)
    assert "10197" in (headlines(gateway, handle.subscription_id).error or "")


async def test_subscribe_news_resubscribes(
    service: NewsService, gateway: Gateway, ib: MagicMock
) -> None:
    handle = await service.subscribe_news(AAPL)
    assert await gateway.subscriptions.resubscribe_all() == 1
    assert ib.client.cancelMktData.call_args.args == (9000,)
    assert ib.client.reqMktData.call_args.args[0] == 9001
    tick(ib, 9000, "Old request")
    tick(ib, 9001, "New request")
    data = headlines(gateway, handle.subscription_id)
    assert [h.headline for h in data.headlines] == ["New request"]


async def test_resubscribing_after_a_reconnect_cancels_no_stale_request_id(
    service: NewsService, gateway: Gateway, ib: MagicMock
) -> None:
    """ib_async reuses request ids in a new session: the old id may be another request."""
    await service.subscribe_news(AAPL)
    gateway.connection._session += 1  # what a reconnect does
    assert await gateway.subscriptions.resubscribe_all() == 1
    ib.client.cancelMktData.assert_not_called()
    assert ib.client.reqMktData.call_args.args[0] == 9001


async def test_headline_buffer_is_bounded(
    service: NewsService, gateway: Gateway, ib: MagicMock
) -> None:
    ib.wrapper.newsTicks = [object()] * 1005
    handle = await service.subscribe_news(provider_code="BZ")
    for index in range(NEWS_BUFFER_SIZE + 5):
        tick(ib, 9000, f"H{index}")
    data = headlines(gateway, handle.subscription_id)
    assert len(data.headlines) == NEWS_BUFFER_SIZE
    assert data.headlines[-1].headline == f"H{NEWS_BUFFER_SIZE + 4}"
    assert data.received == NEWS_BUFFER_SIZE + 5
    assert len(ib.wrapper.newsTicks) == 1000  # ib_async's own list is capped too


async def test_the_router_is_installed_once(service: NewsService, ib: MagicMock) -> None:
    await service.subscribe_news(provider_code="BZ")
    hooked = ib.wrapper.tickNews
    await service.subscribe_news(provider_code="FLY")
    assert ib.wrapper.tickNews is hooked


async def test_subscribe_news_not_connected(service: NewsService, ib: MagicMock) -> None:
    ib.client.getReqId.side_effect = ConnectionError("Not connected")
    with pytest.raises(NotConnectedError):
        await service.subscribe_news(provider_code="BZ")


async def test_subscribe_news_respects_the_cap(
    settings_factory: Callable[..., Settings], ib: MagicMock
) -> None:
    async with Gateway(settings_factory(max_subscriptions=1), ib_factory=lambda: ib) as gw:
        await gw.news.subscribe_news(provider_code="BZ")
        with pytest.raises(SubscriptionLimitError):
            await gw.news.subscribe_news(provider_code="FLY")
    assert ib.client.reqMktData.call_count == 1


# --- bulletins -----------------------------------------------------------------------------


def bulletins(gateway: Gateway, subscription_id: str) -> NewsBulletinSnapshot:
    return NewsBulletinSnapshot.model_validate(
        gateway.news._subscription_data(subscription_id).data
    )


async def test_subscribe_bulletins(service: NewsService, gateway: Gateway, ib: MagicMock) -> None:
    listeners = len(ib.newsBulletinEvent)
    handle = await service.subscribe_bulletins(all_messages=True)
    assert (handle.kind, handle.key) == ("news_bulletins", "bulletins")
    ib.reqNewsBulletins.assert_called_once_with(True)

    ib.newsBulletinEvent.emit(NewsBulletin(1, 1, " Trading update ", ""))
    ib.newsBulletinEvent.emit(NewsBulletin(2, 2, "Exchange down", "ISLAND"))
    ib.newsBulletinEvent.emit(NewsBulletin(3, 3, "Exchange back", "ISLAND"))
    ib.newsBulletinEvent.emit(NewsBulletin(4, 9, "Something new", ""))
    ib.newsBulletinEvent.emit(NewsBulletin(1, 1, "Trading update", ""))  # repeated
    data = bulletins(gateway, handle.subscription_id)
    assert [(b.msg_id, b.type) for b in data.bulletins] == [
        (1, "news"),
        (2, "exchange_unavailable"),
        (3, "exchange_available"),
        (4, "other"),
    ]
    assert data.bulletins[0].message == "Trading update"
    assert data.bulletins[0].exchange is None
    assert data.bulletins[1].exchange == "ISLAND"
    assert data.received == 4

    again = await service.subscribe_bulletins()
    assert again.deduplicated is True
    assert ib.reqNewsBulletins.call_count == 1

    assert await gateway.subscriptions.resubscribe_all() == 1
    assert ib.reqNewsBulletins.call_count == 2

    await gateway.subscriptions.remove(handle.subscription_id)
    ib.cancelNewsBulletins.assert_called_once_with()
    assert len(ib.newsBulletinEvent) == listeners


async def test_subscribe_bulletins_not_connected(
    service: NewsService, gateway: Gateway, ib: MagicMock
) -> None:
    ib.reqNewsBulletins.side_effect = ConnectionError("Not connected")
    ib.cancelNewsBulletins.side_effect = ConnectionError("Not connected")
    with pytest.raises(NotConnectedError):
        await service.subscribe_bulletins()
    assert gateway.subscriptions.list() == []
