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
