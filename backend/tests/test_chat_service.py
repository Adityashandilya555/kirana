"""What crosses to a customer's phone, and what must not.

The first test in this file is the one that matters. max_allowed_bps was
shipped to the browser on every approved offer, and because plan_ceilings tiers
most stickers below the campaign maximum, that value IS the slot's committed
ceiling for a typical sticker -- so a shopper could ask for 2%, be approved as
proposed, and read their cap out of the response without ever triggering a
clamp. From there the haggling is theatre.

It stays server-side, where propose_offer needs it for the refuse-and-explain
loop. These tests pin the split.
"""

from __future__ import annotations

import pytest

from app.core.bounds import BoundsInput, check
from app.core.tools import CatalogItem, OfferContext
from app.services.chat_service import _offer_context, _offer_payload

TEA = CatalogItem(
    sku="TEA250", name="Tata Tea Gold 250g", unit="pack",
    price_paise=19000, cost_paise=13500,
)

#: Every field name that would reveal, or let a shopper derive, the ceiling.
#: Every field that would hand a shopper a ceiling. cap_bps and
#: customer_tier_cap_bps are new and belong here for the same reason as the
#: rest: knowing the number ends the negotiation. cap_fraction is worse than
#: the others -- combined with a single observed grant it recovers the product
#: cap by division, so it must never reach the phone even alongside a band name.
CEILING_FIELDS = {
    "max_allowed_bps", "ceiling_bps", "slot_ceiling_bps",
    "cap_bps", "product_cap_bps", "customer_cap_bps",
    "customer_tier_cap_bps", "tier_cap_fraction_bps",
}


def _offer(proposed_bps: int, ceiling_bps: int = 1200) -> dict | None:
    oc = OfferContext(
        catalog=[TEA],
        slot_ceiling_bps=ceiling_bps,
        campaign_max_discount_bps=2000,
        margin_floor_bps=1200,
        budget_paise=500_000,
    )
    oc.last_sku, oc.last_qty = "TEA250", 1
    oc.last_decision = check(
        BoundsInput(
            proposed_bps=proposed_bps,
            price_paise=TEA.price_paise,
            cost_paise=TEA.cost_paise,
            slot_ceiling_bps=ceiling_bps,
            campaign_max_discount_bps=2000,
            margin_floor_bps=1200,
            budget_paise=500_000,
        )
    )
    return _offer_payload(oc)


# ------------------------------------------------------------ the leak ------
def test_an_uncapped_offer_does_not_reveal_the_ceiling() -> None:
    """The exact path the leak took: ask small, get approved, read the cap.

    No clamp fires here, so nothing in the UI would have hinted the number was
    sensitive -- which is why it went unnoticed.
    """
    offer = _offer(proposed_bps=200)
    assert offer is not None
    assert offer["capped"] is False
    assert CEILING_FIELDS.isdisjoint(offer)


def test_a_capped_offer_does_not_reveal_the_ceiling_either() -> None:
    offer = _offer(proposed_bps=9000)
    assert offer is not None
    assert offer["capped"] is True
    assert CEILING_FIELDS.isdisjoint(offer)


@pytest.mark.parametrize("ceiling", [300, 500, 1200, 1700, 2000])
def test_the_ceiling_value_never_appears_at_any_tier(ceiling: int) -> None:
    """plan_ceilings spreads stickers across tiers; the leak has to be closed
    at all of them, not just the one a test happened to pick."""
    offer = _offer(proposed_bps=100, ceiling_bps=ceiling)
    assert offer is not None
    assert ceiling not in offer.values()


def test_the_payload_is_a_closed_allowlist() -> None:
    """A field added to Decision must not reach the phone by accident. If this
    fails, decide deliberately whether the new field is safe for a shopper."""
    offer = _offer(proposed_bps=500)
    assert set(offer) == {
        "sku", "qty", "granted_bps", "proposed_bps", "discount_paise",
        "final_amount_paise", "code", "capped", "binding_constraint",
        "customer_reason",
    }


# --------------------------------------------------- what it does contain ---
def test_the_gate_is_still_visible_without_the_ceiling() -> None:
    """Removing the number must not remove the demonstration: asked-versus-
    granted plus the rule that bound still shows the gate working."""
    offer = _offer(proposed_bps=9000)
    assert offer["proposed_bps"] == 9000
    assert offer["granted_bps"] == 1200
    assert offer["binding_constraint"] == "slot_ceiling_bps"


def test_a_refused_offer_produces_no_card_at_all() -> None:
    """Sugar cannot clear the floor, so there is nothing to accept."""
    oc = OfferContext(
        catalog=[CatalogItem(sku="SUGAR1", name="Sugar 1kg", unit="pack",
                             price_paise=4800, cost_paise=4300)],
        slot_ceiling_bps=1200,
        campaign_max_discount_bps=2000,
        margin_floor_bps=1200,
        budget_paise=500_000,
    )
    oc.last_decision = check(
        BoundsInput(proposed_bps=500, price_paise=4800, cost_paise=4300,
                    slot_ceiling_bps=1200, campaign_max_discount_bps=2000,
                    margin_floor_bps=1200, budget_paise=500_000)
    )
    assert _offer_payload(oc) is None


def test_money_in_the_payload_is_integer_paise() -> None:
    offer = _offer(proposed_bps=500)
    assert isinstance(offer["discount_paise"], int)
    assert isinstance(offer["final_amount_paise"], int)


# ----------------------------------------------------- context assembly ----
def test_the_gate_context_does_carry_cost() -> None:
    """The mirror image of the leak tests: cost must reach bounds.check(),
    because the margin floor cannot be applied without it. It is stopped one
    layer later, at the payload."""
    ctx = {
        "session": {"turn_count": 1},
        "slot": {"ceiling_bps": 1200, "status": "unused"},
        "campaign": {"status": "live", "max_discount_bps": 2000,
                     "margin_floor_bps": 1200, "budget_paise": 500_000,
                     "spent_paise": 0, "reserved_paise": 0, "max_turns": 6},
        "merchant": {},
        "catalog": [{"sku": "TEA250", "name": "Tea", "unit": "pack",
                     "price_paise": 19000, "cost_paise": 13500}],
    }
    oc = _offer_context(ctx)
    assert oc.catalog[0].cost_paise == 13500
