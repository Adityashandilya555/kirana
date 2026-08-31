"""The campaign postmortem, driven through the real HTTP route.

This file exists because of a specific outage, and the outage was invisible in
the place anyone would have looked. The shopkeeper saw:

    Access to fetch at '.../campaigns/<id>/postmortem' has been blocked by
    CORS policy: No 'Access-Control-Allow-Origin' header is present

CORS was configured correctly the whole time -- a 401 from the very same route
came back WITH the header. What actually happened is that an unhandled 500 is
produced by Starlette's outermost error middleware, which sits OUTSIDE
CORSMiddleware and so sends no Access-Control-Allow-Origin. A browser has no
way to describe that except as a CORS violation, which sent the search in the
wrong direction entirely.

The 500 underneath was an ambiguous RPC. Production carried two overloads:

    get_session_audit(uuid)                -- 010_audit.sql
    get_session_audit(uuid, int, int)      -- migration 015, both page
                                              arguments DEFAULT 500/0

`advisor.postmortem` named only p_campaign_id, which matches both, so PostgREST
answered 300 Multiple Choices. The audit route named all three and had always
worked -- which is why exactly one screen was broken while the endpoint next to
it was fine.

Two lessons, and both are tests here rather than prose:

  * A caller that names a defaulted argument set matching more than one
    overload is a bug that no unit test can see, because the unit is correct.
    Assert the ARGUMENTS the rpc is called with.
  * A route that can 500 is a route whose failures reach the browser as a
    CORS error. Assert it degrades to a status the console can read.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.config import settings
from app.core.db import RpcError
from app.main import app
from app.services import advisor

KEY = "correct-horse-battery-staple"
CAMPAIGN_ID = "c1503bc7-0760-44be-8008-2d2b382aa0e5"

_CAMPAIGN = {
    "id": CAMPAIGN_ID,
    "name": "Diwali Haggle",
    "status": "committed",
    "budget_paise": 500_000,
    "spent_paise": 120_000,
    "slots_total": 24,
    "slots_redeemed": 2,
    "slots_verified": 2,
}

_FEED = {
    "items": [
        {"kind": "offer", "binding_constraint": "margin_floor_bps",
         "proposed_bps": 500, "granted_bps": 469},
        {"kind": "offer", "binding_constraint": "margin_floor_bps",
         "proposed_bps": 500, "granted_bps": 469},
    ]
}

# What the paginated function actually returns. The shape is the point: two of
# these four keys are strings that look nothing like a session.
_ENVELOPE = {
    "total": 2,
    "returned": 2,
    "offset": 0,
    "sessions": [
        {"session_id": "s1", "withheld_skus": ["SUGAR1", "OIL1L"]},
        {"session_id": "s2", "withheld_skus": []},
    ],
}


class _FakeDb:
    """Records every rpc call so the arguments can be asserted, not just the
    function names. The bug was entirely in the arguments."""

    def __init__(self, audit=_ENVELOPE, fail_audit: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._audit = audit
        self._fail_audit = fail_audit

    async def rpc(self, fn: str, args: dict):
        self.calls.append((fn, args))
        if fn == "get_campaign":
            return _CAMPAIGN
        if fn == "get_audit_feed":
            return _FEED
        if fn == "get_session_audit":
            if self._fail_audit:
                # What an ambiguous overload actually produced: PostgREST 300
                # Multiple Choices, which is transport-shaped, so it arrives as
                # RPC_FAILED rather than a plpgsql code.
                raise RpcError(
                    "get_session_audit", "RPC_FAILED",
                    "The shop's system did not respond.",
                    "300 Multiple Choices",
                )
            return self._audit
        raise AssertionError(f"unexpected rpc: {fn}")

    def args_for(self, fn: str) -> dict:
        for name, args in self.calls:
            if name == fn:
                return args
        raise AssertionError(f"{fn} was never called")


@pytest.fixture(autouse=True)
def _wiring(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "MERCHANT_API_KEY", KEY, raising=False)
    # No provider. postmortem is specified to degrade to its computed half, so
    # every assertion here is about the figures, never the prose.
    async def _no_model(prompt: str, system: str):
        return None

    monkeypatch.setattr(advisor, "_ask", _no_model)
    deps._failures.clear()
    yield
    app.dependency_overrides.clear()
    deps._failures.clear()


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False so an unhandled error surfaces as the 500 a
    # browser would actually receive -- which is the whole subject of this file.
    return TestClient(app, raise_server_exceptions=False)


def _use(db: _FakeDb) -> None:
    app.dependency_overrides[deps.get_db] = lambda: db


# ------------------------------------------------------------------ the bug --
def test_session_audit_is_called_with_all_three_arguments(client: TestClient) -> None:
    """The regression, stated as precisely as it can be.

    Naming only p_campaign_id matches both the one-argument overload and the
    three-argument one whose page arguments default, and PostgREST refuses to
    choose. Naming all three resolves to exactly one function, and keeps doing
    so after 024 drops the stale overload.
    """
    db = _FakeDb()
    _use(db)
    r = client.get(f"/api/v1/campaigns/{CAMPAIGN_ID}/postmortem",
                   headers={"X-Merchant-Key": KEY})
    assert r.status_code == 200, r.text

    args = db.args_for("get_session_audit")
    assert set(args) == {"p_campaign_id", "p_limit", "p_offset"}, (
        "a call naming only p_campaign_id is ambiguous across the two overloads"
    )


def test_envelope_is_unwrapped_not_iterated(client: TestClient) -> None:
    """The second bug, hidden behind the first.

    Iterating the envelope as if it were the old bare array yields its KEYS --
    'total', 'returned', 'offset', 'sessions' -- and the first .get() on one
    raises AttributeError on a str. Same shape as the qr-sheet outage.
    """
    db = _FakeDb()
    _use(db)
    r = client.get(f"/api/v1/campaigns/{CAMPAIGN_ID}/postmortem",
                   headers={"X-Merchant-Key": KEY})
    assert r.status_code == 200, r.text

    # One of the two sessions has withheld skus. Counting the envelope's keys
    # instead would give 4, and counting nothing would give 0.
    assert r.json()["figures"]["bound_sticker_sessions"] == 1
    assert r.json()["figures"]["conversations"] == 2


def test_bare_array_from_the_older_function_still_works(client: TestClient) -> None:
    """A database that has not had 015 applied returns the old array shape.
    Accepting both is what lets this deploy in either order."""
    db = _FakeDb(audit=[{"session_id": "s1", "withheld_skus": ["SUGAR1"]}])
    _use(db)
    r = client.get(f"/api/v1/campaigns/{CAMPAIGN_ID}/postmortem",
                   headers={"X-Merchant-Key": KEY})
    assert r.status_code == 200, r.text
    assert r.json()["figures"]["bound_sticker_sessions"] == 1


def test_missing_sessions_key_is_not_a_crash(client: TestClient) -> None:
    db = _FakeDb(audit={"total": 0, "returned": 0, "offset": 0})
    _use(db)
    r = client.get(f"/api/v1/campaigns/{CAMPAIGN_ID}/postmortem",
                   headers={"X-Merchant-Key": KEY})
    assert r.status_code == 200, r.text
    assert r.json()["figures"]["bound_sticker_sessions"] == 0


# ------------------------------------------------- what the browser sees --
def test_rpc_failure_is_503_not_an_unhandled_500(client: TestClient) -> None:
    """The reason the real cause never reached anyone.

    A 500 from an unhandled exception is generated outside CORSMiddleware and
    carries no Access-Control-Allow-Origin, so the browser calls it a CORS
    error and the response body -- the only place the real fault was named --
    is unreadable to the page. An HTTPException is raised inside that
    middleware and comes back with the header.
    """
    db = _FakeDb(fail_audit=True)
    _use(db)
    r = client.get(
        f"/api/v1/campaigns/{CAMPAIGN_ID}/postmortem",
        headers={"X-Merchant-Key": KEY, "Origin": "https://kirana-sigma.vercel.app"},
    )
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "POSTMORTEM_UNAVAILABLE"
    # The header the outage was missing.
    assert r.headers.get("access-control-allow-origin") == "https://kirana-sigma.vercel.app"


def test_degrades_to_figures_without_a_model(client: TestClient) -> None:
    """No provider must still return every computed number. The prose is the
    wrapper; the figures are the feature."""
    db = _FakeDb()
    _use(db)
    r = client.get(f"/api/v1/campaigns/{CAMPAIGN_ID}/postmortem",
                   headers={"X-Merchant-Key": KEY})
    body = r.json()
    assert body["summary_available"] is False
    assert body["summary"] is None
    assert body["figures"]["most_common_bind"] == "margin_floor_bps"
    assert body["figures"]["clamped_count"] == 2


def test_bad_key_is_401(client: TestClient) -> None:
    _use(_FakeDb())
    r = client.get(f"/api/v1/campaigns/{CAMPAIGN_ID}/postmortem",
                   headers={"X-Merchant-Key": "nope"})
    assert r.status_code == 401
