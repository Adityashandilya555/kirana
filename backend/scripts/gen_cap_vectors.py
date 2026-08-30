"""Generate the shared fixture for the per-product cap tree.

Run: uv run python scripts/gen_cap_vectors.py

Twin of gen_merkle_vectors.py, and it exists for the same reason. The cap
commitment is computed in Python at commit time and re-checked in TypeScript on
a customer's phone. If those two ever disagree by one byte, the second proof
walk fails on the phone -- and nothing in either test suite notices, because
each side is internally consistent. The fixture is the only thing that makes
the disagreement visible.

Deterministic on purpose: fixed salts, fixed campaign id, no randomness
anywhere. Regenerating must produce a byte-identical file, or the fixture is
recording the generator's mood rather than the algorithm.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import merkle  # noqa: E402

CAMPAIGN_ID = "3f2a1c8e-0b4d-4e6f-9a12-77c5d3e8b401"

#: Real shapes from the demo shop, plus the two that matter at the edges: a
#: product whose margin blocks any discount at all (cap 0), and one that reaches
#: the campaign maximum.
CAP_CASES: list[list[dict[str, object]]] = [
    # One row: the tree is a single leaf and the root IS the leaf.
    [{"sku": "TEA250", "cap_bps": 1600}],
    # Two rows: exactly a power of two, no padding.
    [{"sku": "ATTA5", "cap_bps": 900}, {"sku": "TEA250", "cap_bps": 1600}],
    # Three rows: padded to four. This is the CVE-2012-2459 shape -- an odd
    # count that a naive implementation would handle by duplicating the last
    # leaf, letting two different catalogues produce one root.
    [
        {"sku": "ATTA5", "cap_bps": 900},
        {"sku": "SUGAR1", "cap_bps": 0},
        {"sku": "TEA250", "cap_bps": 1600},
    ],
    # Six rows, the demo catalogue's size, padded to eight.
    [
        {"sku": "ATTA5", "cap_bps": 900},
        {"sku": "DAL1K", "cap_bps": 1200},
        {"sku": "OIL1L", "cap_bps": 450},
        {"sku": "RICE5", "cap_bps": 2000},
        {"sku": "SUGAR1", "cap_bps": 0},
        {"sku": "TEA250", "cap_bps": 1600},
    ],
]

TIER_CASES = [
    # The default: no rule configured. Must hash stably even when inert.
    {"tier_min_txn_count": 0, "tier_min_spend_paise": 0,
     "tier_window_days": None, "base_cap_fraction_bps": 10000},
    # A configured rule over a window.
    {"tier_min_txn_count": 3, "tier_min_spend_paise": 100000,
     "tier_window_days": 30, "base_cap_fraction_bps": 5000},
    # Lifetime. null and 0 are DIFFERENT promises and must hash differently;
    # the next case is the same rule with a zero window to prove it.
    {"tier_min_txn_count": 10, "tier_min_spend_paise": 0,
     "tier_window_days": None, "base_cap_fraction_bps": 2500},
]


def salt_for(index: int) -> str:
    """Fixed, distinct, 32 hex chars. Distinct so a swapped row is visible."""
    return f"{index:032x}"


def build() -> dict[str, object]:
    cases = []
    for rows in CAP_CASES:
        leaves = []
        for i, row in enumerate(rows):
            salt = salt_for(i)
            leaves.append(
                {
                    "row_index": i,
                    "sku": row["sku"],
                    "cap_bps": row["cap_bps"],
                    "salt_hex": salt,
                    "leaf_hash": merkle.cap_leaf_hash(
                        CAMPAIGN_ID, i, str(row["sku"]), int(row["cap_bps"]), salt
                    ),
                }
            )
        tree = merkle.build_tree([leaf["leaf_hash"] for leaf in leaves])
        cases.append(
            {
                "rows": leaves,
                "cap_root": tree.root,
                "cap_tree_size": tree.tree_size,
                "depth": tree.depth,
                "proofs": tree.all_proofs(),
            }
        )

    tiers = [
        {**case, "campaign_id": CAMPAIGN_ID,
         "expected": merkle.tier_hash(
             CAMPAIGN_ID,
             int(case["tier_min_txn_count"]),
             int(case["tier_min_spend_paise"]),
             case["tier_window_days"],  # type: ignore[arg-type]
             int(case["base_cap_fraction_bps"]),
         )}
        for case in TIER_CASES
    ]

    return {
        "cap_domain": merkle.CAP_DOMAIN,
        "campaign_id": CAMPAIGN_ID,
        "cases": cases,
        "tier_cases": tiers,
    }


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parents[1] / "tests/fixtures/cap_vectors.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
