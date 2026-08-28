"""The proof walk, and the token formats it has to accept.

verify_service is what a merchant's counter screen runs. Its failure modes
matter more than its happy path: a walk that silently accepts a bad proof
would make the whole commitment theatre, so each failure is asserted
individually rather than trusting one "invalid" result.
"""

from __future__ import annotations

import pytest

from app.core import merkle
from app.services import verify_service as vs

CAMPAIGN = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _tree(n: int = 8):
    """A real tree, so the proofs under test are the ones production makes."""
    leaves = [
        merkle.slot_leaf_hash(CAMPAIGN, i, f"SLOT{i:06d}", 1000 + i * 100, "cd" * 16)
        for i in range(n)
    ]
    return leaves, merkle.build_tree(leaves)


# ----------------------------------------------------------- the happy walk --
def test_a_real_proof_reproduces_the_root() -> None:
    leaves, tree = _tree()
    walk = vs.proof_walk(leaves[3], 3, tree.proof(3), tree.root, tree.tree_size)
    assert walk["ok"]
    assert walk["computed_root"] == tree.root


def test_the_walk_returns_every_intermediate_hash() -> None:
    """The UI animates these, so there must be one step per rung."""
    leaves, tree = _tree()
    walk = vs.proof_walk(leaves[0], 0, tree.proof(0), tree.root, tree.tree_size)
    assert len(walk["steps"]) == len(tree.proof(0))
    assert all("computed" in s for s in walk["steps"])


@pytest.mark.parametrize("index", range(8))
def test_every_leaf_in_the_tree_verifies(index: int) -> None:
    leaves, tree = _tree()
    assert vs.proof_walk(
        leaves[index], index, tree.proof(index), tree.root, tree.tree_size
    )["ok"]


# -------------------------------------------------------- the failure modes --
def test_a_wrong_root_is_caught_and_named() -> None:
    leaves, tree = _tree()
    walk = vs.proof_walk(leaves[2], 2, tree.proof(2), "0" * 64, tree.tree_size)
    assert not walk["ok"]
    assert walk["failure"] == "root_mismatch"


def test_a_proof_from_a_different_leaf_does_not_verify() -> None:
    """The substitution an attacker would actually try."""
    leaves, tree = _tree()
    walk = vs.proof_walk(leaves[2], 2, tree.proof(5), tree.root, tree.tree_size)
    assert not walk["ok"]


def test_a_truncated_proof_is_rejected() -> None:
    """A short proof stops on an interior node, which must never be mistaken
    for a root."""
    leaves, tree = _tree()
    walk = vs.proof_walk(leaves[1], 1, tree.proof(1)[:-1], tree.root, tree.tree_size)
    assert not walk["ok"]
    assert walk["failure"] == "proof_length"


def test_a_flipped_sibling_position_is_rejected() -> None:
    leaves, tree = _tree()
    proof = [dict(s) for s in tree.proof(4)]
    proof[0]["position"] = "left" if proof[0]["position"] == "right" else "right"
    walk = vs.proof_walk(leaves[4], 4, proof, tree.root, tree.tree_size)
    assert not walk["ok"]
    assert walk["failure"] == "position"


def test_a_malformed_hash_is_rejected() -> None:
    leaves, tree = _tree()
    proof = [dict(s) for s in tree.proof(0)]
    proof[0]["hash"] = "not-a-hash"
    walk = vs.proof_walk(leaves[0], 0, proof, tree.root, tree.tree_size)
    assert not walk["ok"]
    assert walk["failure"] == "malformed"


def test_an_index_outside_the_tree_is_rejected() -> None:
    leaves, tree = _tree()
    walk = vs.proof_walk(leaves[0], 99, tree.proof(0), tree.root, tree.tree_size)
    assert not walk["ok"]
    assert walk["failure"] == "tree_size"


def test_a_negative_index_is_rejected() -> None:
    leaves, tree = _tree()
    assert vs.proof_walk(leaves[0], -1, tree.proof(0), tree.root)["failure"] == "index"


def test_partial_steps_survive_a_failure() -> None:
    """A proof that breaks at rung three is far more useful on screen than a
    bare 'invalid' -- the merchant needs to tell a typo from a forgery."""
    leaves, tree = _tree()
    walk = vs.proof_walk(leaves[2], 2, tree.proof(2), "0" * 64, tree.tree_size)
    assert walk["steps"], "the walk should show how far it got"


# ------------------------------------------------------------ token formats --
def test_the_hex_format_settle_payment_actually_mints_is_accepted() -> None:
    """settle_payment writes encode(gen_random_bytes(12),'hex') -- 24 lowercase
    hex. This is what every live redemption token really looks like."""
    assert vs.canonical_redemption_token("e9c9d3d1974bf95260614dec") == (
        "e9c9d3d1974bf95260614dec"
    )


def test_a_scanned_payload_is_unwrapped() -> None:
    assert vs.canonical_redemption_token("KIRANA1:e9c9d3d1974bf95260614dec") == (
        "e9c9d3d1974bf95260614dec"
    )


def test_case_and_separators_a_human_types_are_folded() -> None:
    assert vs.canonical_redemption_token(" E9C9-D3D1 974B f952 6061 4DEC ") == (
        "e9c9d3d1974bf95260614dec"
    )


def test_another_systems_payload_is_not_guessed_at() -> None:
    assert vs.canonical_redemption_token("OTHERAPP:abc123") is None


@pytest.mark.parametrize("junk", ["", "   ", "hello", "12345"])
def test_junk_is_refused(junk: str) -> None:
    assert vs.canonical_redemption_token(junk) is None
