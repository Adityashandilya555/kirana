"""Phone normalisation.

The stake here is not validation, it is identity. A number that folds two ways
means one shopper becomes two customer records, their purchase history splits
silently in half, and the tier that history feeds gets the wrong answer with no
error anywhere. So the property that matters most is idempotence: normalising
an already-normalised number must be a no-op, and every shape of the same
number must land on the same string.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.services import customer_service as cs

#: Must stay identical to the CHECK constraint in sql/016_customers.sql. If
#: this and the column disagree, normalize_phone starts producing values the
#: database rejects at insert time, which surfaces as a 500 on a scan.
COLUMN_CHECK = re.compile(r"^\+[1-9][0-9]{7,14}$")


# ------------------------------------------------------------ the shapes ----
@pytest.mark.parametrize(
    "raw",
    [
        "9876543210",
        "+919876543210",
        "+91 98765 43210",
        "+91-98765-43210",
        "09876543210",
        "0091 9876543210",
        "  9876543210  ",
        "(98765) 43210",
        "98765.43210",
        "+91 (98765) 43210",
    ],
)
def test_every_way_a_person_writes_it_folds_to_one_string(raw: str) -> None:
    assert cs.normalize_phone(raw) == "+919876543210"


def test_normalising_twice_changes_nothing() -> None:
    once = cs.normalize_phone("098765 43210")
    assert once is not None
    assert cs.normalize_phone(once) == once


def test_the_trunk_zero_and_the_old_international_prefix_are_different() -> None:
    """0091... must not lose a digit to the trunk-zero rule and become 091..."""
    assert cs.normalize_phone("00919876543210") == "+919876543210"
    assert cs.normalize_phone("09876543210") == "+919876543210"


# ------------------------------------------------------------- refusals -----
@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "12345",             # too short
        "98765432101234",    # too long
        "5876543210",        # Indian mobiles start 6-9
        "1234567890",        # ditto
        "abcdefghij",
        "+1 415 555 2671",   # not an Indian mobile
        "+919876543210x",
        "98765 4321",        # nine digits
        "0119876543210",     # landline-shaped
    ],
)
def test_anything_not_a_plausible_indian_mobile_is_refused(raw: str | None) -> None:
    # None, not a best guess. A half-read number is how one shopper becomes
    # two customers.
    assert cs.normalize_phone(raw) is None


# ------------------------------------------------- agreement with the DB ----
@given(st.integers(min_value=6_000_000_000, max_value=9_999_999_999))
def test_every_accepted_value_satisfies_the_column_constraint(n: int) -> None:
    out = cs.normalize_phone(str(n))
    assert out is not None
    assert COLUMN_CHECK.match(out), out


@given(st.text(max_size=30))
def test_never_returns_something_the_column_would_reject(raw: str) -> None:
    out = cs.normalize_phone(raw)
    assert out is None or COLUMN_CHECK.match(out)


# ------------------------------------------------------------- masking ------
def test_only_the_last_four_digits_are_ever_shown() -> None:
    e164 = cs.normalize_phone("9876543210")
    assert e164 is not None
    assert cs.last4(e164) == "3210"
    assert cs.masked(e164) == "…3210"
    # The whole point: the full number must not appear in the display form.
    assert "9876543210" not in cs.masked(e164)


def test_masking_an_absent_number_says_so() -> None:
    assert cs.masked(None) == "not given"


# ============================================================== the band ====
#
# The rule is two comparisons, which is easy to get right and easy to get
# subtly wrong at the boundary. These pin the boundary, the unidentified case,
# and the one property that matters downstream: a band can only ever LOWER a
# ceiling, never raise one.

from app.services.customer_service import (  # noqa: E402
    MAX_BPS,
    CustomerStats,
    TierRule,
    effective_cap_bps,
    evaluate_tier,
    standing_phrase,
)

REGULAR = TierRule(min_txn_count=3, min_spend_paise=100_000, base_cap_fraction_bps=5_000)


def test_meeting_both_thresholds_qualifies() -> None:
    v = evaluate_tier(REGULAR, CustomerStats(5, 250_000, identified=True))
    assert v.qualifies and v.key == "preferred"
    assert v.cap_fraction_bps == MAX_BPS


def test_the_threshold_is_inclusive() -> None:
    """Exactly the stated number qualifies. "3 purchases" must mean 3, not 4."""
    v = evaluate_tier(REGULAR, CustomerStats(3, 100_000, identified=True))
    assert v.qualifies


@pytest.mark.parametrize(
    ("txns", "spend"),
    [(2, 250_000), (5, 99_999), (2, 99_999), (0, 0)],
)
def test_missing_either_threshold_does_not_qualify(txns: int, spend: int) -> None:
    v = evaluate_tier(REGULAR, CustomerStats(txns, spend, identified=True))
    assert not v.qualifies and v.key == "new"
    assert v.cap_fraction_bps == 5_000


def test_an_unidentified_shopper_is_new_however_rich_the_stats_look() -> None:
    # Stats without identity are meaningless; the guard must not be bypassable
    # by a caller that fills the numbers in anyway.
    v = evaluate_tier(REGULAR, CustomerStats(99, 9_999_999, identified=False))
    assert not v.qualifies and v.key == "new"
    assert "no history" in v.reason


def test_the_refusal_says_what_is_missing() -> None:
    v = evaluate_tier(REGULAR, CustomerStats(1, 40_000, identified=True))
    assert "2 more purchases" in v.reason
    assert "600.00 more spent" in v.reason


def test_an_unconfigured_rule_is_inert_and_says_so() -> None:
    assert not TierRule().configured
    assert TierRule(min_txn_count=1).configured
    assert TierRule(base_cap_fraction_bps=5_000).configured


# ------------------------------------------------------------ the ceiling ---
def test_a_qualifying_shopper_reaches_the_whole_cap() -> None:
    assert effective_cap_bps(1_600, MAX_BPS) == 1_600


def test_a_new_shopper_gets_the_fraction() -> None:
    assert effective_cap_bps(1_600, 5_000) == 800


def test_the_cap_floors_rather_than_rounds_up() -> None:
    # 333 * 0.5 = 166.5. A ceiling that rounds up is a ceiling one basis point
    # above the number that was committed.
    assert effective_cap_bps(333, 5_000) == 166


@given(
    st.integers(min_value=0, max_value=MAX_BPS),
    st.integers(min_value=0, max_value=MAX_BPS),
)
def test_a_band_can_only_ever_lower_a_ceiling(cap: int, fraction: int) -> None:
    """The property everything downstream depends on.

    If a band could raise a cap, the committed per-product ceiling would stop
    being a ceiling and the merchant's printed promise would be breakable.
    """
    assert 0 <= effective_cap_bps(cap, fraction) <= cap


@given(st.integers(min_value=-5_000, max_value=20_000))
def test_a_nonsense_fraction_cannot_exceed_the_cap(fraction: int) -> None:
    assert 0 <= effective_cap_bps(1_000, fraction) <= 1_000


# ------------------------------------------------------------- phrasing -----
def test_the_agent_is_given_words_and_never_a_percentage() -> None:
    for txns in (0, 1, 4, 25):
        phrase = standing_phrase(CustomerStats(txns, txns * 10_000, identified=True))
        assert "%" not in phrase
        assert not any(ch.isdigit() for ch in phrase), phrase


def test_an_unidentified_shopper_reads_as_new() -> None:
    assert standing_phrase(CustomerStats(identified=False)) == "a new face"
