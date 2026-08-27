"""One command back to a known demo state.

Two modes, and the difference matters on demo day:

  --reset  Wipes sessions, payments and decisions but KEEPS slot tokens,
           salts, ceilings, proofs and the Merkle root. The printed sheet on
           the table stays valid. This is what you run between rehearsals.

  --nuke   Destroys the campaign and builds a new one. New tokens, new root.
           The sheet in your hand is now worthless. Reprint it.

  --settle Drives one slot all the way to a redemption token WITHOUT Razorpay,
           through the real reserve_slot -> settle_payment path with
           source='manual'. This is what makes Phase 3's verification UI
           testable before any payment credential exists: the token it mints
           is indistinguishable from one a real card payment would produce,
           because it is produced by exactly the same function.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from app.core import bounds, ids
from app.core.config import settings
from app.core.db import create_db_backend
from app.services import campaign_service

DEMO_MERCHANT_ID = "00000000-0000-0000-0000-00000000d001"


async def settle_slot(db, slot_token: str, sku: str, qty: int) -> int:
    """Open a session on `slot_token` and settle it manually.

    Discount is whatever the gate allows at this slot's ceiling -- asking for
    the ceiling and letting bounds.check() clamp is the same path the agent
    takes, so this cannot mint a token the rules would not have permitted.
    """
    token = ids.normalize_token(slot_token)
    opened = await db.rpc("open_session_by_token", {"p_slot_token": token})
    ctx = opened["context"]
    session_id = opened["session_id"]

    item = next((c for c in ctx["catalog"] if c["sku"] == sku.upper()), None)
    if item is None:
        print(f"  no such sku {sku!r}; have: "
              f"{', '.join(c['sku'] for c in ctx['catalog'])}")
        return 1

    campaign, slot = ctx["campaign"], ctx["slot"]
    verdict = bounds.check(
        bounds.BoundsInput(
            proposed_bps=slot["ceiling_bps"],
            price_paise=item["price_paise"],
            cost_paise=item["cost_paise"],
            qty=qty,
            slot_ceiling_bps=slot["ceiling_bps"],
            slot_status=slot["status"],
            campaign_status=campaign["status"],
            campaign_max_discount_bps=campaign["max_discount_bps"],
            margin_floor_bps=campaign["margin_floor_bps"],
            budget_paise=campaign["budget_paise"],
            spent_paise=campaign["spent_paise"],
            reserved_paise=campaign["reserved_paise"],
            turn_count=0,
            max_turns=campaign["max_turns"],
        )
    )
    if not verdict.approved:
        print(f"  gate refused: {verdict.code.value} — {verdict.reason}")
        return 1

    order_id = f"order_manual_{uuid.uuid4().hex[:14]}"
    await db.rpc("reserve_slot", {
        "p_session_id": session_id,
        "p_sku": item["sku"],
        "p_qty": qty,
        "p_discount_bps": verdict.granted_bps,
        "p_discount_paise": verdict.discount_paise,
        "p_amount_paise": verdict.final_amount_paise,
        "p_rzp_order_id": order_id,
    })
    settled = await db.rpc("settle_payment", {
        "p_rzp_order_id": order_id,
        "p_rzp_payment_id": f"pay_manual_{uuid.uuid4().hex[:14]}",
        "p_signature": None,
        "p_amount_paise": verdict.final_amount_paise,
        "p_source": "manual",
    })

    redemption = settled["redemption_token"]
    print(f"\n  settled {item['sku']} x{qty} at {verdict.granted_bps/100:g}% "
          f"(ceiling {slot['ceiling_bps']/100:g}%)")
    print(f"  paid      ₹{verdict.final_amount_paise/100:,.2f}"
          f"  saved ₹{verdict.discount_paise/100:,.2f}")
    print(f"\n  redemption token  {redemption}")
    print(f"  customer screen   {settings.PUBLIC_APP_BASE_URL}/r/{redemption}")
    print(f"  merchant scans    {ids.redemption_payload(redemption)}")
    print(f"  verify page       {settings.PUBLIC_APP_BASE_URL}/verify\n")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="keep the printed sheet valid")
    ap.add_argument("--nuke", action="store_true", help="new campaign; reprint required")
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--budget", type=int, default=500_000, help="paise")
    ap.add_argument("--max-discount-bps", type=int, default=2000)
    ap.add_argument("--margin-floor-bps", type=int, default=1200)
    ap.add_argument("--settle", metavar="SLOT_TOKEN",
                    help="mint a redemption token via settle_payment(source='manual')")
    ap.add_argument("--settle-sku", default="TEA250",
                    help="which item to settle (default TEA250: most margin headroom)")
    ap.add_argument("--settle-qty", type=int, default=1)
    args = ap.parse_args()

    db = await create_db_backend()
    try:
        if args.settle:
            return await settle_slot(db, args.settle, args.settle_sku, args.settle_qty)

        campaigns = await db.rpc("list_merchant_campaigns", {"p_merchant_id": DEMO_MERCHANT_ID})
        live = [c for c in (campaigns or []) if c["status"] == "live"]

        if args.reset:
            if not live:
                print("nothing live to reset; run with --nuke to build a campaign")
                return 1
            target = live[0]
            result = await db.rpc("reset_demo", {"p_campaign_id": target["id"]})
            campaign = await db.rpc("get_campaign", {"p_campaign_id": target["id"]})
            print(f"reset {result['slots_reset']} slots — {result['note']}")
        else:
            if args.nuke:
                await db.rpc("nuke_demo", {"p_merchant_id": DEMO_MERCHANT_ID})
                print("nuked previous campaigns")
            created = await campaign_service.create_campaign(
                db,
                merchant_id=DEMO_MERCHANT_ID,
                name="Diwali Haggle",
                budget_paise=args.budget,
                max_discount_bps=args.max_discount_bps,
                margin_floor_bps=args.margin_floor_bps,
                max_turns=6,
                slot_count=args.slots,
            )
            out = await campaign_service.commit_campaign(db, created["id"])
            campaign = out["campaign"]
            print("\n  ⚠  NEW ROOT — REPRINT THE QR SHEET\n")

        slots = await db.rpc("list_campaign_slots", {"p_campaign_id": campaign["id"]})
        key = settings.MERCHANT_API_KEY
        print(f"  campaign  {campaign['id']}  \"{campaign['name']}\"  [{campaign['status']}]")
        print(f"  root      {campaign['merkle_root']}")
        print(f"  tree      {campaign['tree_size']} leaves for {campaign['slot_count']} slots")
        print(f"  budget    ₹{campaign['budget_paise']/100:,.2f}"
              f"  spent ₹{campaign['spent_paise']/100:,.2f}")
        print(f"\n  QR sheet  http://127.0.0.1:8000/api/v1/campaigns/{campaign['id']}/qr-sheet?k={key}")

        print("\n  one token per ceiling, for a scripted demo:")
        seen: set[int] = set()
        for s in slots:
            if s["ceiling_bps"] not in seen:
                seen.add(s["ceiling_bps"])
                print(f"    {s['ceiling_bps']/100:>5g}%  {s['slot_token']}"
                      f"   {settings.PUBLIC_APP_BASE_URL}/s/{s['slot_token']}")
        print("\n  test card 4111 1111 1111 1111 · any future expiry · any CVV · OTP 1234\n")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
