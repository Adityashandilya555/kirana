"""The product cap and the customer band, once they actually move prices.

Three things are being pinned here, in descending order of how expensive they
would be to get wrong:

  1. Neither ceiling can ever RAISE a grant. They are ceilings; if one could
     lift a number, the committed per-product promise would be breakable and
     the printed sticker would be a lie.
  2. Tie-breaking names the right rule. Numerically the smallest ceiling always
     wins, but when two are equal the ORDER decides which one the shopkeeper is
     told bound the offer -- and one of those sentences is about the shopper's
     standing, which is the socially expensive thing to say by accident.
  3. The customer-facing sentence for a band clamp never leaks the number.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.bounds import BoundsInput, check
from app.core.codes import BINDING, BoundsCode

# Tea: ₹190 list, ₹120 cost. Margin at list is ~36.8%, so a 12% floor leaves
# plenty of room and the margin ceiling is not what binds in these cases.
TEA = {"price_paise": 19_000, "cost_paise": 12_000}


def gate(**over) -> object:
    base = dict(
        proposed_bps=2_000,
        price_paise=TEA["price_paise"],
        cost_paise=TEA["cost_paise"],
        qty=1,
        slot_ceiling_bps=2_000,
        campaign_max_discount_bps=2_000,
        margin_floor_bps=1_200,
        budget_paise=5_000_000,
    )
    base.update(over)
    return check(BoundsInput(**base))


# ------------------------------------------------------- the product cap ----
def test_a_product_cap_clamps_the_grant() -> None:
    d = gate(proposed_bps=2_000, product_cap_bps=800)
    assert d.granted_bps == 800
    assert d.code is BoundsCode.OK_CLAMPED_PRODUCT_CAP
    assert d.binding_constraint == "product_cap_bps"


def test_absent_caps_change_nothing() -> None:
    """The compatibility guarantee. A campaign committed before caps existed
    passes None and must behave exactly as it always did."""
    without = gate(proposed_bps=2_000)
    with_none = gate(proposed_bps=2_000, product_cap_bps=None, customer_cap_bps=None)
    assert without.granted_bps == with_none.granted_bps
    assert without.code == with_none.code
    assert without.binding_constraint == with_none.binding_constraint


def test_a_cap_of_zero_refuses_rather_than_offering_nothing_at_a_discount() -> None:
    d = gate(proposed_bps=1_000, product_cap_bps=0)
    assert d.granted_bps == 0


# ------------------------------------------------------ the customer band ---
def test_a_band_clamps_below_the_product_cap() -> None:
    d = gate(proposed_bps=2_000, product_cap_bps=1_600, customer_cap_bps=800)
    assert d.granted_bps == 800
    assert d.code is BoundsCode.OK_CLAMPED_CUSTOMER_TIER
    assert d.binding_constraint == "customer_tier_cap_bps"


def test_the_band_sentence_never_quotes_the_ceiling_or_blames_the_shopper() -> None:
    d = gate(proposed_bps=2_000, product_cap_bps=1_600, customer_cap_bps=800)
    said = d.customer_reason.lower()
    # Not "the best this code allows" -- that is the wrong explanation here,
    # and it is also the sentence that hands over a ceiling.
    assert "16" not in said
    assert "new customer" not in said and "not a regular" not in said
    # It should read as reachable, because the whole point of a band is that
    # it can be reached.
    assert "shop here" in said or "better" in said


def test_a_band_that_reduces_nothing_is_never_named() -> None:
    """A preferred shopper's cap EQUALS the product cap. Naming the band there
    would tell a shopkeeper their loyalty rule bound an offer it did not."""
    d = gate(proposed_bps=2_000, product_cap_bps=1_600, customer_cap_bps=1_600)
    assert d.granted_bps == 1_600
    assert d.binding_constraint == "product_cap_bps"


# ------------------------------------------------------------ precedence ----
def test_on_a_tie_the_product_cap_is_named_before_the_slot() -> None:
    """Both at 800. The product cap is the more specific promise -- committed
    per sku, provable with a proof -- so it is the one to report."""
    d = gate(proposed_bps=2_000, product_cap_bps=800, slot_ceiling_bps=800)
    assert d.granted_bps == 800
    assert d.binding_constraint == "product_cap_bps"


def test_on_a_tie_the_band_is_named_last() -> None:
    """Everything at 800, including the band. Blaming the shopper's standing
    for a ceiling four other rules also produced is the message this ordering
    exists to avoid."""
    d = gate(
        proposed_bps=2_000, product_cap_bps=800, slot_ceiling_bps=800,
        customer_cap_bps=800,
    )
    assert d.granted_bps == 800
    assert d.binding_constraint == "product_cap_bps"


def test_the_band_is_named_when_it_is_genuinely_alone() -> None:
    d = gate(proposed_bps=2_000, product_cap_bps=1_600, customer_cap_bps=400)
    assert d.binding_constraint == "customer_tier_cap_bps"


# ------------------------------------------------------------ properties ----
@given(
    st.integers(min_value=0, max_value=10_000),
    st.integers(min_value=0, max_value=10_000),
    st.integers(min_value=0, max_value=10_000),
)
def test_neither_new_ceiling_can_ever_raise_a_grant(
    proposed: int, product_cap: int, customer_cap: int
) -> None:
    """The load-bearing property. Adding a ceiling may only ever lower the
    number; if it could lift one, the committed promise would be breakable."""
    without = gate(proposed_bps=proposed)
    with_caps = gate(
        proposed_bps=proposed,
        product_cap_bps=product_cap,
        customer_cap_bps=customer_cap,
    )
    assert with_caps.granted_bps <= without.granted_bps
    assert with_caps.granted_bps <= product_cap
    assert with_caps.granted_bps <= customer_cap


@given(st.integers(min_value=0, max_value=10_000))
def test_a_grant_never_exceeds_the_product_cap(cap: int) -> None:
    assert gate(proposed_bps=10_000, product_cap_bps=cap).granted_bps <= cap


# --------------------------------------------- the cross-language contract --
def test_every_binding_value_has_a_label_in_the_frontend() -> None:
    """BINDING's values are keys in frontend/src/lib/plainLanguage.ts.

    This is the drift that already happened once and stayed silent for weeks:
    the backend sent `remaining_budget_paise` while the frontend keyed
    `budget_paise`, so every budget clamp rendered a generic fallback instead
    of the sentence written for it. Nothing failed and nothing logged -- the
    page just quietly said less than it knew.
    """
    import pathlib
    import re

    ts = (
        pathlib.Path(__file__).resolve().parents[2]
        / "frontend/src/lib/plainLanguage.ts"
    ).read_text()
    block = ts.split("export const LIMIT_LABEL", 1)[1].split("}", 1)[0]
    labelled = set(re.findall(r"^\s*([a-z_]+):", block, re.MULTILINE))

    assert set(BINDING.values()) == labelled, (
        "BINDING values and LIMIT_LABEL keys have drifted: "
        f"only in Python {set(BINDING.values()) - labelled}, "
        f"only in TypeScript {labelled - set(BINDING.values())}"
    )
