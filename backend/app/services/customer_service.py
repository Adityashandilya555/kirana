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
