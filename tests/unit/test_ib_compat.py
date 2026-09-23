"""_ib_compat: the ib_async internals the services rely on still exist and behave."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from ib_async import Contract
from ib_async.wrapper import Wrapper

from ib_gateway_mcp import _ib_compat


@pytest.mark.parametrize("name", _ib_compat.WRAPPER_INTERNALS)
def test_every_internal_attribute_exists_on_the_pinned_wrapper(name: str) -> None:
    """A version bump that renames one of these must fail here, not inside a service."""
    wrapper = Wrapper(MagicMock())
    assert hasattr(wrapper, name)


async def test_end_request_and_pending_request_id() -> None:
    wrapper = Wrapper(MagicMock())
    future = wrapper.startReq(5)
    assert _ib_compat.pending_request_id(wrapper, future) == 5
    _ib_compat.end_request(wrapper, 5, "done")
    assert await future == "done"
    assert _ib_compat.pending_request_id(wrapper, future) is None


async def test_fail_pending_requests() -> None:
    wrapper = Wrapper(MagicMock())
    pending = wrapper.startReq(6)
    finished = wrapper.startReq(7)
    finished.set_result("ok")
    assert _ib_compat.fail_pending_requests(wrapper, "gone") == 1
    with pytest.raises(ConnectionError, match="gone"):
        await asyncio.wait_for(pending, 1)


async def test_requests_for_contract() -> None:
    wrapper = Wrapper(MagicMock())
    contract = Contract(conId=1)
    wrapper.startReq(8, contract)
    wrapper.startReq(9, Contract(conId=1))  # equal, but another object
    assert _ib_compat.requests_for_contract(wrapper, contract) == [8]
