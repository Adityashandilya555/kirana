"""RFC-6962 Merkle commitments.

This module is the whole proof story. A merchant commits to a per-slot
discount ceiling *before* any customer scans anything; afterwards, anyone
holding a redemption code can be shown that the discount they were granted
was inside a bound that was fixed in advance.

Three design choices carry the security, and each one is load-bearing:

1.  DOMAIN SEPARATION.  Leaves are hashed with a 0x00 prefix and internal
    nodes with 0x01.  Without this, an attacker can present a 64-byte
    "leaf" that is really two concatenated child hashes and pass it off as
    an internal node (a second-preimage attack on the tree shape).

2.  POWER-OF-TWO PADDING.  The obvious way to handle an odd node count is
    to duplicate the last node.  That is exactly the Bitcoin bug
    CVE-2012-2459: two *different* leaf sets can then produce an identical
    root, so a root no longer uniquely identifies what was committed.  We
    instead pad the leaf layer out to the next power of two with a fixed
    sentinel, which makes the tree shape a pure function of `tree_size`.

3.  SALTED LEAVES.  A ceiling is a small integer out of ten thousand
    possible values.  An unsalted leaf hash could be brute-forced back to
    its ceiling in microseconds, which would let a customer read the
    merchant's committed bound off a public root before negotiating.  Each
    leaf carries 16 bytes of entropy, so the commitment is hiding until the
    merchant chooses to open it.

Everything here is pure: no I/O, no database, no clock, no randomness.
`frontend/src/lib/merkle.ts` is a line-for-line twin, and
`tests/fixtures/merkle_vectors.json` is the shared fixture that pins the two
implementations to the same bytes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, TypedDict

# Bumping this changes every hash in the system. It exists so that a root
# committed by an older build can never be confused with one from a newer.
DOMAIN = "kirana.v1"

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"

Position = Literal["left", "right"]


class ProofStep(TypedDict):
    """One rung of the walk from a leaf up to the root.

    `position` is where the *sibling* sits, so a reader of the audit trail
    can replay the walk without recomputing anything.
    """

    hash: str
    position: Position


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_leaf(preimage: bytes) -> str:
    return _sha256_hex(LEAF_PREFIX + preimage)


def hash_node(left_hex: str, right_hex: str) -> str:
    return _sha256_hex(NODE_PREFIX + bytes.fromhex(left_hex) + bytes.fromhex(right_hex))


# The padding sentinel. A real slot's preimage is never empty, so no genuine
# leaf can collide with this value.
EMPTY_LEAF: str = hash_leaf(b"")


def next_power_of_two(n: int) -> int:
    """Smallest power of two >= n. `next_power_of_two(0) == 1`."""
    if n < 0:
        raise ValueError("n must be non-negative")
    if n <= 1:
        return 1
    return 1 << (n - 1).bit_length()


def leaf_preimage(
    campaign_id: str,
    leaf_index: int,
    slot_token: str,
    ceiling_bps: int,
    salt_hex: str,
) -> bytes:
    """The canonical bytes a slot commits to.

    Pipe-delimited rather than JSON because JSON key ordering and unicode
    escaping differ between Python and JavaScript, and the two
    implementations have to agree byte for byte. None of the fields can
    contain a pipe: the campaign id is a UUID, the token is Crockford
    base32, the salt is hex, and the other two are integers.
    """
    if not (0 <= ceiling_bps <= 10_000):
        raise ValueError(f"ceiling_bps out of range: {ceiling_bps}")
    if leaf_index < 0:
        raise ValueError(f"leaf_index must be non-negative: {leaf_index}")
    for name, value in (
        ("campaign_id", campaign_id),
        ("slot_token", slot_token),
        ("salt_hex", salt_hex),
    ):
        if "|" in value:
            raise ValueError(f"{name} must not contain a pipe: {value!r}")
    parts = (
        DOMAIN,
        campaign_id,
        str(leaf_index),
        slot_token,
        str(ceiling_bps),
        salt_hex,
    )
    return "|".join(parts).encode("utf-8")


def slot_leaf_hash(
    campaign_id: str,
    leaf_index: int,
    slot_token: str,
    ceiling_bps: int,
    salt_hex: str,
) -> str:
    return hash_leaf(
        leaf_preimage(campaign_id, leaf_index, slot_token, ceiling_bps, salt_hex)
    )


@dataclass(frozen=True)
class MerkleTree:
    """A built tree. `levels[0]` is the padded leaf layer, `levels[-1]` the root."""

    levels: list[list[str]]
    leaf_count: int

    @property
    def root(self) -> str:
        return self.levels[-1][0]

    @property
    def tree_size(self) -> int:
        """The padded leaf count -- what gets stored as `campaigns.tree_size`."""
        return len(self.levels[0])

    @property
    def depth(self) -> int:
        return len(self.levels) - 1

    def proof(self, leaf_index: int) -> list[ProofStep]:
        if not (0 <= leaf_index < self.leaf_count):
            raise IndexError(
                f"leaf_index {leaf_index} outside [0, {self.leaf_count})"
            )
        steps: list[ProofStep] = []
        idx = leaf_index
        for level in self.levels[:-1]:
            sibling = idx ^ 1
            # A sibling at an even index sits to our left.
            position: Position = "left" if sibling < idx else "right"
            steps.append({"hash": level[sibling], "position": position})
            idx //= 2
        return steps

    def all_proofs(self) -> list[list[ProofStep]]:
        return [self.proof(i) for i in range(self.leaf_count)]


def build_tree(leaf_hashes: list[str]) -> MerkleTree:
    """Build a tree over `leaf_hashes`, padded to a power of two."""
    if not leaf_hashes:
        raise ValueError("cannot build a tree over zero leaves")

    leaf_count = len(leaf_hashes)
    padded = list(leaf_hashes) + [EMPTY_LEAF] * (
        next_power_of_two(leaf_count) - leaf_count
    )

    levels: list[list[str]] = [padded]
    while len(levels[-1]) > 1:
        current = levels[-1]
        levels.append(
            [hash_node(current[i], current[i + 1]) for i in range(0, len(current), 2)]
        )
    return MerkleTree(levels=levels, leaf_count=leaf_count)


def verify_proof(
    leaf_hash: str,
    leaf_index: int,
    proof: list[ProofStep],
    root: str,
    tree_size: int | None = None,
) -> bool:
    """Replay a proof and check it lands on `root`.

    The positions recorded in the proof must agree with the ones implied by
    `leaf_index`. Trusting the recorded positions alone would let a valid
    proof be replayed at a *different* index, which is precisely the claim
    the verify page is making ("this slot, this ceiling").
    """
    if leaf_index < 0:
        return False
    if tree_size is not None:
        if tree_size != next_power_of_two(tree_size) or leaf_index >= tree_size:
            return False
        if len(proof) != max(1, tree_size).bit_length() - 1:
            return False

    computed = leaf_hash
    idx = leaf_index
    for step in proof:
        expected: Position = "left" if idx % 2 == 1 else "right"
        if step["position"] != expected:
            return False
        try:
            if step["position"] == "left":
                computed = hash_node(step["hash"], computed)
            else:
                computed = hash_node(computed, step["hash"])
        except ValueError:  # not hex
            return False
        idx //= 2

    # A proof shorter than the tree is deep stops early and would otherwise
    # "verify" against an intermediate node.
    if idx != 0:
        return False
    return computed == root


def policy_hash(
    campaign_id: str,
    max_discount_bps: int,
    margin_floor_bps: int,
    budget_paise: int,
    max_turns: int,
    slot_count: int,
) -> str:
    """Commit to the campaign-wide rules, not just the per-slot ceilings.

    The root pins what each slot may offer; this pins the envelope those
    offers live in. Both are frozen at commit time and neither can move
    afterwards without the mismatch being visible.
    """
    payload = json.dumps(
        {
            "domain": DOMAIN,
            "campaign_id": campaign_id,
            "max_discount_bps": max_discount_bps,
            "margin_floor_bps": margin_floor_bps,
            "budget_paise": budget_paise,
            "max_turns": max_turns,
            "slot_count": slot_count,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_hex(payload.encode("utf-8"))
