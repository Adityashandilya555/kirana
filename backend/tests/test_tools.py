"""The five tools, and the contract that makes the gate work.

tools.py is the largest module in the project and had no direct tests. The
properties below are the ones the whole security argument rests on: the model
never learns a cost, never learns its ceiling except as an instruction to
re-propose, and cannot price something outside its slot's scope.
"""

from __future__ import annotations

import json

import pytest

from app.core import tools as toolsmod
from app.core.tools import CatalogItem, OfferContext

TEA = CatalogItem(
    sku="TEA250", name="Tata Tea Gold 250g", unit="pack",
    price_paise=19000, cost_paise=13500,
)
SUGAR = CatalogItem(
    sku="SUGAR1", name="Sugar 1kg", unit="pack",
    price_paise=4800, cost_paise=4300,
)


def _ctx(**kw) -> OfferContext:
    base = dict(
        catalog=[TEA, SUGAR],
        slot_ceiling_bps=1200,
        campaign_max_discount_bps=2000,
        margin_floor_bps=1200,
        budget_paise=500_000,
    )
    base.update(kw)
    return OfferContext(**base)


def _call(ctx: OfferContext, name: str, **args) -> dict:
    _, tool_map = toolsmod.build_tools(ctx)
    return json.loads(tool_map[name].invoke(args))


# ------------------------------------------------------ what must not leak --
def test_no_tool_ever_returns_a_cost() -> None:
    """cost_paise reaches the gate but must never reach the model. If it did,
    an injection could ask for it and the shop's margins would be public."""
    ctx = _ctx()
    _, tool_map = toolsmod.build_tools(ctx)
    for name, args in [
        ("list_catalog", {}),
        ("find_item", {"query": "tea"}),
        ("get_item_detail", {"sku": "TEA250"}),
        ("price_quote", {"sku": "TEA250", "qty": 1, "discount_bps": 500}),
        ("propose_offer", {"sku": "TEA250", "qty": 1, "discount_bps": 500,
                           "message": "here you go"}),
    ]:
        blob = str(tool_map[name].invoke(args))
        assert "cost" not in blob.lower(), name
        assert "13500" not in blob, name


def test_the_system_prompt_carries_no_cost_value() -> None:
    """The VALUE, not the word.

    The prompt does contain "costs" -- in the instruction never to discuss
    them -- so asserting on the substring fails against correct code. What
    must be absent is the number: 13500 and 4300 are what the gate uses and
    what the model must never see.
    """
    prompt = toolsmod.render_system_prompt("Shop", "Somewhere", [TEA, SUGAR])
    assert "13500" not in prompt
    assert "4300" not in prompt
    # The selling prices, by contrast, are deliberately seeded in.
    assert "190" in prompt


# ------------------------------------------------- the refuse-and-explain --
def test_a_clamp_tells_the_model_the_number_it_may_ask_for() -> None:
    """The find_student pattern: a refusal that names the maximum, so the model
    re-proposes from the error text instead of arguing."""
    ctx = _ctx(slot_ceiling_bps=600)
    out = _call(ctx, "propose_offer", sku="TEA250", qty=1, discount_bps=5000,
                message="how about half off")
    assert out["granted_bps"] == 600
    assert out["max_allowed_bps"] == 600
    assert "600" in out["reason"]


def test_the_model_does_see_max_allowed_even_though_the_customer_does_not() -> None:
    """The split the ceiling-leak fix depends on: this field is the model's
    instruction and must stay here, while chat_service omits it from the
    customer payload."""
    out = _call(_ctx(), "propose_offer", sku="TEA250", qty=1, discount_bps=9000,
                message="")
    assert "max_allowed_bps" in out


def test_an_approved_offer_is_a_quote_not_a_reservation() -> None:
    ctx = _ctx()
    _call(ctx, "propose_offer", sku="TEA250", qty=1, discount_bps=500, message="")
    # Nothing about the context's budget moved: the tool computes, it does not
    # commit. Reserving happens later, server-side, after a second gate run.
    assert ctx.spent_paise == 0
    assert ctx.reserved_paise == 0


# ------------------------------------------------------- forced dependency --
def test_pricing_an_unknown_sku_directs_the_model_to_find_item() -> None:
    for name in ("get_item_detail", "price_quote", "propose_offer"):
        args = {"sku": "NOPE"}
        if name != "get_item_detail":
            args |= {"qty": 1, "discount_bps": 100}
        if name == "propose_offer":
            args |= {"message": ""}
        out = _call(_ctx(), name, **args)
        assert "find_item" in json.dumps(out), name


def test_find_item_lists_what_exists_when_it_finds_nothing() -> None:
    out = _call(_ctx(), "find_item", query="motorcycle")
    assert out["found"] is False
    assert {i["sku"] for i in out["available"]} == {"TEA250", "SUGAR1"}


# --------------------------------------------------------------- the gate --
def test_the_margin_floor_refuses_an_item_it_cannot_clear() -> None:
    """Sugar's margin is 10.4%, below a 12% floor. No discount is possible."""
    out = _call(_ctx(), "propose_offer", sku="SUGAR1", qty=1, discount_bps=100,
                message="")
    assert out["approved"] is False
    assert "MARGIN" in out["code"]


def test_quantity_outside_the_allowed_range_is_refused() -> None:
    out = _call(_ctx(), "price_quote", sku="TEA250", qty=99, discount_bps=0)
    assert out["ok"] is False
    assert out["code"] == "QTY_OUT_OF_RANGE"


def test_arithmetic_is_exact_integer_paise() -> None:
    out = _call(_ctx(), "price_quote", sku="TEA250", qty=2, discount_bps=1000)
    # 19000 * 2 = 38000; 10% = 3800; payable 34200
    assert out["payable_paise"] == 34200


# -------------------------------------------------------------- the scope --
def test_a_scoped_catalog_hides_everything_else() -> None:
    """A shelf-bound slot's context carries only its shelf, so the tools cannot
    reach past it -- there is no filtering here to forget, the list is short."""
    ctx = _ctx(catalog=[TEA])
    assert _call(ctx, "find_item", query="sugar")["found"] is False
    assert _call(ctx, "get_item_detail", sku="SUGAR1")["found"] is False


def test_scope_note_names_the_shelf_for_the_prompt() -> None:
    note = toolsmod.scope_note(
        {"bound_sku": None, "shelf_name": "Tea & Beverages"}, [TEA]
    )
    assert "Tea & Beverages" in note


def test_an_unbound_slot_has_no_scope_note() -> None:
    assert toolsmod.scope_note({"bound_sku": None, "shelf_name": None}, [TEA]) == ""


# -------------------------------------------------------------- the upsell --
def test_an_addon_goes_through_the_same_gate() -> None:
    ctx = _ctx()
    out = _call(ctx, "suggest_addon", sku="TEA250")
    # Sugar is the only other item and cannot clear the floor, so the add-on is
    # withheld -- the bound applying to upsells, not only to discounts.
    assert out["suggested"] is False
    assert ctx.last_addon is not None


def test_an_addon_is_never_drawn_from_outside_the_scope() -> None:
    ctx = _ctx(catalog=[TEA])
    out = _call(ctx, "suggest_addon", sku="TEA250")
    assert out["suggested"] is False
    assert out["code"] == "NOTHING_TO_ADD"


def test_tool_calls_are_recorded_for_the_audit_trail() -> None:
    ctx = _ctx()
    _call(ctx, "find_item", query="tea")
    assert [c.name for c in ctx.calls] == ["find_item"]
