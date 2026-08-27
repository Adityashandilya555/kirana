"""Merkle tests.

These are written as attacks, not as coverage. Each one names the thing that
goes wrong in the real world if the property does not hold.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core import merkle

FIXTURE = Path(__file__).parent / "fixtures" / "merkle_vectors.json"


@pytest.fixture(scope="module")
def vectors() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def leaves_for(n: int, campaign_id: str = "camp-a") -> list[str]:
    return [
        merkle.slot_leaf_hash(campaign_id, i, f"TOKEN{i:04d}", 500 + i * 100, f"{i:032x}")
        for i in range(n)
    ]


# ------------------------------------------------------------ shape ----


@pytest.mark.parametrize(
    "n,expected",
    [(0, 1), (1, 1), (2, 2), (3, 4), (4, 4), (5, 8), (8, 8), (9, 16), (24, 32), (32, 32)],
)
def test_next_power_of_two(n: int, expected: int) -> None:
    assert merkle.next_power_of_two(n) == expected


def test_next_power_of_two_rejects_negative() -> None:
    with pytest.raises(ValueError):
        merkle.next_power_of_two(-1)


def test_empty_tree_is_an_error() -> None:
    # A campaign with zero slots is a bug upstream; fail loudly rather than
    # inventing a root for nothing.
    with pytest.raises(ValueError):
        merkle.build_tree([])


def test_single_leaf_root_is_the_leaf() -> None:
    leaf = leaves_for(1)[0]
    tree = merkle.build_tree([leaf])
    assert tree.root == leaf
    assert tree.tree_size == 1
    assert tree.proof(0) == []
    assert merkle.verify_proof(leaf, 0, [], tree.root, tree_size=1)


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 8, 9, 16, 24, 31, 32, 33])
def test_tree_size_is_a_power_of_two(n: int) -> None:
    tree = merkle.build_tree(leaves_for(n))
    assert tree.tree_size == merkle.next_power_of_two(n)
    assert tree.tree_size >= n
    assert 2**tree.depth == tree.tree_size


# ------------------------------------------------ proofs round trip ----


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 8, 9, 16, 24, 31, 32, 33])
def test_every_leaf_proof_verifies(n: int) -> None:
    leaves = leaves_for(n)
    tree = merkle.build_tree(leaves)
    for i, leaf in enumerate(leaves):
        proof = tree.proof(i)
        assert len(proof) == tree.depth, f"proof length wrong for leaf {i} of {n}"
        assert merkle.verify_proof(leaf, i, proof, tree.root, tree_size=tree.tree_size)


@settings(max_examples=60, deadline=None)
@given(st.integers(min_value=1, max_value=64))
def test_proofs_verify_for_any_slot_count(n: int) -> None:
    leaves = leaves_for(n)
    tree = merkle.build_tree(leaves)
    assert all(
        merkle.verify_proof(leaf, i, tree.proof(i), tree.root, tree_size=tree.tree_size)
        for i, leaf in enumerate(leaves)
    )


def test_proof_for_a_padding_leaf_is_refused() -> None:
    # Indices past slot_count are padding. They are not slots and must not be
    # openable as if they were.
    tree = merkle.build_tree(leaves_for(3))
    assert tree.tree_size == 4
    with pytest.raises(IndexError):
        tree.proof(3)


# --------------------------------------------------------- attacks ----


def test_cve_2012_2459_duplicate_last_leaf_does_not_collide() -> None:
    """The Bitcoin bug: padding by duplicating the last node lets two
    different leaf sets share a root, so the root stops identifying what was
    committed. Power-of-two padding must make these distinct."""
    leaves = leaves_for(3)
    duplicated = leaves + [leaves[-1]]
    assert merkle.build_tree(leaves).root != merkle.build_tree(duplicated).root


@pytest.mark.parametrize("n", [3, 5, 6, 7, 9, 24])
def test_appending_a_duplicate_changes_the_root(n: int) -> None:
    leaves = leaves_for(n)
    assert merkle.build_tree(leaves).root != merkle.build_tree(leaves + [leaves[-1]]).root


def test_domain_separation_blocks_leaf_node_confusion() -> None:
    """A 64-byte 'leaf' that is really two child hashes must not hash to the
    same value as the internal node over those children. Without the 0x00 /
    0x01 prefixes it would, and an attacker could re-interpret a subtree as
    a single slot."""
    a, b = leaves_for(2)
    forged_leaf = merkle.hash_leaf(bytes.fromhex(a) + bytes.fromhex(b))
    real_node = merkle.hash_node(a, b)
    assert forged_leaf != real_node


def test_tampering_with_the_ceiling_breaks_the_proof() -> None:
    """The demo's central claim. Raise the ceiling after commit and the
    stored proof stops landing on the committed root."""
    leaves = leaves_for(8, campaign_id="camp-x")
    tree = merkle.build_tree(leaves)
    proof = tree.proof(3)

    honest = merkle.slot_leaf_hash("camp-x", 3, "TOKEN0003", 800, f"{3:032x}")
    assert honest == leaves[3]
    assert merkle.verify_proof(honest, 3, proof, tree.root)

    greedy = merkle.slot_leaf_hash("camp-x", 3, "TOKEN0003", 9000, f"{3:032x}")
    assert not merkle.verify_proof(greedy, 3, proof, tree.root)


def test_a_valid_proof_cannot_be_replayed_at_another_index() -> None:
    leaves = leaves_for(8)
    tree = merkle.build_tree(leaves)
    proof = tree.proof(2)
    assert merkle.verify_proof(leaves[2], 2, proof, tree.root)
    for other in (3, 6, 0):
        assert not merkle.verify_proof(leaves[2], other, proof, tree.root)


def test_flipped_position_is_rejected() -> None:
    tree = merkle.build_tree(leaves_for(8))
    proof = tree.proof(5)
    flipped = list(proof)
    flipped[0] = {
        "hash": proof[0]["hash"],
        "position": "left" if proof[0]["position"] == "right" else "right",
    }
    assert not merkle.verify_proof(leaves_for(8)[5], 5, flipped, tree.root)


def test_truncated_proof_is_rejected() -> None:
    """A short proof stops on an interior node. If that node were accepted as
    a root, any subtree would look like a whole campaign."""
    leaves = leaves_for(8)
    tree = merkle.build_tree(leaves)
    interior = tree.levels[1][0]
    short = tree.proof(0)[:1]
    assert not merkle.verify_proof(leaves[0], 0, short, tree.root)
    # ...and it must not verify against the interior node it actually reaches.
    assert not merkle.verify_proof(leaves[0], 0, short, interior, tree_size=8)


def test_wrong_length_proof_rejected_when_tree_size_known() -> None:
    leaves = leaves_for(8)
    tree = merkle.build_tree(leaves)
    padded = tree.proof(0) + [{"hash": merkle.EMPTY_LEAF, "position": "right"}]
    assert not merkle.verify_proof(leaves[0], 0, padded, tree.root, tree_size=8)


def test_non_power_of_two_tree_size_rejected() -> None:
    leaves = leaves_for(8)
    tree = merkle.build_tree(leaves)
    assert not merkle.verify_proof(leaves[0], 0, tree.proof(0), tree.root, tree_size=7)


def test_negative_index_rejected() -> None:
    tree = merkle.build_tree(leaves_for(4))
    assert not merkle.verify_proof(merkle.EMPTY_LEAF, -1, [], tree.root)


def test_malformed_hex_in_proof_is_rejected_not_raised() -> None:
    leaves = leaves_for(4)
    tree = merkle.build_tree(leaves)
    bad = [{"hash": "nothex" * 10 + "zz", "position": p["position"]} for p in tree.proof(1)]
    assert merkle.verify_proof(leaves[1], 1, bad, tree.root) is False


# ---------------------------------------------------------- leaves ----


def test_salt_hides_the_ceiling() -> None:
    """Same slot, same ceiling, different salt -> different leaf. This is
    what stops a customer brute-forcing the merchant's committed bound out
    of a published root before they start negotiating."""
    a = merkle.slot_leaf_hash("c", 0, "TOKEN", 1200, "aa" * 16)
    b = merkle.slot_leaf_hash("c", 0, "TOKEN", 1200, "bb" * 16)
    assert a != b


def test_leaf_binds_every_field() -> None:
    base = dict(
        campaign_id="c", leaf_index=0, slot_token="TOKEN", ceiling_bps=1200,
        salt_hex="aa" * 16,
    )
    baseline = merkle.slot_leaf_hash(**base)
    for field, other in [
        ("campaign_id", "d"),
        ("leaf_index", 1),
        ("slot_token", "TOKEM"),
        ("ceiling_bps", 1201),
        ("salt_hex", "ab" * 16),
    ]:
        assert merkle.slot_leaf_hash(**{**base, field: other}) != baseline, field


def test_leaf_preimage_is_unambiguous() -> None:
    """Field boundaries must not be forgeable by shifting characters between
    adjacent fields."""
    a = merkle.leaf_preimage("c", 1, "AB", 1200, "ff")
    b = merkle.leaf_preimage("c", 1, "A", 1200, "ff")
    assert a != b


@pytest.mark.parametrize("bad", [-1, 10001])
def test_leaf_rejects_out_of_range_ceiling(bad: int) -> None:
    with pytest.raises(ValueError):
        merkle.slot_leaf_hash("c", 0, "T", bad, "ff")


def test_leaf_rejects_pipe_injection() -> None:
    with pytest.raises(ValueError):
        merkle.slot_leaf_hash("c", 0, "TOK|EN", 1200, "ff")


def test_empty_leaf_is_distinct_from_any_real_slot() -> None:
    assert merkle.EMPTY_LEAF not in leaves_for(32)


def test_build_is_deterministic() -> None:
    leaves = leaves_for(24)
    assert merkle.build_tree(leaves).root == merkle.build_tree(leaves).root


# ---------------------------------------------------- policy_hash ----


def test_policy_hash_is_sensitive_to_every_field() -> None:
    base = dict(
        campaign_id="c", max_discount_bps=2000, margin_floor_bps=800,
        budget_paise=500_000, max_turns=6, slot_count=24,
    )
    baseline = merkle.policy_hash(**base)
    for field in base:
        other = "d" if field == "campaign_id" else base[field] + 1
        assert merkle.policy_hash(**{**base, field: other}) != baseline, field


# ------------------------------------------- cross-language fixture ----


def test_fixture_matches_this_implementation(vectors: dict) -> None:
    """The parity anchor. `frontend/src/lib/merkle.test.ts` asserts against
    this same file, so if the two implementations drift, one of them fails."""
    assert vectors["domain"] == merkle.DOMAIN
    assert vectors["empty_leaf"] == merkle.EMPTY_LEAF

    pc = vectors["policy_hash_case"]
    assert pc["expected"] == merkle.policy_hash(
        pc["campaign_id"],
        max_discount_bps=pc["max_discount_bps"],
        margin_floor_bps=pc["margin_floor_bps"],
        budget_paise=pc["budget_paise"],
        max_turns=pc["max_turns"],
        slot_count=pc["slot_count"],
    )

    for case in vectors["cases"]:
        leaves = [
            merkle.slot_leaf_hash(
                vectors["campaign_id"], s["leaf_index"], s["slot_token"],
                s["ceiling_bps"], s["salt_hex"],
            )
            for s in case["slots"]
        ]
        assert leaves == case["leaf_hashes"], f"leaf drift at n={case['slot_count']}"
        tree = merkle.build_tree(leaves)
        assert tree.root == case["merkle_root"], f"root drift at n={case['slot_count']}"
        assert tree.tree_size == case["tree_size"]
        assert tree.depth == case["depth"]
        assert [list(p) for p in tree.all_proofs()] == [
            [dict(s) for s in p] for p in case["proofs"]
        ]


def test_fixture_covers_odd_and_padded_sizes(vectors: dict) -> None:
    sizes = {c["slot_count"] for c in vectors["cases"]}
    assert {1, 3, 5, 24}.issubset(sizes), "fixture must exercise padding"
