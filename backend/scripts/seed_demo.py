"""One command back to a known demo state.

Two modes, and the difference matters on demo day:

  --reset  Wipes sessions, payments and decisions but KEEPS slot tokens,
           salts, ceilings, proofs and the Merkle root. The printed sheet on
           the table stays valid. This is what you run between rehearsals.

  --nuke   Destroys the campaign and builds a new one. New tokens, new root.
           The sheet in your hand is now worthless. Reprint it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import settings
from app.core.db import create_db_backend
from app.services import campaign_service

DEMO_MERCHANT_ID = "00000000-0000-0000-0000-00000000d001"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="keep the printed sheet valid")
    ap.add_argument("--nuke", action="store_true", help="new campaign; reprint required")
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--budget", type=int, default=500_000, help="paise")
    ap.add_argument("--max-discount-bps", type=int, default=2000)
    ap.add_argument("--margin-floor-bps", type=int, default=1200)
    args = ap.parse_args()

    db = await create_db_backend()
    try:
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
