"""The printable sheet, driven through the real HTTP route.

This file exists because of a specific outage. `qr_sheet` is the only place
that calls `require_merchant_key` as a plain function instead of a dependency
-- it has to, because the key arrives as a query param on a URL opened in a
print tab, not as a header. When the throttle was scoped per caller,
`require_merchant_key` gained `request: Request` as its FIRST parameter, and
this call site still read:

    await require_merchant_key(k)

so the key string was bound to `request`, and `_caller` reached for
`request.headers` on a `str`. Every print produced
`AttributeError: 'str' object has no attribute 'headers'` -> 500.

Nothing caught it. test_auth.py exercises `require_merchant_key(request, key)`
directly with a fake request, so the function's own contract stayed green while
the one caller that had to pass those arguments by hand was broken. The unit
test could not fail, because the unit was fine.

The lesson is the shape of the test, not the assertion: this is the first test
in the suite that drives a route through the ASGI app. A route whose auth is
wired by hand needs at least one test that actually calls the route.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.config import settings
from app.main import app

KEY = "correct-horse-battery-staple"
CAMPAIGN_ID = "11111111-2222-3333-4444-555555555555"

# Mirrors what sql/002_functions.sql:get_campaign actually builds, including the
# nested merchant object. A thinner stub passes the auth assertions and then
# dies in the template, which would make this file test the wrong thing.
_CAMPAIGN = {
    "id": CAMPAIGN_ID,
    "name": "Diwali Haggle",
    "status": "committed",
    "merchant": {
        "id": "00000000-0000-0000-0000-00000000d001",
        "name": "Sharma Kirana Store",
        "store_line": "Since 1998 - Lajpat Nagar, New Delhi",
    },
    "budget_paise": 500000,
    "max_discount_bps": 2000,
    "margin_floor_bps": 1200,
    "merkle_root": "2ff09aca22960d6e0c06a802043e64e9633c12ef650c4d78266fcdcef521bbce",
    "policy_hash": "9b1c1f7d2c4a5e6f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f7",
    "committed_at": "2026-08-28T11:04:08.536Z",
}

_SLOTS = [
    {"slot_token": "ABCD1234EF", "ceiling_bps": 1500, "leaf_index": 0, "status": "unused"},
    {"slot_token": "GHIJ5678KL", "ceiling_bps": 1200, "leaf_index": 1, "status": "unused"},
]


class _FakeDb:
    """Just the two rpc calls the sheet makes."""

    def __init__(self, campaign: dict | None = _CAMPAIGN) -> None:
        self._campaign = campaign

    async def rpc(self, fn: str, args: dict):
        if fn == "get_campaign":
            return self._campaign
        if fn == "list_campaign_slots":
            return _SLOTS
        raise AssertionError(f"unexpected rpc: {fn}")


@pytest.fixture(autouse=True)
def _wiring(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "MERCHANT_API_KEY", KEY, raising=False)
    monkeypatch.setattr(settings, "PUBLIC_APP_BASE_URL", "https://example.test", raising=False)
    deps._failures.clear()
    app.dependency_overrides[deps.get_db] = lambda: _FakeDb()
    yield
    app.dependency_overrides.clear()
    deps._failures.clear()


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False so an unhandled error surfaces as the 500 a
    # browser would actually receive, rather than blowing up inside the test.
    return TestClient(app, raise_server_exceptions=False)


# ------------------------------------------------------------------ the bug --
def test_correct_key_renders_the_sheet(client: TestClient) -> None:
    """The regression. This returned 500 for every caller, valid key included."""
    r = client.get(f"/api/v1/campaigns/{CAMPAIGN_ID}/qr-sheet", params={"k": KEY})
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers["content-type"]
    # Proves it rendered rather than merely returning something: the tokens on
    # the sheet are the whole point of the sheet.
    assert "ABCD1234EF" in r.text
    assert "GHIJ5678KL" in r.text


def test_bad_key_is_401_and_never_500(client: TestClient) -> None:
    """A wrong key must be refused, not crash. Both were 500 before the fix, so
    the route failed identically whether or not you were authorised."""
    r = client.get(f"/api/v1/campaigns/{CAMPAIGN_ID}/qr-sheet", params={"k": "nope"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "BAD_MERCHANT_KEY"


def test_missing_key_is_401_and_never_500(client: TestClient) -> None:
    r = client.get(f"/api/v1/campaigns/{CAMPAIGN_ID}/qr-sheet")
    assert r.status_code == 401


# ------------------------------------------------- the guards still reachable --
# These 4xx paths sit *after* the auth call. While it was raising AttributeError
# none of them could ever be reached, so they are worth pinning too.
def test_unknown_campaign_is_404(client: TestClient) -> None:
    app.dependency_overrides[deps.get_db] = lambda: _FakeDb(campaign=None)
    r = client.get(f"/api/v1/campaigns/{CAMPAIGN_ID}/qr-sheet", params={"k": KEY})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "CAMPAIGN_NOT_FOUND"


def test_uncommitted_campaign_refuses_to_print(client: TestClient) -> None:
    """Printing before commit would put codes on paper that nothing has
    promised anything about."""
    app.dependency_overrides[deps.get_db] = lambda: _FakeDb(
        campaign={**_CAMPAIGN, "merkle_root": None}
    )
    r = client.get(f"/api/v1/campaigns/{CAMPAIGN_ID}/qr-sheet", params={"k": KEY})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NOT_COMMITTED"
