"""Who is standing at the counter.

A sticker is a shelf fixture, not a coupon: many people scan the same one over
its life. "Already used" therefore has to mean "already used by this person",
and that needs a person. The phone number is how a kirana already identifies a
regular, so it is what this uses.

Everything in this module is a pure function over its arguments. No clock, no
database, no model. That is deliberate and it is the same argument
`bounds.check()` makes: a rule that decides what someone is allowed to be
offered must be checkable by reading it, and testable without a fixture.

The phone number itself never reaches the model. It is collected in the UI
before the chat mounts and posted alongside the slot token; routing it through
a tool would put it into the model's context, into `decisions.raw_llm_output`,
and into the transcript the merchant console renders. Same posture as
`cost_paise`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: India only, and deliberately so. This is a kirana in Lajpat Nagar; accepting
#: arbitrary international numbers would mean accepting arbitrary lengths, and
#: the value of the check is that a typo fails loudly at the front door rather
#: than creating a second customer record for the same person.
DEFAULT_COUNTRY_CODE = "91"

#: Indian mobile numbers are ten digits and start 6-9. Landlines do not, and a
#: shopper cannot receive a redemption code on one.
_INDIAN_MOBILE = re.compile(r"^[6-9][0-9]{9}$")

#: What the column will accept. Kept in sync with the CHECK constraint in
#: sql/016_customers.sql by test_customer_service.py.
_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")

_STRIP = str.maketrans({c: None for c in " -()./ ‐‑‒–—"})


def normalize_phone(raw: str | None) -> str | None:
    """Fold what a person types into the one form the column stores.

    Returns None for anything that is not a plausible Indian mobile number,
    and None is a refusal rather than a fallback: storing a half-valid number
    means the same shopper gets two customer records and their history splits
    silently in half, which is worse than asking them to type it again.

    Accepts the shapes people actually write:

        9876543210            bare ten digits
        +91 98765 43210       spaced, with country code
        098765-43210          leading trunk zero, hyphenated
        0091 9876543210       the old international prefix
    """
    if raw is None:
        return None

    candidate = raw.strip().translate(_STRIP)
    if not candidate:
        return None

    if candidate.startswith("+"):
        digits, had_plus = candidate[1:], True
    else:
        digits, had_plus = candidate, False

    if not digits.isdigit():
        return None

    # 00 is the older way of writing +. Do this before the trunk-zero strip, or
    # "0091..." loses one zero and becomes an unrecognisable "091...".
    if not had_plus and digits.startswith("00"):
        digits = digits[2:]
    elif not had_plus and len(digits) == 11 and digits.startswith("0"):
        # Domestic trunk prefix: 0 then the ten-digit mobile.
        digits = digits[1:]

    if len(digits) == 10:
        digits = DEFAULT_COUNTRY_CODE + digits

    if not digits.startswith(DEFAULT_COUNTRY_CODE):
        return None

    national = digits[len(DEFAULT_COUNTRY_CODE):]
    if not _INDIAN_MOBILE.match(national):
        return None

    e164 = "+" + digits
    return e164 if _E164.match(e164) else None


def last4(e164: str) -> str:
    """The four digits a counter uses to match a person to a screen.

    The console shows this and never the full number: identifying a customer
    across a counter does not require publishing their phone into an audit
    feed that several people read.
    """
    return e164[-4:]


def masked(e164: str | None) -> str:
    """For anything a human reads. Never the whole number."""
    return f"…{last4(e164)}" if e164 else "not given"


# ============================================================== the band ====
#
# Two bands, and the rule is two comparisons. It is written twice on purpose
# and the duplication is bounded: SQL evaluates it inside open_session_by_token
# because that is where the snapshot is written, and this module evaluates it
# for the merchant-side preview ("how many of my shoppers would qualify?").
# The second question never decides anyone's price, so a drift between them
# costs a wrong preview rather than a wrong discount.

MAX_BPS = 10_000

#: The window presets the console offers. Days, because "three weeks" and
#: "last month" are just numbers and a shopkeeper wanting 45 should not need a
#: migration. None is lifetime.
WINDOW_PRESETS: dict[str, int | None] = {
    "3 weeks": 21,
    "last month": 30,
    "3 months": 90,
    "lifetime": None,
}


@dataclass(frozen=True)
class TierRule:
    """What the merchant set. Frozen at commit, alongside the ceilings."""

    min_txn_count: int = 0
    min_spend_paise: int = 0
    window_days: int | None = None
    #: What a shopper who does NOT qualify may reach, as a fraction of each
    #: product's own cap. 10000 = the whole thing, i.e. no reduction, which is
    #: the default so an unconfigured campaign behaves as it always has.
    base_cap_fraction_bps: int = MAX_BPS

    @property
    def configured(self) -> bool:
        """False when the merchant has not asked for tiers at all.

        Worth naming: with no thresholds AND no reduction, every shopper
        qualifies and the cap is untouched, so the whole feature is inert and
        the console should say so rather than implying a rule is in force.
        """
        return (
            self.min_txn_count > 0
            or self.min_spend_paise > 0
            or self.base_cap_fraction_bps < MAX_BPS
        )


@dataclass(frozen=True)
class CustomerStats:
    """What this shopper has actually done here, inside the window."""

    txn_count: int = 0
    spend_paise: int = 0
    identified: bool = False

    @classmethod
    def from_snapshot(cls, raw: dict[str, Any] | None) -> CustomerStats:
        """Read the snapshot written at session open.

        Tolerant of missing keys: sessions that predate the snapshot have none,
        and the honest reading of "no record" is "no history", not a crash.
        """
        raw = raw or {}
        return cls(
            txn_count=int(raw.get("txn_count") or 0),
            spend_paise=int(raw.get("spend_paise") or 0),
            identified=bool(raw.get("identified")),
        )


@dataclass(frozen=True)
class TierVerdict:
    key: str  # 'new' | 'preferred'
    qualifies: bool
    cap_fraction_bps: int
    reason: str


def evaluate_tier(rule: TierRule, stats: CustomerStats) -> TierVerdict:
    """Does this shopper reach the full ceiling, or a fraction of it?

    Pure: no clock, no database, no model. The time window is already applied
    by the time the stats arrive, precisely so that this function cannot give
    two different answers on two different days for the same inputs.

    An unidentified shopper is 'new'. That is not a punishment for declining to
    give a number -- it is the only honest answer, because there is no history
    to read.
    """
    if not stats.identified:
        return TierVerdict(
            key="new", qualifies=False,
            cap_fraction_bps=rule.base_cap_fraction_bps,
            reason="Not identified, so there is no history to go on.",
        )

    enough_visits = stats.txn_count >= rule.min_txn_count
    enough_spend = stats.spend_paise >= rule.min_spend_paise

    if enough_visits and enough_spend:
        return TierVerdict(
            key="preferred", qualifies=True, cap_fraction_bps=MAX_BPS,
            reason=(
                f"{stats.txn_count} purchases and "
                f"₹{stats.spend_paise / 100:,.2f} spent here."
            ),
        )

    missing = []
    if not enough_visits:
        missing.append(f"{rule.min_txn_count - stats.txn_count} more purchases")
    if not enough_spend:
        missing.append(f"₹{(rule.min_spend_paise - stats.spend_paise) / 100:,.2f} more spent")
    return TierVerdict(
        key="new", qualifies=False,
        cap_fraction_bps=rule.base_cap_fraction_bps,
        reason="Needs " + " and ".join(missing) + ".",
    )


def effective_cap_bps(product_cap_bps: int, cap_fraction_bps: int) -> int:
    """This shopper's ceiling on this product.

    Integer arithmetic and floor division, like every other money path here: a
    float in a ceiling is how a cap ends up one basis point above the number
    that was committed.
    """
    if product_cap_bps <= 0:
        return 0
    fraction = max(0, min(cap_fraction_bps, MAX_BPS))
    return product_cap_bps * fraction // MAX_BPS


def caps_for_item(
    cap_bps: int | None, tier_cap_fraction_bps: int | None
) -> tuple[int | None, int | None]:
    """The two committed ceilings for one product, as bounds.check wants them.

    THE single derivation. Four places gate an offer -- the human turn, the
    upsell, the accept re-gate, and the machine-buyer quote -- and two of them
    hold typed objects while two hold raw rpc dicts. Without one function they
    would each grow their own arithmetic, and a divergence is a discount an AI
    buyer could get that a human could not, which agent_commerce.py names as
    the exact failure this project exists to prevent.

    Returns (None, None) when the campaign predates per-product caps, which is
    what makes those campaigns behave exactly as they always have.
    """
    if cap_bps is None:
        return None, None
    fraction = MAX_BPS if tier_cap_fraction_bps is None else tier_cap_fraction_bps
    customer = effective_cap_bps(cap_bps, fraction)
    # A band that reduces nothing is not worth naming as the binding rule: it
    # would blame the shopper's standing for a ceiling their standing did not
    # cause.
    return cap_bps, (customer if customer < cap_bps else None)


def standing_phrase(stats: CustomerStats) -> str:
    """How the assistant is allowed to describe someone, out loud.

    Bands, never numbers. The agent may truthfully say "you shop here often";
    it may not say "you are on 12%", because a shopper who learns a percentage
    has learned a ceiling and the haggling is over.
    """
    if not stats.identified:
        return "a new face"
    if stats.txn_count >= 10:
        return "a regular here"
    if stats.txn_count >= 3:
        return "a returning customer"
    if stats.txn_count >= 1:
        return "been here once or twice"
    return "a first visit"
