"""The per-product cap tree, pinned to the fixture the TypeScript shares.

The failure this guards is quiet. Python computes the cap commitment at commit
time; TypeScript re-checks it on a shopper's phone. If the two disagree by one
byte the second proof walk fails on the phone and NEITHER test suite notices,
because each side is internally consistent with itself. Only a shared fixture
makes the disagreement visible, which is why these vectors exist and why
regenerating them must be deterministic.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.core import merkle

VECTORS = json.loads(
    (pathlib.Path(__file__).parent / "fixtures/cap_vectors.json").read_text()
)
CAMPAIGN_ID = VECTORS["campaign_id"]


def test_the_fixture_pins_the_domain() -> None:
    # Changing this string changes every cap hash ever committed.
    assert VECTORS["cap_domain"] == merkle.CAP_DOMAIN == "kirana.caps.v1"


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: f"{len(c['rows'])}rows")
def test_leaf_hashes_match_the_fixture(case: dict) -> None:
    for row in case["rows"]:
        assert merkle.cap_leaf_hash(
            CAMPAIGN_ID, row["row_index"], row["sku"],
            row["cap_bps"], row["salt_hex"],
        ) == row["leaf_hash"]


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: f"{len(c['rows'])}rows")
def test_roots_and_padding_match_the_fixture(case: dict) -> None:
    tree = merkle.build_tree([r["leaf_hash"] for r in case["rows"]])
    assert tree.root == case["cap_root"]
    # Padding to a power of two is what stops two different catalogues
    # producing one root -- CVE-2012-2459, same as the slot tree.
    assert tree.tree_size == case["cap_tree_size"]
    assert tree.depth == case["depth"]


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: f"{len(c['rows'])}rows")
def test_every_proof_verifies_against_the_committed_root(case: dict) -> None:
    tree = merkle.build_tree([r["leaf_hash"] for r in case["rows"]])
    for row in case["rows"]:
        proof = tree.proof(row["row_index"])
        assert proof == case["proofs"][row["row_index"]]
        assert merkle.verify_proof(
            row["leaf_hash"], row["row_index"], proof, case["cap_root"]
        )


@pytest.mark.parametrize("case", VECTORS["tier_cases"], ids=lambda c: str(c["tier_window_days"]))
def test_tier_hashes_match_the_fixture(case: dict) -> None:
    assert merkle.tier_hash(
        case["campaign_id"], case["tier_min_txn_count"],
        case["tier_min_spend_paise"], case["tier_window_days"],
        case["base_cap_fraction_bps"],
    ) == case["expected"]


# --------------------------------------------------- domain separation ------
def test_a_cap_leaf_cannot_be_replayed_as_a_slot_leaf() -> None:
    """Both leaves carry the 0x00 prefix. The domain string is what keeps them
    apart -- without it, identical positional arguments would collide."""
    slot = merkle.slot_leaf_hash(CAMPAIGN_ID, 0, "TEA250", 1600, "ab" * 16)
    cap = merkle.cap_leaf_hash(CAMPAIGN_ID, 0, "TEA250", 1600, "ab" * 16)
    assert slot != cap


def test_the_slot_tree_is_untouched_by_this_change() -> None:
    """The whole point of a second tree. If this fails, the live campaign's
    root is no longer reproducible and its printed sheet is waste paper."""
    slot_vectors = json.loads(
        (pathlib.Path(__file__).parent / "fixtures/merkle_vectors.json").read_text()
    )
    assert slot_vectors["domain"] == merkle.DOMAIN == "kirana.v1"
    case = slot_vectors["policy_hash_case"]
    assert merkle.policy_hash(
        case["campaign_id"], case["max_discount_bps"], case["margin_floor_bps"],
        case["budget_paise"], case["max_turns"], case["slot_count"],
    ) == case["expected"]


# ------------------------------------------------------------- the edges ----
def test_lifetime_and_a_zero_window_are_different_promises() -> None:
    """null means "ever"; 0 means a window nothing can fall inside. Coercing
    one to the other would make two different rules hash the same."""
    lifetime = merkle.tier_hash(CAMPAIGN_ID, 3, 100_000, None, 5_000)
    zero = merkle.tier_hash(CAMPAIGN_ID, 3, 100_000, 0, 5_000)
    assert lifetime != zero


def test_a_cap_of_zero_still_commits() -> None:
    """An undiscountable product is a promise too -- "never" is a ceiling, and
    omitting it would leave a gap the merchant could later fill."""
    assert merkle.cap_leaf_hash(CAMPAIGN_ID, 0, "SUGAR1", 0, "00" * 16)


@pytest.mark.parametrize("cap", [-1, 10_001])
def test_an_out_of_range_cap_is_refused(cap: int) -> None:
    with pytest.raises(ValueError, match="cap_bps"):
        merkle.cap_leaf_hash(CAMPAIGN_ID, 0, "TEA250", cap, "00" * 16)


def test_a_pipe_in_any_field_is_refused() -> None:
    # The delimiter is a pipe, so a pipe in a value would let two different
    # rows produce one preimage.
    with pytest.raises(ValueError, match="sku"):
        merkle.cap_leaf_hash(CAMPAIGN_ID, 0, "TEA|250", 1_600, "00" * 16)
