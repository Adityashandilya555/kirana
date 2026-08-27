"""Regenerate tests/fixtures/merkle_vectors.json.

The fixture is the contract between `app/core/merkle.py` and
`frontend/src/lib/merkle.ts`. Both test suites assert against it, so a
divergence between the two implementations fails a test instead of failing
on stage, in front of judges, on the one screen that matters.

Run after any intentional change to the hashing scheme:

    cd backend && uv run python scripts/gen_merkle_vectors.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import merkle  # noqa: E402

# Fixed inputs. Nothing here is random -- the point is reproducibility.
CAMPAIGN_ID = "3f2a1c8e-0b4d-4e6f-9a12-77c5d3e8b401"
CEILING_CYCLE = [500, 800, 1200, 1500, 2000]
SIZES = [1, 2, 3, 4, 5, 7, 8, 9, 16, 24, 31, 32]


def slot(i: int) -> dict[str, object]:
    return {
        "leaf_index": i,
        "slot_token": f"KIR{i:07d}",
        "ceiling_bps": CEILING_CYCLE[i % len(CEILING_CYCLE)],
        # Deterministic stand-in for secrets.token_hex(16).
        "salt_hex": f"{i:032x}",
    }


def main() -> None:
    cases = []
    for n in SIZES:
        slots = [slot(i) for i in range(n)]
        leaves = [
            merkle.slot_leaf_hash(
                CAMPAIGN_ID,
                s["leaf_index"],  # type: ignore[arg-type]
                s["slot_token"],  # type: ignore[arg-type]
                s["ceiling_bps"],  # type: ignore[arg-type]
                s["salt_hex"],  # type: ignore[arg-type]
            )
            for s in slots
        ]
        tree = merkle.build_tree(leaves)
        cases.append(
            {
                "slot_count": n,
                "slots": slots,
                "leaf_hashes": leaves,
                "tree_size": tree.tree_size,
                "depth": tree.depth,
                "merkle_root": tree.root,
                "proofs": tree.all_proofs(),
            }
        )

    payload = {
        "domain": merkle.DOMAIN,
        "campaign_id": CAMPAIGN_ID,
        "empty_leaf": merkle.EMPTY_LEAF,
        "policy_hash_case": {
            "campaign_id": CAMPAIGN_ID,
            "max_discount_bps": 2000,
            "margin_floor_bps": 800,
            "budget_paise": 500_000,
            "max_turns": 6,
            "slot_count": 24,
            "expected": merkle.policy_hash(
                CAMPAIGN_ID,
                max_discount_bps=2000,
                margin_floor_bps=800,
                budget_paise=500_000,
                max_turns=6,
                slot_count=24,
            ),
        },
        "cases": cases,
    }

    out = Path(__file__).resolve().parents[1] / "tests/fixtures/merkle_vectors.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(cases)} cases)")


if __name__ == "__main__":
    main()
