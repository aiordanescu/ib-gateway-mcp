"""services._hooks: temporary wrapper callbacks and the tickNews router."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest
from ib_async import NewsTick, SoftDollarTier
from ib_async.wrapper import Wrapper

from ib_gateway_mcp.services import _hooks
from ib_gateway_mcp.services._hooks import TickNewsRouter, hooked


class _Wrapper:
    def softDollarTiers(self, req_id: int, tiers: list[SoftDollarTier]) -> None:
        """ib_async's stub."""


def test_hook_shadows_the_class_callback_and_goes_away() -> None:
    wrapper = _Wrapper()
    calls: list[int] = []
    with hooked(wrapper, "softDollarTiers", lambda req_id, _tiers: calls.append(req_id)):
        wrapper.softDollarTiers(1, [])
        with hooked(wrapper, "replaceFAEnd", lambda *_: None):  # not on the class at all
            assert callable(vars(wrapper)["replaceFAEnd"])
        assert "replaceFAEnd" not in vars(wrapper)
    assert calls == [1]
    assert "softDollarTiers" not in vars(wrapper)
    assert wrapper.softDollarTiers.__func__ is _Wrapper.softDollarTiers  # type: ignore[attr-defined]


def test_hooked_restores_the_class_method_of_a_real_wrapper() -> None:
    wrapper = Wrapper(MagicMock())
    calls: list[tuple[Any, ...]] = []
    with hooked(wrapper, "positionMultiEnd", lambda *args: calls.append(args)):
        wrapper.positionMultiEnd(5)
    assert calls == [(5,)]
    assert "positionMultiEnd" not in vars(wrapper)
    assert wrapper.positionMultiEnd.__func__ is Wrapper.positionMultiEnd  # type: ignore[attr-defined]


def test_hooked_restores_an_instance_attribute_and_a_test_double() -> None:
    wrapper = _Wrapper()
    own = MagicMock()
    wrapper.softDollarTiers = own  # type: ignore[method-assign]
    with hooked(wrapper, "softDollarTiers", lambda *_: None):
        assert wrapper.softDollarTiers is not own
    assert wrapper.softDollarTiers is own

    double = create_autospec(Wrapper, instance=True)
    child = double.completedOrder
    with hooked(double, "completedOrder", lambda *_: None):
        assert double.completedOrder is not child
    assert double.completedOrder is child  # still usable afterwards


def test_hooked_refuses_a_second_hook_on_the_same_callback() -> None:
    """Overlapping hooks would restore each other's handlers in the wrong order."""
    wrapper = _Wrapper()
    with hooked(wrapper, "softDollarTiers", lambda *_: None):
        with (
            pytest.raises(RuntimeError, match="already hooked"),
            hooked(wrapper, "softDollarTiers", lambda *_: None),
        ):
            pass  # pragma: no cover
        with hooked(wrapper, "familyCodes", lambda *_: None):  # another callback is fine
            pass
    with hooked(wrapper, "softDollarTiers", lambda *_: None):  # free again afterwards
        pass
    assert "softDollarTiers" not in vars(wrapper)


def test_router_remove_keeps_a_newer_route_under_a_reused_id() -> None:
    wrapper = create_autospec(Wrapper, instance=True)
    router = TickNewsRouter(wrapper)
    old: list[NewsTick] = []
    new: list[NewsTick] = []
    router.add(3, old.append)
    router.add(3, new.append)  # a later session reused request id 3
    router.remove(3, old.append)
    wrapper.tickNews(3, 1, "BRFG", "a1", "headline", "")
    assert len(new) == 1
    assert old == []


def test_tick_news_router_routes_by_request_id_and_caps_the_cache() -> None:
    wrapper = create_autospec(Wrapper, instance=True)
    original = wrapper.tickNews
    wrapper.newsTicks = [object()] * (_hooks.WRAPPER_NEWS_TICKS + 5)
    router = TickNewsRouter(wrapper)
    seen: list[NewsTick] = []
    router.add(7, seen.append)

    wrapper.tickNews(7, 1_700_000_000_000, "BRFG", "BRFG$1", "Headline", "")
    wrapper.tickNews(8, 1_700_000_000_000, "BRFG", "BRFG$2", "Other", "")
    router.remove(7)
    wrapper.tickNews(7, 1_700_000_000_000, "BRFG", "BRFG$3", "Late", "")

    assert [tick.articleId for tick in seen] == ["BRFG$1"]
    assert original.call_count == 3  # ib_async's own handler still runs
    assert len(wrapper.newsTicks) == _hooks.WRAPPER_NEWS_TICKS


def test_a_failing_route_is_logged_not_raised() -> None:
    wrapper = create_autospec(Wrapper, instance=True)
    router = TickNewsRouter(wrapper)

    def broken(_tick: NewsTick) -> None:
        raise RuntimeError("boom")

    router.add(3, broken)
    wrapper.tickNews(3, 1_700_000_000_000, "BRFG", "BRFG$1", "Headline", "")
