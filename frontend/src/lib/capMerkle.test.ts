/**
 * The cross-language parity check for the per-product cap tree.
 *
 * Twin of `backend/tests/test_cap_merkle.py`, asserting against the same
 * `cap_vectors.json`. This pairing is the whole point: Python computes the cap
 * commitment at commit time and this file's implementation re-checks it on a
 * shopper's phone. A one-byte disagreement fails the proof walk on the phone
 * and NEITHER suite notices alone, because each side is internally consistent
 * with itself.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  CAP_DOMAIN,
  DOMAIN,
  buildTree,
  capLeafHash,
  proofFor,
  tierHash,
  verifyProof,
} from "./merkle";

const here = dirname(fileURLToPath(import.meta.url));
const vectors = JSON.parse(
  readFileSync(
    resolve(here, "../../../backend/tests/fixtures/cap_vectors.json"),
    "utf-8",
  ),
) as {
  cap_domain: string;
  campaign_id: string;
  cases: {
    rows: { row_index: number; sku: string; cap_bps: number; salt_hex: string; leaf_hash: string }[];
    cap_root: string;
    cap_tree_size: number;
    depth: number;
    proofs: { hash: string; position: "left" | "right" }[][];
  }[];
  tier_cases: {
    campaign_id: string;
    tier_min_txn_count: number;
    tier_min_spend_paise: number;
    tier_window_days: number | null;
    base_cap_fraction_bps: number;
    expected: string;
  }[];
};

const CAMPAIGN_ID = vectors.campaign_id;

describe("cap tree parity", () => {
  it("agrees with Python on the domain string", () => {
    // Changing this changes every cap hash ever committed.
    expect(vectors.cap_domain).toBe(CAP_DOMAIN);
    expect(CAP_DOMAIN).toBe("kirana.caps.v1");
  });

  for (const [i, c] of vectors.cases.entries()) {
    describe(`case ${i} (${c.rows.length} rows)`, () => {
      it("computes every leaf hash the same way", async () => {
        for (const row of c.rows) {
          await expect(
            capLeafHash(CAMPAIGN_ID, row.row_index, row.sku, row.cap_bps, row.salt_hex),
          ).resolves.toBe(row.leaf_hash);
        }
      });

      it("builds the same root, with the same power-of-two padding", async () => {
        const tree = await buildTree(c.rows.map((r) => r.leaf_hash));
        expect(tree.levels[tree.levels.length - 1][0]).toBe(c.cap_root);
        // Padding is what stops two different catalogues sharing one root.
        expect(tree.levels[0].length).toBe(c.cap_tree_size);
        expect(tree.levels.length - 1).toBe(c.depth);
      });

      it("produces proofs that verify against the committed root", async () => {
        const tree = await buildTree(c.rows.map((r) => r.leaf_hash));
        for (const row of c.rows) {
          const proof = proofFor(tree, row.row_index);
          expect(proof).toEqual(c.proofs[row.row_index]);
          await expect(
            verifyProof(row.leaf_hash, row.row_index, proof, c.cap_root),
          ).resolves.toBe(true);
        }
      });
    });
  }
});

describe("tier hash parity", () => {
  for (const [i, t] of vectors.tier_cases.entries()) {
    it(`matches Python for case ${i} (window ${String(t.tier_window_days)})`, async () => {
      await expect(
        tierHash(
          t.campaign_id,
          t.tier_min_txn_count,
          t.tier_min_spend_paise,
          t.tier_window_days,
          t.base_cap_fraction_bps,
        ),
      ).resolves.toBe(t.expected);
    });
  }

  it("keeps lifetime and a zero window distinct", async () => {
    // null means "ever"; 0 means a window nothing can fall inside. Coercing
    // one to the other would make two different rules hash identically.
    const lifetime = await tierHash(CAMPAIGN_ID, 3, 100000, null, 5000);
    const zero = await tierHash(CAMPAIGN_ID, 3, 100000, 0, 5000);
    expect(lifetime).not.toBe(zero);
  });
});

describe("domain separation", () => {
  it("keeps the slot domain and the cap domain apart", () => {
    // Both leaf kinds carry the 0x00 prefix; only the domain string stops a
    // cap leaf being replayed as a slot leaf.
    expect(DOMAIN).not.toBe(CAP_DOMAIN);
  });
});
