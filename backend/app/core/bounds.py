"""The deterministic gate.

The whole security argument reduces to this file. A language model can ask
for any discount it likes; what a customer actually receives is decided here,
by a pure function with no network, no clock, no randomness and no model in
it. The model's output is an *input* to this function, never an output of it.

Two stages, in this order:

  Stage A -- hard refusals. Conditions under which no offer exists at all.
             Evaluated in a fixed order; the first one that fires wins, so a
             session that is both out of turns and pointing at a redeemed slot
             reports the turn limit rather than whichever check ran first.

  Stage B -- ceilings. Four independent upper bounds are computed and the
             smallest wins. The result is clamped to it, and the bound that
             produced the number is named, because "12%" without "because the
             shelf code is capped at 12%" is not an explanation.

Stage B clamps rather than refuses on purpose. `propose_offer` hands the
refusal back to the model with `max_allowed_bps` attached, so it re-proposes
inside the bound instead of arguing -- the same trick the placement agent used
when `get_student_profile` rejected a roll number and named the tool to call
first.

All arithmetic is integer. Money is paise, rates are basis points. A float
anywhere in a money path is a bug.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.codes import BINDING, OK_CODES, BoundsCode

MAX_BPS = 10_000
MAX_QTY = 20


@dataclass(frozen=True)
class BoundsInput:
    proposed_bps: int
    price_paise: int
    cost_paise: int
    qty: int = 1
    slot_ceiling_bps: int = 0
    slot_status: str = "unused"
    campaign_status: str = "live"
    campaign_max_discount_bps: int = 0
    margin_floor_bps: int = 0
    budget_paise: int = 0
    spent_paise: int = 0
    reserved_paise: int = 0
    turn_count: int = 0
    max_turns: int = 6

    @property
    def gross_paise(self) -> int:
        return self.price_paise * self.qty

    @property
    def remaining_budget_paise(self) -> int:
        return self.budget_paise - self.spent_paise - self.reserved_paise


@dataclass(frozen=True)
class Decision:
    approved: bool
    code: BoundsCode
    granted_bps: int
    max_allowed_bps: int
    binding_constraint: str | None
    reason: str           # merchant audit: full detail, names the rule
    customer_reason: str  # the phone: one plain sentence
    discount_paise: int
    final_amount_paise: int
    proposed_bps: int


# ---------------------------------------------------------------------------
# Margin arithmetic
# ---------------------------------------------------------------------------
def margin_bps_after(price_paise: int, cost_paise: int, discount_bps: int) -> int:
    """Gross margin in bps once `discount_bps` is applied.

    Margin is measured on the *sale* price, not on cost -- which is the
    convention a shopkeeper actually uses and the one the campaign's
    margin_floor_bps is expressed in.
    """
    sale = price_paise * (MAX_BPS - discount_bps) // MAX_BPS
    if sale <= 0:
        return -MAX_BPS
    return (sale - cost_paise) * MAX_BPS // sale


def max_discount_for_margin(price_paise: int, cost_paise: int, floor_bps: int) -> int:
    """Largest discount that still clears the margin floor, or -1 if none does.

    Binary search rather than the closed form: margin is monotonically
    decreasing in the discount, and searching sidesteps the integer-rounding
    edge where the analytic answer is off by one basis point in the unsafe
    direction. Fourteen iterations is not a hot path.
    """
    if margin_bps_after(price_paise, cost_paise, 0) < floor_bps:
        return -1
    lo, hi = 0, MAX_BPS
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if margin_bps_after(price_paise, cost_paise, mid) >= floor_bps:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _pct(bps: int) -> str:
    return f"{bps / 100:g}%"


def _rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def _refuse(i: BoundsInput, code: BoundsCode, reason: str, customer: str) -> Decision:
    return Decision(
        approved=False,
        code=code,
        granted_bps=0,
        max_allowed_bps=0,
        binding_constraint=None,
        reason=reason,
        customer_reason=customer,
        discount_paise=0,
        final_amount_paise=i.gross_paise,
        proposed_bps=i.proposed_bps,
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def check(i: BoundsInput) -> Decision:
    # -- Stage A: is any offer possible at all? -----------------------------
    if i.turn_count >= i.max_turns:
        return _refuse(
            i, BoundsCode.TURN_LIMIT,
            f"Turn limit reached: {i.turn_count} of {i.max_turns} used.",
            "We have gone back and forth a few times -- this is my final price.",
        )

    if i.slot_status not in {"unused", "offered"}:
        return _refuse(
            i, BoundsCode.SLOT_NOT_OPEN,
            f"Slot is '{i.slot_status}', not open for a new offer.",
            "This code has already been used.",
        )

    if i.campaign_status != "live":
        return _refuse(
            i, BoundsCode.CAMPAIGN_NOT_LIVE,
            f"Campaign status is '{i.campaign_status}'.",
            "This offer has ended.",
        )

    if not (1 <= i.qty <= MAX_QTY):
        return _refuse(
            i, BoundsCode.QTY_OUT_OF_RANGE,
            f"Quantity {i.qty} outside 1..{MAX_QTY}.",
            f"I can price between 1 and {MAX_QTY} units at a time.",
        )

    if i.remaining_budget_paise <= 0:
        return _refuse(
            i, BoundsCode.BUDGET_EXHAUSTED,
            f"Promo budget exhausted: {_rupees(i.spent_paise)} spent and "
            f"{_rupees(i.reserved_paise)} reserved of {_rupees(i.budget_paise)}.",
            "Today's discount budget is finished, so this is at full price.",
        )

    margin_ceiling = max_discount_for_margin(
        i.price_paise, i.cost_paise, i.margin_floor_bps
    )
    if margin_ceiling < 0:
        # The item is already below the floor at full price. No discount, however
        # small, can satisfy the rule -- so this is a refusal, not a clamp.
        return _refuse(
            i, BoundsCode.MARGIN_FLOOR_BLOCKS_ALL,
            f"Margin at list price is "
            f"{_pct(margin_bps_after(i.price_paise, i.cost_paise, 0))}, already below "
            f"the {_pct(i.margin_floor_bps)} floor. No discount is possible.",
            "I cannot go below cost on this one -- it is already keenly priced.",
        )

    # -- Stage B: four ceilings, smallest wins ------------------------------
    budget_ceiling = i.remaining_budget_paise * MAX_BPS // i.gross_paise

    ceilings: list[tuple[int, BoundsCode]] = [
        (i.slot_ceiling_bps, BoundsCode.OK_CLAMPED_SLOT_CEILING),
        (i.campaign_max_discount_bps, BoundsCode.OK_CLAMPED_CAMPAIGN_CEILING),
        (margin_ceiling, BoundsCode.OK_CLAMPED_MARGIN_FLOOR),
        (budget_ceiling, BoundsCode.OK_CLAMPED_BUDGET),
    ]
    max_allowed = min(c for c, _ in ceilings)

    wanted = max(0, min(i.proposed_bps, MAX_BPS))
    granted = min(wanted, max_allowed)

    if granted >= wanted:
        code = BoundsCode.OK_AS_PROPOSED
        binding = None
        reason = (
            f"Approved {_pct(granted)} as proposed; every bound left room "
            f"(headroom to {_pct(max_allowed)})."
        )
        customer = f"Done -- {_pct(granted)} off."
    else:
        # Ties resolve in listed order, so the most specific promise the
        # merchant made -- this particular shelf code -- gets named first.
        code = next(c for value, c in ceilings if value == max_allowed)
        binding = BINDING[code]
        reason = (
            f"Model proposed {_pct(i.proposed_bps)}; clamped to {_pct(granted)} by "
            f"{binding}. Ceilings: slot {_pct(i.slot_ceiling_bps)}, campaign "
            f"{_pct(i.campaign_max_discount_bps)}, margin {_pct(margin_ceiling)}, "
            f"budget {_pct(budget_ceiling)}."
        )
        customer = f"The best this code allows is {_pct(granted)} off."

    discount = i.gross_paise * granted // MAX_BPS
    return Decision(
        approved=code in OK_CODES,
        code=code,
        granted_bps=granted,
        max_allowed_bps=max_allowed,
        binding_constraint=binding,
        reason=reason,
        customer_reason=customer,
        discount_paise=discount,
        final_amount_paise=i.gross_paise - discount,
        proposed_bps=i.proposed_bps,
    )
