"""The customer's whole journey, over HTTP, against a real Postgres.

Scan, haggle two items, pay once, land on a redemption code, and have the
shopkeeper scan it. Every other test in this suite checks one layer; this one
exists because the bug it guards against lived BETWEEN layers -- the gate was
right, the SQL was right, and the session could still only ever hold one offer,
so a shopper who asked about a second item lost the first.

The model is deliberately absent: `provider_chain` is emptied, which drops the
agent onto its deterministic tier. That tier is not a stub -- it resolves an
item, calls propose_offer, and goes through the same gate -- so the basket is
being filled by the real path, just without a network call or a language model
in the middle of an assertion.

Payments run in stub mode, which is what makes settlement reachable without
Razorpay credentials. `test_payments.py` guards the far more important property
that stub mode cannot be reached by accident.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from app.api import deps
from app.core import agent as agentmod
from app.core import llm as llmmod
from app.core.config import settings
from app.main import app

pytestmark = pytest.mark.integration

MERCHANT_KEY = "test-merchant-key-for-cart-http"
CEILING_BPS = 1_200


@pytest.fixture
async def db():
    dsn = os.environ.get("DATABASE_URL") or settings.DATABASE_URL
    if not dsn:
        pytest.skip("no DATABASE_URL (set it, or run `make db-reset`)")
    from app.core.db import PostgresBackend

    backend = await PostgresBackend.connect(dsn)
    try:
        async with backend._pool.acquire() as conn:
            if not await conn.fetchval("select to_regclass('public.carts')"):
                pytest.skip("cart migration not applied to this database")
        yield backend
    finally:
        await backend.close()


@pytest.fixture(autouse=True)
def _wiring(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "MERCHANT_API_KEY", MERCHANT_KEY, raising=False)
    # Stub payments: no Razorpay credentials, explicit opt-in, not production.
    monkeypatch.setattr(settings, "ALLOW_STUB_PAYMENTS", True, raising=False)
    monkeypatch.setattr(settings, "APP_ENV", "local", raising=False)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "", raising=False)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "", raising=False)
    # No provider, so run_agent falls to the deterministic tier: real tools,
    # real gate, no network.
    monkeypatch.setattr(llmmod, "provider_chain", lambda: [])
    monkeypatch.setattr(agentmod.llmmod, "provider_chain", lambda: [])
    deps._failures.clear()
    yield
    app.dependency_overrides.clear()
    deps._failures.clear()


@pytest.fixture
async def client(db):
    """An in-process HTTP client on the SAME event loop as the pool.

    Not fastapi.testclient.TestClient, which runs the app in its own loop on
    another thread -- an asyncpg pool created here and used from there fails
    with "another operation is in progress", which is asyncpg correctly
    refusing to be shared across loops rather than anything wrong with the app.
    """
    app.dependency_overrides[deps.get_db] = lambda: db
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


#: A one-leaf Merkle tree: with tree_size 1 the proof is empty and the root IS
#: the leaf, so verify_service's walk reproduces it and `valid` can actually be
#: true. The hash value itself is arbitrary -- what is under test here is the
#: basket and the counter, and the real hashing has its own parity fixture
#: checked on both sides of the wire in test_merkle.py and merkle.test.ts.
LEAF = "d" * 64


async def _live_sticker(db) -> str:
    """One live campaign, one shared sticker, committed to a one-leaf tree."""
    token = "H" + uuid.uuid4().hex[:9].upper()
    async with db._pool.acquire() as conn:
        merchant = await conn.fetchval("select id from merchants limit 1")
        campaign = await conn.fetchval(
            """
            insert into campaigns (merchant_id, name, status, budget_paise,
                                   max_discount_bps, margin_floor_bps, max_turns,
                                   slot_count, merkle_root, policy_hash,
                                   tree_size, committed_at, sticker_sharing)
            values ($1,'cart http test','live',5000000,2000,500,6,1,
                    $2, repeat('b',64), 1, now(), 'shared')
            returning id
            """,
            merchant, LEAF,
        )
        await conn.execute(
            """
            insert into slots (campaign_id, leaf_index, slot_token, salt_hex,
                               ceiling_bps, leaf_hash, proof, sharing)
            values ($1, 0, $2, repeat('c',32), $3, $4, '[]'::jsonb, 'shared')
            """,
            campaign, token, CEILING_BPS, LEAF,
        )
    return token


def _phone() -> str:
    return "+9197" + uuid.uuid4().int.__str__()[:8]


async def _open(client, token: str, phone: str | None = None) -> str:
    res = await client.post(
        "/api/v1/sessions", json={"slot_token": token, "phone": phone or _phone()})
    assert res.status_code == 200, res.text
    return res.json()["session_id"]


async def _say(client, session: str, message: str) -> dict:
    res = await client.post(
        f"/api/v1/sessions/{session}/chat", json={"message": message})
    assert res.status_code == 200, res.text
    return res.json()


# ---------------------------------------------------------------- the shop --
async def test_two_negotiations_make_one_basket(client, db) -> None:
    """The bug, end to end. Ask about atta, then about oil, and BOTH are still
    there -- each at the rate its own line was granted."""
    session = await _open(client, await _live_sticker(db))

    assert (await _say(client, session, "I want atta"))["cart"]["count"] == 1
    cart = (await _say(client, session, "also sunflower oil"))["cart"]

    assert cart["count"] == 2
    assert {i["sku"] for i in cart["items"]} == {"ATTA5", "OIL1L"}
    assert all(i["granted_bps"] > 0 for i in cart["items"])
    assert cart["total_paise"] == sum(i["line_total_paise"] for i in cart["items"])


async def test_the_reply_never_asks_the_shopper_to_pay(client, db) -> None:
    """The assistant's job ends at the price. Payment is a button on the
    basket, and an assistant that asks for money has decided on the shopper's
    behalf that they are finished shopping."""
    session = await _open(client, await _live_sticker(db))
    reply = (await _say(client, session, "I want atta"))["reply"].lower()

    assert "shall i take the payment" not in reply
    assert "checkout" not in reply
    # And it names the item, rather than the bare "Done -- 5% off." that told a
    # shopper with three things in the basket nothing at all.
    assert "atta" in reply


async def test_the_basket_survives_a_reload(client, db) -> None:
    token = await _live_sticker(db)
    phone = _phone()
    session = await _open(client, token, phone)
    await _say(client, session, "atta")

    # Same sticker, same phone: open_session_by_token resumes rather than forks.
    assert await _open(client, token, phone) == session

    cart = (await client.get(f"/api/v1/sessions/{session}/cart")).json()
    assert cart["count"] == 1


async def test_a_line_can_be_taken_back_out(client, db) -> None:
    session = await _open(client, await _live_sticker(db))
    await _say(client, session, "atta")
    await _say(client, session, "sunflower oil")

    after = await client.post(
        f"/api/v1/sessions/{session}/cart/remove", json={"sku": "OIL1L"})
    assert after.status_code == 200, after.text
    assert [i["sku"] for i in after.json()["items"]] == ["ATTA5"]


# ------------------------------------------------------------ the checkout --
async def test_an_empty_basket_cannot_check_out(client, db) -> None:
    session = await _open(client, await _live_sticker(db))
    refused = await client.post(f"/api/v1/sessions/{session}/checkout", json={})
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["code"] == "CART_EMPTY"


async def test_one_order_covers_the_whole_basket(client, db) -> None:
    session = await _open(client, await _live_sticker(db))
    await _say(client, session, "atta")
    cart = (await _say(client, session, "sunflower oil"))["cart"]

    res = await client.post(f"/api/v1/sessions/{session}/checkout", json={})
    assert res.status_code == 200, res.text
    order = res.json()

    assert order["line_count"] == 2
    assert order["amount_paise"] == cart["total_paise"]
    assert order["gross_paise"] - order["discount_paise"] == order["amount_paise"]
    # Every line kept its own rate; the aggregate is a mean, not a re-quote.
    assert order["effective_bps"] <= CEILING_BPS


# -------------------------------------------------- pay, redeem, verify -----
async def _through_to_payment(client, db):
    session = await _open(client, await _live_sticker(db))
    await _say(client, session, "atta")
    await _say(client, session, "sunflower oil")
    order = (await client.post(
        f"/api/v1/sessions/{session}/checkout", json={})).json()

    res = await client.post("/api/v1/payments/confirm", json={
        "order_id": order["order_id"],
        "payment_id": "pay_" + uuid.uuid4().hex[:14],
        "signature": "stub",
    })
    assert res.status_code == 200, res.text
    return session, order, res.json()


async def test_paying_produces_a_redemption_code(client, db) -> None:
    """REGRESSION. The token used to come back null on a shared sticker, so the
    phone had nothing to navigate to and the merchant had no QR to scan."""
    _, order, settled = await _through_to_payment(client, db)

    assert settled["settled"] is True
    assert settled["redemption_token"]

    # And the poll path, which is what the phone falls back to when the
    # callback does not arrive, has to agree with the callback that just did.
    status = (await client.get(
        f"/api/v1/payments/{order['order_id']}/status",
        params={"session_id": settled["session_id"]},
    )).json()
    assert status["redemption_token"] == settled["redemption_token"]


async def test_the_customers_own_screen_shows_the_whole_basket(client, db) -> None:
    _, order, settled = await _through_to_payment(client, db)

    res = await client.get(f"/api/v1/redemption/{settled['redemption_token']}")
    assert res.status_code == 200, res.text
    view = res.json()

    assert view["final_amount_paise"] == order["amount_paise"]
    assert view["bill"]["count"] == 2
    assert view["bill"]["total_paise"] == order["amount_paise"]


async def test_the_counter_sees_the_customer_and_the_bill(client, db) -> None:
    _, order, settled = await _through_to_payment(client, db)

    res = await client.post(
        "/api/v1/verify",
        json={"token": settled["redemption_token"]},
        headers={"X-Merchant-Key": MERCHANT_KEY},
    )
    assert res.status_code == 200, res.text
    verdict = res.json()

    assert verdict["valid"] is True
    assert verdict["customer"]["identified"] is True
    assert len(verdict["customer"]["phone_last4"]) == 4
    assert verdict["bill"]["count"] == 2
    assert verdict["bill"]["total_paise"] == order["amount_paise"]
    # The proof is still the proof.
    assert verdict["proof"] is not None
    assert verdict["within_ceiling"] is True


async def test_a_paid_conversation_will_not_sell_anything_more(client, db) -> None:
    """A shared sticker's slot stays 'offered' forever -- that is what makes it
    a shelf fixture rather than a coupon. So nothing in the gate stops the
    model negotiating a second discount into a session that has already been
    paid for. The shopper would be told a price that no checkout could honour."""
    session, _, _ = await _through_to_payment(client, db)

    after = await _say(client, session, "I want rice as well")
    assert after["offer"] is None
    assert after["cart"]["count"] == 2  # unchanged: nothing new was added
    assert "paid" in after["reply"].lower()


async def test_verifying_twice_is_still_refused(client, db) -> None:
    _, _, settled = await _through_to_payment(client, db)
    headers = {"X-Merchant-Key": MERCHANT_KEY}
    body = {"token": settled["redemption_token"]}

    first = (await client.post("/api/v1/verify", json=body, headers=headers)).json()
    assert first["valid"] is True

    second = (await client.post("/api/v1/verify", json=body, headers=headers)).json()
    assert second["valid"] is False
    assert second["code"] == "V02_ALREADY_VERIFIED"
