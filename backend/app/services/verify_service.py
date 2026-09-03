"""Redemption verification, and the proof walk that makes it checkable.

`verify_redemption` in plpgsql does the burn-once part: the first scan flips
`verified_at` and returns valid, every later scan returns already-verified.
That is the security-relevant half and it lives in the database, under a row
lock, because two merchants scanning the same screen at once is a race.

This module adds the half a person can read. It replays the inclusion proof
from the leaf up to the committed root, one rung at a time, and states the
sentence the whole pitch reduces to:

    granted 1200 <= committed ceiling 1200, and this leaf is in root 63c660eb

The walk mirrors `frontend/src/lib/merkle.ts:verifyProofWalk` step for step so
the merchant's page and the customer's phone compute the same thing from the
same fields -- the customer's browser doing it independently is the beat that
makes the proof feel real rather than asserted.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from app.core import ids, merkle
from app.core.db import DbBackend

HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Two token formats are in play, and this is a real inconsistency in the
# codebase rather than a defensive nicety:
#
#   settle_payment() mints encode(gen_random_bytes(12),'hex') -- 24 lowercase
#   hex characters. That is what every live redemption token actually is.
#
#   ids.py declares REDEMPTION_TOKEN_LENGTH = 12 and parse_redemption_payload
#   folds through normalize_token(), which upper-cases into the Crockford
#   alphabet. Running a hex token through it yields DB6E... which matches
#   nothing, because the column holds db6e...
#
# Phase 3 is the first code to look a redemption token up, so this is where it
# surfaces. Accepting both shapes keeps verification working against tokens
# already minted while the format is unified; see the note in the PR.
_SEPARATORS = str.maketrans({"-": None, " ": None, "_": None, ".": None})
_HEX_TOKEN = re.compile(r"^[0-9a-f]{24}$")
_CROCKFORD_TOKEN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{12}$")


def canonical_redemption_token(raw: str) -> str | None:
    """Fold a scan or a hand-typed code to exactly what the column holds."""
    candidate = (raw or "").strip()
    if candidate.upper().startswith(ids.REDEMPTION_PREFIX):
        candidate = candidate[len(ids.REDEMPTION_PREFIX):]
    elif ":" in candidate:
        return None  # some other system's payload; do not guess
    candidate = candidate.translate(_SEPARATORS)

    if _HEX_TOKEN.match(candidate.lower()):
        return candidate.lower()
    # Crockford is the hand-typeable shape: fold O->0 and I->1 the way a human
    # reading off a screen actually mis-types them.
    folded = ids.normalize_token(candidate)
    if _CROCKFORD_TOKEN.match(folded):
        return folded
    return None

Failure = Literal[
    "index", "tree_size", "proof_length", "position", "malformed", "root_mismatch"
]


def proof_walk(
    leaf_hash: str,
    leaf_index: int,
    proof: list[dict[str, Any]],
    root: str,
    tree_size: int | None = None,
) -> dict[str, Any]:
    """Replay the walk, returning every intermediate hash.

    Deliberately returns the partial `steps` even on failure: a proof that
    breaks at rung three is far more informative on screen than a bare
    "invalid", and the merchant needs to be able to tell a typo from a forgery.
    """

    def fail(reason: Failure, steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "steps": steps or [],
            "computed_root": leaf_hash,
            "failure": reason,
        }

    if not isinstance(leaf_index, int) or leaf_index < 0:
        return fail("index")
    if tree_size is not None:
        if tree_size != merkle.next_power_of_two(tree_size) or leaf_index >= tree_size:
            return fail("tree_size")
        if len(proof) != max(1, tree_size).bit_length() - 1:
            return fail("proof_length")
    if not HEX64.match(leaf_hash or ""):
        return fail("malformed")

    steps: list[dict[str, Any]] = []
    computed = leaf_hash
    idx = leaf_index

    for step in proof:
        position = step.get("position")
        sibling = step.get("hash", "")
        expected = "left" if idx % 2 == 1 else "right"
        if position != expected:
            return fail("position", steps)
        if not HEX64.match(sibling or ""):
            return fail("malformed", steps)
        computed = (
            merkle.hash_node(sibling, computed)
            if position == "left"
            else merkle.hash_node(computed, sibling)
        )
        steps.append({**step, "computed": computed})
        idx //= 2

    # A proof shorter than the tree is deep stops on an interior node, which
    # must not be mistaken for a root.
    if idx != 0:
        return fail("proof_length", steps)
    if computed != root:
        return {
            "ok": False, "steps": steps,
            "computed_root": computed, "failure": "root_mismatch",
        }
    return {"ok": True, "steps": steps, "computed_root": computed}


def _pct(bps: int | None) -> str:
    return "—" if bps is None else f"{bps / 100:g}%"


async def verify(db: DbBackend, raw_token: str) -> dict[str, Any]:
    """Burn-once check plus the readable proof.

    `raw_token` may be a full `KIRANA1:<token>` scan payload or a hand-typed
    code; both are folded to canonical form first, because O/0 and I/1 are
    exactly the substitutions a human makes reading a printed code.
    """
    token = canonical_redemption_token(raw_token)
    if not token:
        # "nothing scanned" and "scanned something that is not a code of ours"
        # look identical to the code but completely different at a counter:
        # one means try again, the other means this customer has the wrong QR.
        empty = not (raw_token or "").strip()
        return {
            "valid": False,
            "code": "V05_EMPTY_TOKEN" if empty else "V06_MALFORMED_TOKEN",
            "headline": "Nothing scanned" if empty else "Not a valid code",
            "detail": (
                "No code was read. Try again, or type it in."
                if empty
                else "That is not one of our redemption codes. Check you scanned "
                     "the customer's redemption screen, not the shelf sticker."
            ),
            "proof": None,
        }

    result = await db.rpc("verify_redemption", {"p_redemption_token": token})
    if not result:
        return {
            "valid": False,
            "code": "V04_UNKNOWN_TOKEN",
            "headline": "Not one of ours",
            "detail": "That code was not issued by this shop.",
            "proof": None,
        }

    code = result.get("code")
    if code == "V04_UNKNOWN_TOKEN":
        return {
            "valid": False, "code": code,
            "headline": "Not one of ours",
            "detail": "That code was not issued by this shop.",
            "proof": None, "raw": result,
        }

    walk = proof_walk(
        result.get("leaf_hash") or "",
        int(result.get("leaf_index") or 0),
        result.get("proof") or [],
        result.get("merkle_root") or "",
        result.get("tree_size"),
    )

    granted = result.get("granted_bps")
    ceiling = result.get("ceiling_bps")
    within = granted is not None and ceiling is not None and granted <= ceiling

    # The commitment holding is independent of whether this scan is the first.
    # An already-used code still proves the merchant kept their promise; it
    # just cannot be redeemed twice.
    first_use = bool(result.get("valid"))
    proven = bool(walk["ok"]) and within

    if first_use and proven:
        headline, detail = "Valid", "First use. Apply the discount."
    elif not proven:
        headline = "Proof failed"
        detail = (
            f"The inclusion proof did not reproduce the committed root "
            f"({walk.get('failure')})."
            if not walk["ok"]
            else f"Granted {_pct(granted)} exceeds the committed ceiling {_pct(ceiling)}."
        )
    else:
        headline = "Already used"
        detail = f"This code was verified at {result.get('first_verified_at')}."

    return {
        "valid": first_use and proven,
        "code": code,
        "headline": headline,
        "detail": detail,
        "first_verified_at": result.get("first_verified_at"),
        "paid_at": result.get("paid_at"),
        "store": result.get("store"),
        "campaign_name": result.get("campaign_name"),
        "sku": result.get("sku"),
        "qty": result.get("qty"),
        # Who is standing at the counter, and what is in the bag. Neither was
        # here before: a shopkeeper scanning a code learned the discount was
        # inside its committed ceiling and nothing about the person or the
        # basket. The proof is the argument; this is the shop.
        #
        # `customer` carries the last four digits of the phone and never the
        # number, the same rule the session-opened audit row follows -- enough
        # to recognise someone across a counter, not enough to be a contact
        # list. `band` is read off the session snapshot: it is the band that
        # actually priced this basket, not one recomputed now.
        "customer": result.get("customer") or {"identified": False},
        "bill": result.get("bill") or {"items": [], "count": 0},
        "slot_token": result.get("slot_token"),
        "leaf_index": result.get("leaf_index"),
        "granted_bps": granted,
        "ceiling_bps": ceiling,
        "discount_paise": result.get("discount_paise"),
        "final_amount_paise": result.get("final_amount_paise"),
        "within_ceiling": within,
        # The one line the demo says out loud.
        "argument": (
            f"granted {_pct(granted)} <= committed ceiling {_pct(ceiling)}"
            if within
            else f"granted {_pct(granted)} EXCEEDS committed ceiling {_pct(ceiling)}"
        ),
        "commitment": {
            "merkle_root": result.get("merkle_root"),
            "policy_hash": result.get("policy_hash"),
            "tree_size": result.get("tree_size"),
            "committed_at": result.get("committed_at"),
            "leaf_hash": result.get("leaf_hash"),
            "salt_hex": result.get("salt_hex"),
            "proof": result.get("proof") or [],
        },
        "proof": walk,
    }
