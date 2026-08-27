"""Campaign creation and the commit ceremony.

Commit is the moment the merchant's promise becomes checkable. It generates
every slot, hashes each one into a leaf, builds the tree, precomputes all N
inclusion proofs, and writes the root. After it returns, the ceilings cannot
move without the root moving with them -- and the root is already printed on
the QR sheet and shown on the console.

Proofs are precomputed here rather than derived at redemption time. A proof
is cheap to build but the redemption path is the one that runs while a
customer is standing at a counter, so it does no tree work at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core import ids, merkle
from app.core.db import DbBackend

# Ceilings as a fraction of the campaign maximum, with how much of the sheet
# gets each one. Most codes are modest and a few are generous: a sheet where
# every code is identical makes the per-slot commitment pointless, and one
# where every code is generous burns the budget in the first ten scans.
DEFAULT_TIERS: tuple[tuple[float, float], ...] = (
    (0.25, 0.34),
    (0.60, 0.42),
    (0.85, 0.17),
    (1.00, 0.07),
)

CEILING_ROUNDING_BPS = 50


@dataclass(frozen=True)
class SlotSpec:
    leaf_index: int
    slot_token: str
    salt_hex: str
    ceiling_bps: int
    leaf_hash: str
    #: What this sticker is scoped to. Neither is part of the leaf hash: the
    #: commitment is about the ceiling, and folding scope into the preimage
    #: would change every hash in the system. See sql/009_shelves.sql.
    bound_sku: str | None = None
    shelf_id: str | None = None

    def to_row(self, proof: list[merkle.ProofStep]) -> dict[str, Any]:
        return {
            "leaf_index": self.leaf_index,
            "slot_token": self.slot_token,
            "salt_hex": self.salt_hex,
            "ceiling_bps": self.ceiling_bps,
            "leaf_hash": self.leaf_hash,
            "proof": proof,
            "bound_sku": self.bound_sku,
            "shelf_id": self.shelf_id,
        }


def plan_bindings(
    slot_count: int, binding: str, targets: list[str] | None
) -> list[tuple[str | None, str | None]]:
    """Assign each sticker its scope, as (bound_sku, shelf_id).

    Round-robin rather than contiguous blocks. A sheet is printed in reading
    order and then cut up, so contiguous assignment would put every tea sticker
    on one strip of paper -- fine until the strip is lost and the tea shelf has
    no codes at all. Interleaving spreads that risk.
    """
    if binding == "open" or not targets:
        return [(None, None)] * slot_count

    clean = [t for t in (x.strip() for x in targets) if t]
    if not clean:
        return [(None, None)] * slot_count

    out: list[tuple[str | None, str | None]] = []
    for i in range(slot_count):
        target = clean[i % len(clean)]
        if binding == "product":
            out.append((target.upper(), None))
        elif binding == "shelf":
            out.append((None, target))
        else:
            out.append((None, None))
    return out


def plan_ceilings(
    slot_count: int,
    max_discount_bps: int,
    tiers: tuple[tuple[float, float], ...] = DEFAULT_TIERS,
) -> list[int]:
    """Ceilings for each slot, deterministic and always exactly slot_count long."""
    if slot_count < 1:
        raise ValueError("slot_count must be at least 1")

    ceilings: list[int] = []
    for fraction, weight in tiers:
        value = int(max_discount_bps * fraction)
        value = (value // CEILING_ROUNDING_BPS) * CEILING_ROUNDING_BPS
        # A ceiling of zero is a code that can never do anything.
        value = max(CEILING_ROUNDING_BPS, min(value, max_discount_bps))
        ceilings.extend([value] * round(slot_count * weight))

    # Rounding can leave us a slot or two short or long.
    lowest = int(max_discount_bps * tiers[0][0]) or CEILING_ROUNDING_BPS
    lowest = max(CEILING_ROUNDING_BPS, (lowest // CEILING_ROUNDING_BPS) * CEILING_ROUNDING_BPS)
    while len(ceilings) < slot_count:
        ceilings.append(lowest)
    return sorted(ceilings[:slot_count])


def build_slot_specs(
    campaign_id: str,
    slot_count: int,
    max_discount_bps: int,
    binding: str = "open",
    targets: list[str] | None = None,
) -> list[SlotSpec]:
    specs: list[SlotSpec] = []
    seen: set[str] = set()
    scopes = plan_bindings(slot_count, binding, targets)
    for index, ceiling in enumerate(plan_ceilings(slot_count, max_discount_bps)):
        token = ids.new_slot_token()
        while token in seen:  # unique index would catch it; cheaper to not collide
            token = ids.new_slot_token()
        seen.add(token)
        salt = ids.new_salt_hex()
        bound_sku, shelf_id = scopes[index]
        specs.append(
            SlotSpec(
                leaf_index=index,
                slot_token=token,
                salt_hex=salt,
                ceiling_bps=ceiling,
                leaf_hash=merkle.slot_leaf_hash(
                    campaign_id, index, token, ceiling, salt
                ),
                bound_sku=bound_sku,
                shelf_id=shelf_id,
            )
        )
    return specs


async def create_campaign(
    db: DbBackend,
    *,
    merchant_id: str,
    name: str,
    budget_paise: int,
    max_discount_bps: int,
    margin_floor_bps: int,
    max_turns: int,
    slot_count: int,
    slot_binding: str = "open",
) -> dict[str, Any]:
    return await db.rpc(
        "create_campaign",
        {
            "p_merchant_id": merchant_id,
            "p_name": name,
            "p_budget_paise": budget_paise,
            "p_max_discount_bps": max_discount_bps,
            "p_margin_floor_bps": margin_floor_bps,
            "p_max_turns": max_turns,
            "p_slot_count": slot_count,
            "p_slot_binding": slot_binding,
        },
    )


async def commit_campaign(
    db: DbBackend, campaign_id: str,
    binding: str = "open", targets: list[str] | None = None,
) -> dict[str, Any]:
    """Irreversible. Generates slots, freezes the root, stores every proof."""
    campaign = await db.rpc("get_campaign", {"p_campaign_id": campaign_id})
    if campaign is None:
        raise ValueError("CAMPAIGN_NOT_FOUND")

    specs = build_slot_specs(
        campaign_id, campaign["slot_count"], campaign["max_discount_bps"],
        binding=binding, targets=targets,
    )
    tree = merkle.build_tree([s.leaf_hash for s in specs])
    proofs = tree.all_proofs()

    # Belt and braces: a proof that does not verify here must never reach a
    # printed sticker, because by then the root is public and unchangeable.
    for spec, proof in zip(specs, proofs, strict=True):
        if not merkle.verify_proof(spec.leaf_hash, spec.leaf_index, proof, tree.root):
            raise RuntimeError(
                f"self-check failed for leaf {spec.leaf_index}; refusing to commit"
            )

    result = await db.rpc(
        "commit_campaign",
        {
            "p_campaign_id": campaign_id,
            "p_merkle_root": tree.root,
            "p_policy_hash": merkle.policy_hash(
                campaign_id,
                campaign["max_discount_bps"],
                campaign["margin_floor_bps"],
                campaign["budget_paise"],
                campaign["max_turns"],
                campaign["slot_count"],
            ),
            "p_tree_size": tree.tree_size,
            "p_slots": [s.to_row(p) for s, p in zip(specs, proofs, strict=True)],
        },
    )
    return {"commit": result, "campaign": await db.rpc(
        "get_campaign", {"p_campaign_id": campaign_id}
    )}
