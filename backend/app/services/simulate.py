"""What these numbers will actually do, before they are frozen.

Commit is irreversible: once a campaign is committed the ceilings cannot move
without the Merkle root moving, and the sheet is already printed. So the one
moment a shopkeeper can change their mind is *before* they press it, and the
one number they cannot reason about unaided is the margin floor.

A 12% floor sounds mild. On this shop's real catalog it means sugar and
cooking oil can never be discounted by a single paisa, and the flagship atta
is capped at 2.31% — which looks like a broken agent on stage if nobody
warned you. This module computes that answer from the same pure functions the
live gate uses, so the preview cannot drift from the enforcement.

It also answers the question every merchant asks second: how far does the
budget actually go?
"""

from __future__ import annotations

from typing import Any

from app.core import bounds
from app.services import campaign_service


def _pct(bps: int) -> float:
    return round(bps / 100, 2)


def item_headroom(
    price_paise: int, cost_paise: int, margin_floor_bps: int, campaign_max_bps: int
) -> dict[str, Any]:
    """The most this item could ever be discounted, and what stops it."""
    margin_now = bounds.margin_bps_after(price_paise, cost_paise, 0)
    margin_ceiling = bounds.max_discount_for_margin(
        price_paise, cost_paise, margin_floor_bps
    )

    if margin_ceiling < 0:
        return {
            "max_discount_bps": 0,
            "max_discount_pct": 0.0,
            "margin_at_list_bps": margin_now,
            "margin_at_list_pct": _pct(margin_now),
            "discountable": False,
            "binding": "margin_floor_bps",
            "explain": (
                f"Margin at list price is {_pct(margin_now)}%, already below the "
                f"{_pct(margin_floor_bps)}% floor. No discount is possible."
            ),
        }

    allowed = min(margin_ceiling, campaign_max_bps)
    binding = (
        "margin_floor_bps" if margin_ceiling < campaign_max_bps
        else "campaign_max_discount_bps"
    )
    return {
        "max_discount_bps": allowed,
        "max_discount_pct": _pct(allowed),
        "margin_at_list_bps": margin_now,
        "margin_at_list_pct": _pct(margin_now),
        "discountable": allowed > 0,
        "binding": binding,
        "explain": (
            f"Can go to {_pct(allowed)}% off, held there by "
            + ("the margin floor." if binding == "margin_floor_bps"
               else "the campaign maximum.")
        ),
    }


def simulate(
    catalog: list[dict[str, Any]],
    *,
    max_discount_bps: int,
    margin_floor_bps: int,
    budget_paise: int,
    slot_count: int,
) -> dict[str, Any]:
    """A full pre-commit picture: per item, per sticker, and per budget."""
    items = []
    blocked = []
    for c in catalog:
        head = item_headroom(
            int(c["price_paise"]), int(c["cost_paise"]),
            margin_floor_bps, max_discount_bps,
        )
        entry = {
            "sku": c["sku"], "name": c["name"],
            "price_paise": int(c["price_paise"]),
            **head,
        }
        items.append(entry)
        if not head["discountable"]:
            blocked.append(c["sku"])

    ceilings = campaign_service.plan_ceilings(slot_count, max_discount_bps)
    tiers: dict[int, int] = {}
    for c in ceilings:
        tiers[c] = tiers.get(c, 0) + 1

    # Budget reach: the worst case is every sticker redeemed at its ceiling on
    # whichever item costs the shop most at that ceiling. Cheap optimism here
    # would let a shop print a sheet its budget cannot honour.
    #
    # This used to pair the dearest item's PRICE with a different item's
    # HEADROOM, which no single sticker can produce -- and because the dearest
    # item is usually the one with the tightest headroom, the overstatement was
    # large enough to fire spurious "budget will run out" warnings and train
    # the shopkeeper to ignore them. The maximum has to be taken over items,
    # each evaluated whole.
    #
    # It also ignored quantity entirely. A slot can be redeemed for up to
    # MAX_QTY units, so the true exposure per sticker is qty times higher.
    discountable = [i for i in items if i["discountable"]]

    def worst_spend_at(ceiling_bps: int) -> int:
        """Most the shop can give away on one sticker capped at `ceiling_bps`."""
        return max(
            (
                i["price_paise"] * bounds.MAX_QTY
                * min(ceiling_bps, i["max_discount_bps"]) // 10_000
                for i in discountable
            ),
            default=0,
        )

    worst_case_per_slot = [worst_spend_at(c) for c in ceilings]

    # How many stickers the budget survives if the dearest redemptions land
    # first. Sorted descending on purpose: the pessimistic order is the useful
    # one when the question is "when does the budget rule start refusing?"
    running = 0
    slots_before_exhausted = 0
    for spend in sorted(worst_case_per_slot, reverse=True):
        if spend <= 0:
            continue
        if running + spend > budget_paise:
            break
        running += spend
        slots_before_exhausted += 1

    return {
        "items": sorted(items, key=lambda i: (-i["max_discount_bps"], i["sku"])),
        "blocked_skus": blocked,
        "blocked_count": len(blocked),
        "discountable_count": len(discountable),
        "ceiling_tiers": [
            {"ceiling_bps": k, "ceiling_pct": _pct(k), "slots": v}
            for k, v in sorted(tiers.items())
        ],
        "budget": {
            "budget_paise": budget_paise,
            "worst_case_total_paise": sum(worst_case_per_slot),
            "covers_all_slots": sum(worst_case_per_slot) <= budget_paise,
            "slots_before_exhausted": slots_before_exhausted,
            "slot_count": slot_count,
        },
        "warnings": _warnings(items, blocked, ceilings, budget_paise,
                             sum(worst_case_per_slot), margin_floor_bps),
    }


def _warnings(
    items: list[dict[str, Any]], blocked: list[str], ceilings: list[int],
    budget_paise: int, worst_case: int, margin_floor_bps: int,
) -> list[dict[str, str]]:
    """Plain sentences, in the order a shopkeeper would care about them."""
    out: list[dict[str, str]] = []

    if blocked:
        out.append({
            "level": "warn",
            "message": (
                f"{len(blocked)} product(s) can never be discounted at a "
                f"{_pct(margin_floor_bps)}% floor: {', '.join(blocked)}. "
                "The agent will politely refuse on these, which is correct — "
                "but do not pick one for a live demonstration."
            ),
        })

    if not any(i["discountable"] for i in items):
        out.append({
            "level": "stop",
            "message": (
                "No product in the catalog can be discounted at this margin "
                "floor. Every conversation would end in a refusal."
            ),
        })

    if worst_case > budget_paise:
        out.append({
            "level": "warn",
            "message": (
                f"Worst case — every sticker redeemed at its ceiling, on the "
                f"costliest item, at the maximum {bounds.MAX_QTY} units — is "
                f"₹{worst_case / 100:,.2f} against a budget of "
                f"₹{budget_paise / 100:,.2f}. The budget rule would start "
                "refusing before the sheet is used up. This is a ceiling on "
                "your exposure, not a forecast."
            ),
        })

    top = max(ceilings, default=0)
    if top and all(i["max_discount_bps"] < top for i in items if i["discountable"]):
        out.append({
            "level": "info",
            "message": (
                f"Your highest sticker allows {_pct(top)}% but no product can "
                "reach it — the margin floor binds first on everything. Those "
                "stickers will behave identically to lower ones."
            ),
        })

    return out
