"""The basket, the money and the counter, against a real Postgres.

Everything here is a rule that lives in plpgsql and cannot be checked anywhere
else: the per-line ceilings inside `reserve_cart`, the aggregate written onto
the session, the redemption token the phone polls for, and what the shopkeeper
is shown when they scan.

Two of these are REGRESSION guards for bugs that shipped. Migration 022 moved
`redemption_token` from `slots` to `payments` and updated the writers but not
the readers, so on a shared sticker `get_payment_status` answered
`settled: true` with a null token and `get_redemption` found nothing at all --
the customer paid, the phone never reached its redemption screen, and there was
no QR for the merchant to scan. They are named as such below.

Run with `make db-reset && make test-all`, or point DATABASE_URL at any
Postgres with sql/ applied.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.integration

CEILING_BPS = 1_200
CAMPAIGN_MAX_BPS = 2_000
MARGIN_FLOOR_BPS = 500


def ref(kind: str) -> str:
    """A fresh order or payment id.

    Fixed literals looked fine on a clean database and then collided with
    themselves the second time the suite ran -- uq_sessions_rzp_order and
    uq_payment_rzp_payment_id are both doing exactly their job. A test that
    only passes against a freshly reset database is a test nobody runs.
    """
    return f"{kind}_{uuid.uuid4().hex[:14]}"


def phone() -> str:
    """A number nobody else in this run is using.

    Customers are keyed by (merchant, phone), so a fixed number accumulates a
    purchase history across runs -- and the assertions here are about exactly
    that history.
    """
    return "+9198" + uuid.uuid4().int.__str__()[:8]


@pytest.fixture
async def conn():
    from app.core.config import settings

    dsn = os.environ.get("DATABASE_URL") or settings.DATABASE_URL
    if not dsn:
        pytest.skip("no DATABASE_URL (set it, or run `make db-reset`)")
    import asyncpg

    c = await asyncpg.connect(dsn)
    try:
        if not await c.fetchval("select to_regclass('public.carts')"):
            pytest.skip("cart migration not applied to this database")
        yield c
    finally:
        await c.close()


async def _campaign(conn, *, sharing: str = "shared") -> tuple[str, str]:
    """A live campaign with one sticker, committed with dummy hashes.

    The hashes are dummies on purpose: nothing here checks the Merkle proof,
    which has its own tests on both sides of the wire. What is under test is
    what happens to money and to a basket.
    """
    merchant = await conn.fetchval("select id from merchants limit 1")
    campaign = await conn.fetchval(
        """
        insert into campaigns (merchant_id, name, status, budget_paise,
                               max_discount_bps, margin_floor_bps, max_turns,
                               slot_count, merkle_root, policy_hash, tree_size,
                               committed_at, sticker_sharing)
        values ($1, 'cart flow test', 'live', 5000000, $2, $3, 6, 1,
                repeat('a',64), repeat('b',64), 1, now(), $4)
        returning id
        """,
        merchant, CAMPAIGN_MAX_BPS, MARGIN_FLOOR_BPS, sharing,
    )
    token = "T" + uuid.uuid4().hex[:9].upper()
    await conn.execute(
        """
        insert into slots (campaign_id, leaf_index, slot_token, salt_hex,
                           ceiling_bps, leaf_hash, proof, sharing)
        values ($1, 0, $2, repeat('c',32), $3, repeat('d',64), '[]'::jsonb, $4)
        """,
        campaign, token, CEILING_BPS, sharing,
    )
    return campaign, token


async def _session(conn, token: str, phone: str) -> str:
    row = await conn.fetchval(
        "select open_session_by_token($1, 'web', null, $2)", token, phone
    )
    import json

    return json.loads(row)["session_id"]


async def _json(conn, sql: str, *args):
    import json

    raw = await conn.fetchval(sql, *args)
    return json.loads(raw) if isinstance(raw, str) else raw


# ------------------------------------------------------------ the basket ----
async def test_two_items_are_two_lines_at_two_rates(conn) -> None:
    _, token = await _campaign(conn)
    session = await _session(conn, token, phone())

    await conn.execute(
        "select upsert_cart_item($1::uuid,'ATTA5',1,500,0,0,'OK',null)", session)
    cart = await _json(
        conn,
        "select upsert_cart_item($1::uuid,'OIL1L',2,600,0,0,'OK',null)", session)

    assert cart["count"] == 2
    rates = {i["sku"]: i["granted_bps"] for i in cart["items"]}
    assert rates == {"ATTA5": 500, "OIL1L": 600}


async def test_a_worse_requote_does_not_take_a_line_backwards(conn) -> None:
    _, token = await _campaign(conn)
    session = await _session(conn, token, phone())

    await conn.execute(
        "select upsert_cart_item($1::uuid,'OIL1L',2,600,0,0,'OK',null)", session)
    cart = await _json(
        conn,
        "select upsert_cart_item($1::uuid,'OIL1L',2,300,0,0,'OK',null)", session)

    assert cart["items"][0]["granted_bps"] == 600


# --------------------------------------------------------- reserve_cart -----
async def test_reserving_recomputes_every_amount_from_the_live_catalogue(conn) -> None:
    """The cart rows carry deliberately wrong amounts here, standing in for a
    price that moved mid-conversation. What the shopper is charged has to come
    from the catalogue, and the itemised bill has to match the charge -- or the
    lines will not add up to the receipt and nobody at the counter can say why.
    """
    _, token = await _campaign(conn)
    session = await _session(conn, token, phone())

    await conn.execute(
        "select upsert_cart_item($1::uuid,'ATTA5',1,500,1,1,'OK',null)", session)
    await conn.execute(
        "select upsert_cart_item($1::uuid,'OIL1L',2,600,1,1,'OK',null)", session)

    reserved = await _json(
        conn, "select reserve_cart($1::uuid,$2,null)", session, ref("order"))

    # ATTA5 28500 @5% = 1425 off; OIL1L 14500x2 = 29000 @6% = 1740 off.
    assert reserved["gross_paise"] == 57_500
    assert reserved["discount_paise"] == 3_165
    assert reserved["total_paise"] == 54_335

    cart = await _json(conn, "select get_cart($1::uuid)", session)
    assert sum(i["line_total_paise"] for i in cart["items"]) == 54_335


async def test_the_effective_rate_stays_under_the_committed_ceiling(conn) -> None:
    """The aggregate written onto the slot is a weighted mean of per-line rates
    that are each already inside the ceiling, so it is inside it too --
    which is what lets slots.ck_granted_le_ceiling stay unweakened while a
    basket carries several different discounts."""
    # A 'once' sticker, because that is where the aggregate is actually
    # WRITTEN onto the slot and the CHECK constraint bites. A shared sticker
    # stays a fixture and its granted_bps column is deliberately left null --
    # several shoppers hold several baskets and one column cannot mean
    # anything sensible for all of them.
    _, token = await _campaign(conn, sharing="once")
    session = await _session(conn, token, phone())

    await conn.execute(
        "select upsert_cart_item($1::uuid,'ATTA5',1,500,0,0,'OK',null)", session)
    await conn.execute(
        "select upsert_cart_item($1::uuid,'OIL1L',2,600,0,0,'OK',null)", session)
    reserved = await _json(
        conn, "select reserve_cart($1::uuid,$2,null)", session, ref("order"))

    # 3165 off 57500 gross is 550 bps -- between the 500 and 600 the two lines
    # were granted, and comfortably inside the 1200 committed for the sticker.
    assert reserved["effective_bps"] == 550
    assert reserved["effective_bps"] <= CEILING_BPS
    stored = await conn.fetchval(
        "select granted_bps from slots where slot_token = $1", token)
    assert stored == reserved["effective_bps"] <= CEILING_BPS


async def test_an_over_ceiling_line_refuses_the_whole_basket(conn) -> None:
    """Not dropped, not silently re-priced. A basket that changed under someone
    reaching for the Pay button is the outcome worth failing loudly to avoid."""
    _, token = await _campaign(conn)
    session = await _session(conn, token, phone())

    await conn.execute(
        "select upsert_cart_item($1::uuid,'ATTA5',1,500,0,0,'OK',null)", session)
    # Straight into the table, past the gate, as a tampered client would.
    await conn.execute(
        """
        update cart_items set granted_bps = $2
         where sku = 'ATTA5' and cart_id in (
           select id from carts where session_id = $1::uuid)
        """,
        session, CEILING_BPS + 100,
    )

    with pytest.raises(Exception, match="CEILING_VIOLATION"):
        await conn.fetchval(
            "select reserve_cart($1::uuid,$2,null)", session, ref("order"))


async def test_an_empty_basket_cannot_check_out(conn) -> None:
    _, token = await _campaign(conn)
    session = await _session(conn, token, phone())
    with pytest.raises(Exception, match="CART_EMPTY"):
        await conn.fetchval(
            "select reserve_cart($1::uuid,$2,null)", session, ref("order"))


# ----------------------------------------------- settle, poll, redeem -------
async def _paid(conn, number: str | None = None):
    """A two-line basket, paid for. Returns (session, order id, settlement)."""
    number = number or phone()
    order, payment = ref("order"), ref("pay")
    _, token = await _campaign(conn)
    session = await _session(conn, token, number)
    await conn.execute(
        "select upsert_cart_item($1::uuid,'ATTA5',1,500,0,0,'OK',null)", session)
    await conn.execute(
        "select upsert_cart_item($1::uuid,'OIL1L',2,600,0,0,'OK',null)", session)
    reserved = await _json(
        conn, "select reserve_cart($1::uuid,$2,null)", session, order)
    settled = await _json(
        conn,
        "select settle_payment($1,$2,'sig',$3::bigint,'checkout_handler')",
        order, payment, reserved["total_paise"],
    )
    return session, order, settled


async def test_the_poll_path_returns_the_redemption_token(conn) -> None:
    """REGRESSION (migration 022). get_payment_status read the token off the
    SLOT, which settle_payment only writes for sharing='once'. On a shared
    sticker the phone polled, was told the payment had settled, got a null
    token, and had nowhere to navigate -- so the redemption QR was never drawn
    and the merchant had nothing to scan."""
    _, order, settled = await _paid(conn)

    status = await _json(conn, "select get_payment_status($1)", order)
    assert status["settled"] is True
    assert status["redemption_token"] is not None
    assert status["redemption_token"] == settled["redemption_token"]


async def test_the_customer_screen_resolves_a_shared_sticker_token(conn) -> None:
    """REGRESSION (migration 022). get_redemption joined from slots, so /r/<tok>
    404d for every redemption on a shared sticker."""
    _, _, settled = await _paid(conn)

    view = await _json(
        conn, "select get_redemption($1)", settled["redemption_token"])
    assert view is not None
    assert view["final_amount_paise"] == 54_335
    assert view["bill"]["count"] == 2


async def test_the_bill_sums_to_what_was_actually_paid(conn) -> None:
    _, _, settled = await _paid(conn)

    verdict = await _json(
        conn, "select verify_redemption($1)", settled["redemption_token"])
    assert verdict["bill"]["total_paise"] == verdict["final_amount_paise"]
    assert verdict["bill"]["discount_paise"] == verdict["discount_paise"]


# ------------------------------------------------------------ the counter ---
async def test_verifying_shows_who_is_standing_there(conn) -> None:
    _, _, settled = await _paid(conn)

    verdict = await _json(
        conn, "select verify_redemption($1)", settled["redemption_token"])
    customer = verdict["customer"]
    assert customer["identified"] is True
    # Four digits and never the number: enough to recognise someone across a
    # counter, not enough to be a contact list.
    assert customer["phone_last4"] and len(customer["phone_last4"]) == 4
    assert customer["band"] in {"new", "preferred"}
    # The purchase being scanned is excluded, so a first-time buyer reads as
    # zero previous visits rather than one.
    assert customer["visits"] == 0


async def test_a_shopper_with_no_number_is_shown_as_unidentified(conn) -> None:
    """Not an error and not a blank: "no number was given" is a different thing
    at a counter from "we lost their record"."""
    _, token = await _campaign(conn)
    import json

    session = json.loads(await conn.fetchval(
        "select open_session_by_token($1,'web',null,null)", token))["session_id"]
    await conn.execute(
        "select upsert_cart_item($1::uuid,'ATTA5',1,500,0,0,'OK',null)", session)
    order = ref("order")
    reserved = await _json(
        conn, "select reserve_cart($1::uuid,$2,null)", session, order)
    settled = await _json(
        conn,
        "select settle_payment($1,$2,'sig',$3::bigint,'poll')",
        order, ref("pay"), reserved["total_paise"],
    )
    verdict = await _json(
        conn, "select verify_redemption($1)", settled["redemption_token"])
    assert verdict["customer"]["identified"] is False


async def test_a_second_customer_sees_their_own_history(conn) -> None:
    """The point of asking for a number. Visit twice, and the second scan says
    so -- and says it with money the shop actually took."""
    _, token = await _campaign(conn)
    number = phone()

    session = await _session(conn, token, number)
    await conn.execute(
        "select upsert_cart_item($1::uuid,'ATTA5',1,500,0,0,'OK',null)", session)
    order = ref("order")
    first = await _json(
        conn, "select reserve_cart($1::uuid,$2,null)", session, order)
    await conn.fetchval(
        "select settle_payment($1,$2,'sig',$3::bigint,'poll')",
        order, ref("pay"), first["total_paise"])

    # A second campaign, because one discount per customer per campaign is
    # exactly what migration 022's unique index enforces.
    _, token2 = await _campaign(conn)
    session2 = await _session(conn, token2, number)
    await conn.execute(
        "select upsert_cart_item($1::uuid,'OIL1L',1,600,0,0,'OK',null)", session2)
    order2 = ref("order")
    second = await _json(
        conn, "select reserve_cart($1::uuid,$2,null)", session2, order2)
    settled = await _json(
        conn,
        "select settle_payment($1,$2,'sig',$3::bigint,'poll')",
        order2, ref("pay"), second["total_paise"])

    verdict = await _json(
        conn, "select verify_redemption($1)", settled["redemption_token"])
    customer = verdict["customer"]
    assert customer["visits"] == 1
    assert customer["spend_paise"] == first["total_paise"]
    assert customer["returning"] is True


async def test_burn_once_still_holds_for_a_basket(conn) -> None:
    _, _, settled = await _paid(conn)
    token = settled["redemption_token"]

    assert (await _json(conn, "select verify_redemption($1)", token))["valid"] is True
    second = await _json(conn, "select verify_redemption($1)", token)
    assert second["valid"] is False
    assert second["code"] == "V02_ALREADY_VERIFIED"


async def test_a_dismissed_checkout_hands_the_basket_back(conn) -> None:
    """A closed payment sheet must return the shopper to exactly where they
    were: same basket, same prices, free to keep shopping."""
    _, token = await _campaign(conn)
    session = await _session(conn, token, phone())
    await conn.execute(
        "select upsert_cart_item($1::uuid,'ATTA5',1,500,0,0,'OK',null)", session)
    order = ref("order")
    await conn.fetchval(
        "select reserve_cart($1::uuid,$2,null)", session, order)

    await conn.fetchval("select release_reservation($1,'dismissed')", order)

    cart = await _json(conn, "select get_cart($1::uuid)", session)
    assert cart["status"] == "open"
    assert cart["count"] == 1
    assert await conn.fetchval(
        "select reserved_paise from campaigns where id = $1",
        await conn.fetchval(
            "select campaign_id from sessions where id = $1::uuid", session),
    ) == 0
