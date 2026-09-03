"""The basket's pure half.

`preview` is what the tools answer from mid-turn, before anything is written.
Every one of these is a rule that only shows up as a bug on a phone, several
messages into a conversation, which is exactly the kind of rule worth pinning
here instead.
"""

from __future__ import annotations

from app.services.cart_service import EMPTY, CartOp, normalise, preview


def line(sku: str, *, qty: int = 1, price: int = 10_000, bps: int = 500) -> dict:
    gross = price * qty
    discount = gross * bps // 10_000
    return {
        "sku": sku, "name": sku.title(), "unit": "pc", "qty": qty,
        "unit_price_paise": price, "gross_paise": gross,
        "granted_bps": bps, "discount_paise": discount,
        "line_total_paise": gross - discount, "binding_constraint": None,
    }


def cart(*lines: dict) -> dict:
    gross = sum(int(x["gross_paise"]) for x in lines)
    discount = sum(int(x["discount_paise"]) for x in lines)
    return {
        "cart_id": "c1", "status": "open", "items": list(lines),
        "count": len(lines), "gross_paise": gross,
        "discount_paise": discount, "total_paise": gross - discount,
    }


def op(sku: str, *, qty: int = 1, price: int = 10_000, bps: int = 500) -> CartOp:
    gross = price * qty
    discount = gross * bps // 10_000
    return CartOp(
        sku=sku, qty=qty, granted_bps=bps, discount_paise=discount,
        line_total_paise=gross - discount, unit_price_paise=price,
        name=sku.title(), unit="pc", decision_code="OK_AS_PROPOSED",
    )


# ------------------------------------------------------------- normalise ----
def test_a_missing_cart_reads_as_an_empty_one() -> None:
    """get_cart returns SQL NULL on turn one, which is the common case. An
    empty basket and a missing basket are the same thing to everyone above."""
    assert normalise(None) == EMPTY
    assert normalise({"items": None, "count": None}) == EMPTY


# ---------------------------------------------------------------- adding ----
def test_the_first_item_is_the_whole_basket() -> None:
    out = preview(dict(EMPTY), {"ATTA": op("ATTA", price=28_500)})
    assert out["count"] == 1
    assert out["gross_paise"] == 28_500
    assert out["discount_paise"] == 1_425
    assert out["total_paise"] == 27_075


def test_a_second_item_does_not_replace_the_first() -> None:
    """The bug this whole feature exists for. Negotiating oil after atta used
    to leave a shopper holding one offer for the oil and no atta at all."""
    out = preview(cart(line("ATTA")), {"OIL": op("OIL")})
    assert {i["sku"] for i in out["items"]} == {"ATTA", "OIL"}
    assert out["count"] == 2


def test_each_line_keeps_its_own_rate() -> None:
    out = preview(cart(line("ATTA", bps=500)), {"OIL": op("OIL", bps=600)})
    rates = {i["sku"]: i["granted_bps"] for i in out["items"]}
    assert rates == {"ATTA": 500, "OIL": 600}


def test_the_same_item_twice_is_one_line() -> None:
    out = preview(cart(line("ATTA", qty=1)), {"ATTA": op("ATTA", qty=3)})
    assert out["count"] == 1
    assert out["items"][0]["qty"] == 3


# ------------------------------------------------------- never backwards ----
def test_a_worse_requote_does_not_take_a_granted_rate_away() -> None:
    """A shopper was TOLD they had 6%. A later turn that happens to re-price
    the same item at 5% must not quietly reduce it -- that is the one thing
    that would make the negotiation feel like a trick."""
    out = preview(cart(line("OIL", bps=600)), {"OIL": op("OIL", bps=300)})
    assert out["items"][0]["granted_bps"] == 600


def test_a_better_requote_does_improve_the_line() -> None:
    out = preview(cart(line("OIL", bps=300)), {"OIL": op("OIL", bps=600)})
    assert out["items"][0]["granted_bps"] == 600


def test_the_kept_rate_reprices_the_line_against_the_new_quantity() -> None:
    """Holding the rate must not hold the old total: 6% of two bottles is not
    6% of one, and a line whose amount does not match its own quantity is a
    bill nobody can explain."""
    out = preview(
        cart(line("OIL", qty=1, price=14_500, bps=600)),
        {"OIL": op("OIL", qty=2, price=14_500, bps=300)},
    )
    kept = out["items"][0]
    assert kept["granted_bps"] == 600
    assert kept["gross_paise"] == 29_000
    assert kept["discount_paise"] == 1_740
    assert kept["line_total_paise"] == 27_260


# -------------------------------------------------------------- removing ----
def test_removing_takes_the_line_and_its_money_out() -> None:
    out = preview(
        cart(line("ATTA", price=28_500), line("OIL", price=14_500)),
        {"OIL": CartOp(sku="OIL", remove=True)},
    )
    assert [i["sku"] for i in out["items"]] == ["ATTA"]
    assert out["gross_paise"] == 28_500


def test_removing_something_absent_is_not_an_error() -> None:
    out = preview(cart(line("ATTA")), {"TEA": CartOp(sku="TEA", remove=True)})
    assert out["count"] == 1


def test_adding_then_removing_inside_one_turn_leaves_nothing_behind() -> None:
    """The ops dict is keyed by sku, so the last word in a turn wins. A model
    that adds tea and is then told "no, not the tea" must not commit it."""
    ops = {"TEA": op("TEA")}
    ops["TEA"] = CartOp(sku="TEA", remove=True)
    assert preview(dict(EMPTY), ops)["count"] == 0


# ---------------------------------------------------------------- totals ----
def test_totals_are_summed_not_adjusted() -> None:
    """Recomputed from the lines every time, so a total can never drift away
    from the lines that are supposed to add up to it."""
    out = preview(
        cart(line("ATTA", price=28_500, bps=500)),
        {"OIL": op("OIL", qty=2, price=14_500, bps=600)},
    )
    assert out["gross_paise"] == sum(i["gross_paise"] for i in out["items"])
    assert out["discount_paise"] == sum(i["discount_paise"] for i in out["items"])
    assert out["total_paise"] == out["gross_paise"] - out["discount_paise"]
    assert out["total_paise"] == sum(i["line_total_paise"] for i in out["items"])


def test_an_empty_basket_totals_zero_rather_than_raising() -> None:
    out = preview(dict(EMPTY), {})
    assert out["count"] == 0 and out["total_paise"] == 0
