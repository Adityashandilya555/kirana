"""The gate. Written before the implementation, deliberately.

This is the file that carries the security argument. The model can ask for
anything; what it actually gets is decided here, by a pure function with no
network, no randomness and no model in it.

Real catalog numbers from sql/003_seed.sql, so the margin cases are the ones
the demo will actually hit:

    sku      price   cost   gross margin
    SUGAR1     4800   4300     1041 bps   <- below a 1200 floor: no discount possible
    OIL1L     14500  12800     1172 bps   <- also below
    ATTA5     28500  24500     1403 bps
    TEA250    19000  13500     2894 bps   <- room to haggle
"""

from __future__ import annotations

import pytest

from app.core.bounds import BoundsInput, check, margin_bps_after
from app.core.codes import BINDING, OK_CODES, BoundsCode

ATTA5 = dict(price_paise=28500, cost_paise=24500)
TEA250 = dict(price_paise=19000, cost_paise=13500)
SUGAR1 = dict(price_paise=4800, cost_paise=4300)


def inp(**over) -> BoundsInput:
    """A permissive baseline; each test narrows exactly one thing."""
    base = dict(
        proposed_bps=1000,
        price_paise=19000,
        cost_paise=13500,
        qty=1,
        slot_ceiling_bps=2000,
        slot_status="unused",
        campaign_status="live",
        campaign_max_discount_bps=2000,
        margin_floor_bps=1200,
        budget_paise=500_000,
        spent_paise=0,
        reserved_paise=0,
        turn_count=0,
        max_turns=6,
    )
    base.update(over)
    return BoundsInput(**base)


# ---------------------------------------------------------------------------
# The table. One row per rule, plus the orderings that matter.
# ---------------------------------------------------------------------------
CASES = [
    # id,                     input overrides,                                   expected code,                       expected granted
    ("within_all_bounds",     dict(proposed_bps=1000),                            BoundsCode.OK_AS_PROPOSED,            1000),
    ("exactly_at_ceiling",    dict(proposed_bps=1200, slot_ceiling_bps=1200),     BoundsCode.OK_AS_PROPOSED,            1200),
    ("zero_discount_ok",      dict(proposed_bps=0),                               BoundsCode.OK_AS_PROPOSED,            0),

    ("over_slot_ceiling",     dict(proposed_bps=6000, slot_ceiling_bps=1200),     BoundsCode.OK_CLAMPED_SLOT_CEILING,   1200),
    ("absurd_ask_clamped",    dict(proposed_bps=9000, slot_ceiling_bps=500),      BoundsCode.OK_CLAMPED_SLOT_CEILING,   500),
    # Slot ceiling is generous, campaign maximum is the tighter bound.
    ("over_campaign_max",     dict(proposed_bps=1800, slot_ceiling_bps=2000,
                                   campaign_max_discount_bps=1500),               BoundsCode.OK_CLAMPED_CAMPAIGN_CEILING, 1500),

    # Budget: only 100 paise of headroom left on a 19000 item = 52 bps.
    ("budget_is_tightest",    dict(proposed_bps=2000, budget_paise=500_000,
                                   spent_paise=499_900),                          BoundsCode.OK_CLAMPED_BUDGET,         52),

    # -- hard refusals: no offer exists at all ------------------------------
    ("turn_limit",            dict(turn_count=6, max_turns=6),                    BoundsCode.TURN_LIMIT,                0),
    ("turn_limit_exceeded",   dict(turn_count=9, max_turns=6),                    BoundsCode.TURN_LIMIT,                0),
    ("slot_redeemed",         dict(slot_status="redeemed"),                       BoundsCode.SLOT_NOT_OPEN,             0),
    ("slot_locked",           dict(slot_status="locked"),                         BoundsCode.SLOT_NOT_OPEN,             0),
    ("slot_void",             dict(slot_status="void"),                           BoundsCode.SLOT_NOT_OPEN,             0),
    ("campaign_closed",       dict(campaign_status="closed"),                     BoundsCode.CAMPAIGN_NOT_LIVE,         0),
    ("campaign_draft",        dict(campaign_status="draft"),                      BoundsCode.CAMPAIGN_NOT_LIVE,         0),
    ("budget_exhausted",      dict(budget_paise=500_000, spent_paise=500_000),    BoundsCode.BUDGET_EXHAUSTED,          0),
    ("budget_all_reserved",   dict(budget_paise=500_000, reserved_paise=500_000), BoundsCode.BUDGET_EXHAUSTED,          0),
    ("qty_zero",              dict(qty=0),                                        BoundsCode.QTY_OUT_OF_RANGE,          0),
    ("qty_absurd",            dict(qty=999),                                      BoundsCode.QTY_OUT_OF_RANGE,          0),
    # SUGAR1's gross margin (1041 bps) is already under the 1200 floor, so even
    # a zero-percent discount fails. The item simply cannot be discounted.
    ("margin_blocks_all",     dict(proposed_bps=500, **SUGAR1),                   BoundsCode.MARGIN_FLOOR_BLOCKS_ALL,   0),

    # -- ordering: the earlier rule wins ------------------------------------
    ("turns_beat_slot",       dict(turn_count=6, slot_status="redeemed"),         BoundsCode.TURN_LIMIT,                0),
    ("slot_beats_campaign",   dict(slot_status="redeemed", campaign_status="closed"), BoundsCode.SLOT_NOT_OPEN,         0),
    ("hard_rule_beats_clamp", dict(turn_count=6, proposed_bps=9000),              BoundsCode.TURN_LIMIT,                0),
]


@pytest.mark.parametrize(
    "overrides,expected_code,expected_granted",
    [c[1:] for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_bounds_table(overrides, expected_code, expected_granted):
    d = check(inp(**overrides))
    assert d.code is expected_code
    assert d.granted_bps == expected_granted
    assert d.approved is (expected_code in OK_CODES)


def test_margin_floor_clamps_rather_than_refuses():
    """TEA250 has room, but not 20% of room. The gate finds the maximum."""
    d = check(inp(proposed_bps=2000, slot_ceiling_bps=2000,
                  campaign_max_discount_bps=2000, margin_floor_bps=1200, **TEA250))
    assert d.code is BoundsCode.OK_CLAMPED_MARGIN_FLOOR
    assert d.approved
    assert 0 < d.granted_bps < 2000


def test_clamped_result_is_maximal():
    """Not merely safe -- the best offer the rules permit.

    A gate that always returned zero would pass every safety assertion and be
    useless, so this pins the other side: one more basis point must break it.
    """
    d = check(inp(proposed_bps=2000, margin_floor_bps=1200, **TEA250))
    assert margin_bps_after(19000, 13500, d.granted_bps) >= 1200
    assert margin_bps_after(19000, 13500, d.granted_bps + 1) < 1200


@pytest.mark.parametrize("code", sorted(BINDING))
def test_every_clamp_names_the_rule_that_bound_it(code):
    assert BINDING[code], f"{code} has no binding_constraint mapping"


@pytest.mark.parametrize("proposed", [0, 1, 500, 1200, 2000, 5000, 9999, 10000])
def test_granted_never_exceeds_the_committed_slot_ceiling(proposed):
    """The single invariant the whole pitch rests on."""
    d = check(inp(proposed_bps=proposed, slot_ceiling_bps=1200))
    assert d.granted_bps <= 1200


@pytest.mark.parametrize("proposed", [-1, -100, -10_000])
def test_negative_proposals_are_floored_at_zero(proposed):
    """A negative discount is a price increase. Never emit one."""
    assert check(inp(proposed_bps=proposed)).granted_bps >= 0


@pytest.mark.parametrize("proposed", [10_001, 50_000, 10**9])
def test_proposals_above_100_percent_are_clamped(proposed):
    d = check(inp(proposed_bps=proposed, slot_ceiling_bps=2000))
    assert d.granted_bps <= 2000


def test_every_decision_carries_both_audiences():
    for overrides, _, _ in [c[1:] for c in CASES]:
        d = check(inp(**overrides))
        assert d.reason.strip(), "merchant audit needs a sentence"
        assert d.customer_reason.strip(), "the phone needs a sentence"
        assert d.reason != d.customer_reason or not d.approved


def test_money_is_consistent_with_the_grant():
    d = check(inp(proposed_bps=1000, qty=2, **ATTA5))
    gross = 28500 * 2
    assert d.discount_paise == gross * d.granted_bps // 10_000
    assert d.final_amount_paise == gross - d.discount_paise


def test_check_is_pure():
    """Same input, same answer, forever. No clock, no randomness, no I/O."""
    i = inp(proposed_bps=6000, slot_ceiling_bps=1200)
    first = check(i)
    for _ in range(50):
        assert check(i) == first


# ---------------------------------------------------------------------------
# Property-based. The table above proves the cases I thought of; this proves
# the invariant against cases I did not.
# ---------------------------------------------------------------------------
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

_money = st.integers(min_value=100, max_value=10_000_000)
_bps = st.integers(min_value=0, max_value=10_000)


@settings(max_examples=400)
@given(
    proposed=st.integers(min_value=-10_000, max_value=100_000),
    price=_money,
    cost=_money,
    qty=st.integers(min_value=1, max_value=20),
    slot_ceiling=_bps,
    campaign_max=_bps,
    margin_floor=st.integers(min_value=0, max_value=9_000),
    budget=st.integers(min_value=0, max_value=10_000_000),
    spent=st.integers(min_value=0, max_value=10_000_000),
    turns=st.integers(min_value=0, max_value=10),
)
def test_invariants_hold_for_arbitrary_input(
    proposed, price, cost, qty, slot_ceiling, campaign_max, margin_floor,
    budget, spent, turns,
):
    d = check(inp(
        proposed_bps=proposed, price_paise=price, cost_paise=cost, qty=qty,
        slot_ceiling_bps=slot_ceiling, campaign_max_discount_bps=campaign_max,
        margin_floor_bps=margin_floor, budget_paise=budget, spent_paise=spent,
        turn_count=turns, max_turns=6,
    ))

    # 1. THE invariant. Nothing may exceed what the merchant committed to.
    assert d.granted_bps <= slot_ceiling or not d.approved

    # 2. Never a negative discount -- that would be a price increase.
    assert 0 <= d.granted_bps <= 10_000

    # 3. Never more than the campaign envelope.
    assert d.granted_bps <= campaign_max or not d.approved

    # 4. An approved discount always clears the margin floor.
    if d.approved and d.granted_bps > 0:
        assert margin_bps_after(price, cost, d.granted_bps) >= margin_floor

    # 5. An approved discount never overspends the remaining budget.
    if d.approved:
        assert d.discount_paise <= max(0, budget - spent)

    # 6. Money always reconciles.
    assert d.discount_paise + d.final_amount_paise == price * qty
    assert d.discount_paise >= 0 and d.final_amount_paise >= 0

    # 7. A refusal never carries an offer.
    if not d.approved:
        assert d.granted_bps == 0 and d.discount_paise == 0
