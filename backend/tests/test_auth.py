"""The merchant key, and the throttle around it.

The throttle is the interesting part. A first version counted failures in a
single process-wide deque, which meant any unauthenticated caller could send
_FAIL_LIMIT bad requests and lock the shopkeeper out of their own console for
the whole window -- converting a brute-force defence into a trivial denial of
service. These tests pin the property that fix depends on: buckets are keyed by
caller, so one client can only ever throttle itself.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import deps
from app.core.config import settings

KEY = "correct-horse-battery-staple"


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Enough of a Request for _caller(): headers and .client."""

    def __init__(self, ip: str = "10.0.0.1", forwarded: str | None = None) -> None:
        self.headers = {"x-forwarded-for": forwarded} if forwarded else {}
        self.client = _FakeClient(ip)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "MERCHANT_API_KEY", KEY, raising=False)
    deps._failures.clear()
    yield
    deps._failures.clear()


async def _attempt(request: _FakeRequest, key: str) -> int:
    """Returns the HTTP status a call would produce; 200 on success."""
    try:
        await deps.require_merchant_key(request, key)  # type: ignore[arg-type]
        return 200
    except HTTPException as exc:
        return exc.status_code


# ------------------------------------------------------------------- basics --
@pytest.mark.asyncio
async def test_correct_key_is_accepted() -> None:
    assert await _attempt(_FakeRequest(), KEY) == 200


@pytest.mark.asyncio
async def test_wrong_key_is_refused() -> None:
    assert await _attempt(_FakeRequest(), "nope") == 401


@pytest.mark.asyncio
async def test_unset_key_refuses_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """The setting defaults to empty so an unconfigured deploy denies rather
    than accepting a default published in this repository. Note it must not
    accept the empty string either."""
    monkeypatch.setattr(settings, "MERCHANT_API_KEY", "", raising=False)
    assert await _attempt(_FakeRequest(), "") == 503
    assert await _attempt(_FakeRequest(), "anything") == 503


# ----------------------------------------------------------------- throttle --
@pytest.mark.asyncio
async def test_repeated_failures_throttle_that_caller() -> None:
    attacker = _FakeRequest(forwarded="203.0.113.9")
    for _ in range(deps._FAIL_LIMIT):
        assert await _attempt(attacker, "wrong") == 401
    assert await _attempt(attacker, "wrong") == 429


@pytest.mark.asyncio
async def test_one_caller_cannot_lock_out_another() -> None:
    """The regression this file exists for.

    With a global counter, the merchant's next request after an attacker's
    burst returned 429 -- the console went down for everyone, on demand, from
    an unauthenticated caller.
    """
    attacker = _FakeRequest(forwarded="203.0.113.9")
    merchant = _FakeRequest(forwarded="198.51.100.4")

    for _ in range(deps._FAIL_LIMIT * 2):
        await _attempt(attacker, "wrong")

    assert await _attempt(attacker, "wrong") == 429, "attacker should be throttled"
    assert await _attempt(merchant, KEY) == 200, "merchant must still get through"


@pytest.mark.asyncio
async def test_success_clears_that_callers_history() -> None:
    """A shopkeeper who fat-fingers a paste twice and then gets it right should
    not carry those failures toward a lockout."""
    who = _FakeRequest(forwarded="198.51.100.4")
    for _ in range(deps._FAIL_LIMIT - 1):
        await _attempt(who, "wrong")
    assert await _attempt(who, KEY) == 200
    assert who.headers["x-forwarded-for"] not in deps._failures


@pytest.mark.asyncio
async def test_tracked_callers_are_bounded() -> None:
    """A fix that grows a map per spoofed IP would re-introduce the memory
    exhaustion it is meant to prevent."""
    for i in range(deps._MAX_TRACKED + 200):
        await _attempt(_FakeRequest(forwarded=f"198.51.100.{i}"), "wrong")
    assert len(deps._failures) <= deps._MAX_TRACKED


@pytest.mark.asyncio
async def test_forwarded_for_takes_the_first_hop() -> None:
    """Railway appends proxies; the client is the leftmost entry."""
    req = _FakeRequest(forwarded="203.0.113.9, 10.1.1.1, 10.1.1.2")
    await _attempt(req, "wrong")
    assert "203.0.113.9" in deps._failures
