/**
 * The cross-language parity check.
 *
 * These assert against the same `merkle_vectors.json` that
 * `backend/tests/test_merkle.py` uses. If the TypeScript and Python
 * implementations ever disagree about a single byte, one of the two suites
 * goes red -- which is the only way to find that out before the customer's
 * phone says "invalid" over a proof the server considers fine.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  DOMAIN,
  buildTree,
  emptyLeaf,
  hashLeaf,
  hashNode,
  leafPreimage,
  nextPowerOfTwo,
  policyHash,
  proofFor,
  slotLeafHash,
  verifyProof,
  verifyProofWalk,
  type ProofStep,
} from "./merkle";

interface Vectors {
  domain: string;
  campaign_id: string;
  empty_leaf: string;
  policy_hash_case: {
    campaign_id: string;
    max_discount_bps: number;
    margin_floor_bps: number;
    budget_paise: number;
    max_turns: number;
    slot_count: number;
    expected: string;
  };
  cases: {
    slot_count: number;
    slots: { leaf_index: number; slot_token: string; ceiling_bps: number; salt_hex: string }[];
    leaf_hashes: string[];
    tree_size: number;
    depth: number;
    merkle_root: string;
    proofs: ProofStep[][];
  }[];
}

const here = dirname(fileURLToPath(import.meta.url));
const vectors: Vectors = JSON.parse(
  readFileSync(resolve(here, "../../../backend/tests/fixtures/merkle_vectors.json"), "utf-8"),
);

const leavesFor = (n: number, campaignId = "camp-a") =>
  Promise.all(
    Array.from({ length: n }, (_, i) =>
      slotLeafHash(campaignId, i, `TOKEN${String(i).padStart(4, "0")}`, 500 + i * 100, i.toString(16).padStart(32, "0")),
    ),
  );

describe("parity with the Python implementation", () => {
  it("agrees on the domain and the padding sentinel", async () => {
    expect(vectors.domain).toBe(DOMAIN);
    expect(await emptyLeaf()).toBe(vectors.empty_leaf);
  });

  it("agrees on the policy hash", async () => {
    const c = vectors.policy_hash_case;
    expect(
      await policyHash(c.campaign_id, c.max_discount_bps, c.margin_floor_bps, c.budget_paise, c.max_turns, c.slot_count),
    ).toBe(c.expected);
  });

  for (const c of vectors.cases) {
    it(`agrees on leaves, root and proofs for ${c.slot_count} slots`, async () => {
      const leaves = await Promise.all(
        c.slots.map((s) => slotLeafHash(vectors.campaign_id, s.leaf_index, s.slot_token, s.ceiling_bps, s.salt_hex)),
      );
      expect(leaves).toEqual(c.leaf_hashes);

      const tree = await buildTree(leaves);
      expect(tree.root).toBe(c.merkle_root);
      expect(tree.treeSize).toBe(c.tree_size);
      expect(tree.depth).toBe(c.depth);
      expect(c.slots.map((_, i) => proofFor(tree, i))).toEqual(c.proofs);
    });

    it(`verifies every server-supplied proof for ${c.slot_count} slots`, async () => {
      // The path that actually runs on the customer's phone: the leaf, the
      // proof and the root all come from the server, and the browser checks
      // them without recomputing the tree.
      for (let i = 0; i < c.slot_count; i++) {
        expect(await verifyProof(c.leaf_hashes[i], i, c.proofs[i], c.merkle_root, c.tree_size)).toBe(true);
      }
    });
  }
});

describe("tree shape", () => {
  it.each([
    [0, 1], [1, 1], [2, 2], [3, 4], [4, 4], [5, 8], [8, 8], [9, 16], [24, 32], [32, 32],
  ])("nextPowerOfTwo(%i) === %i", (n, expected) => {
    expect(nextPowerOfTwo(n)).toBe(expected);
  });

  it("refuses an empty tree", async () => {
    await expect(buildTree([])).rejects.toThrow();
  });

  it("makes a single-leaf root equal to the leaf", async () => {
    const [leaf] = await leavesFor(1);
    const tree = await buildTree([leaf]);
    expect(tree.root).toBe(leaf);
    expect(await verifyProof(leaf, 0, [], tree.root, 1)).toBe(true);
  });

  it("refuses to open a padding leaf as if it were a slot", async () => {
    const tree = await buildTree(await leavesFor(3));
    expect(tree.treeSize).toBe(4);
    expect(() => proofFor(tree, 3)).toThrow();
  });
});

describe("attacks", () => {
  it("does not collide when the last leaf is duplicated (CVE-2012-2459)", async () => {
    const leaves = await leavesFor(3);
    const a = await buildTree(leaves);
    const b = await buildTree([...leaves, leaves[leaves.length - 1]]);
    expect(a.root).not.toBe(b.root);
  });

  it("separates leaf hashes from node hashes", async () => {
    const [a, b] = await leavesFor(2);
    const forged = await hashLeaf(
      Uint8Array.from([...a.match(/../g)!, ...b.match(/../g)!].map((h) => parseInt(h, 16))),
    );
    expect(forged).not.toBe(await hashNode(a, b));
  });

  it("rejects a raised ceiling against a committed proof", async () => {
    const leaves = await leavesFor(8, "camp-x");
    const tree = await buildTree(leaves);
    const proof = proofFor(tree, 3);

    const honest = await slotLeafHash("camp-x", 3, "TOKEN0003", 800, "3".padStart(32, "0"));
    expect(honest).toBe(leaves[3]);
    expect(await verifyProof(honest, 3, proof, tree.root)).toBe(true);

    const greedy = await slotLeafHash("camp-x", 3, "TOKEN0003", 9000, "3".padStart(32, "0"));
    const walk = await verifyProofWalk(greedy, 3, proof, tree.root);
    expect(walk.ok).toBe(false);
    expect(walk.failure).toBe("root_mismatch");
  });

  it("rejects a valid proof replayed at another index", async () => {
    const leaves = await leavesFor(8);
    const tree = await buildTree(leaves);
    const proof = proofFor(tree, 2);
    expect(await verifyProof(leaves[2], 2, proof, tree.root)).toBe(true);
    for (const other of [3, 6, 0]) {
      expect(await verifyProof(leaves[2], other, proof, tree.root)).toBe(false);
    }
  });

  it("rejects a flipped sibling position", async () => {
    const leaves = await leavesFor(8);
    const tree = await buildTree(leaves);
    const proof = proofFor(tree, 5);
    const flipped = [...proof];
    flipped[0] = { hash: proof[0].hash, position: proof[0].position === "left" ? "right" : "left" };
    const walk = await verifyProofWalk(leaves[5], 5, flipped, tree.root);
    expect(walk.ok).toBe(false);
    expect(walk.failure).toBe("position");
  });

  it("rejects a truncated proof, and the interior node it reaches", async () => {
    const leaves = await leavesFor(8);
    const tree = await buildTree(leaves);
    const short = proofFor(tree, 0).slice(0, 1);
    expect(await verifyProof(leaves[0], 0, short, tree.root)).toBe(false);
    expect(await verifyProof(leaves[0], 0, short, tree.levels[1][0], 8)).toBe(false);
  });

  it("rejects a proof of the wrong length for a known tree size", async () => {
    const leaves = await leavesFor(8);
    const tree = await buildTree(leaves);
    const padded: ProofStep[] = [...proofFor(tree, 0), { hash: await emptyLeaf(), position: "right" }];
    const walk = await verifyProofWalk(leaves[0], 0, padded, tree.root, 8);
    expect(walk.failure).toBe("proof_length");
  });

  it("rejects a non-power-of-two tree size", async () => {
    const leaves = await leavesFor(8);
    const tree = await buildTree(leaves);
    expect(await verifyProof(leaves[0], 0, proofFor(tree, 0), tree.root, 7)).toBe(false);
  });

  it("returns false rather than throwing on malformed hex", async () => {
    const leaves = await leavesFor(4);
    const tree = await buildTree(leaves);
    const bad = proofFor(tree, 1).map((s) => ({ ...s, hash: "zz".repeat(32) }));
    const walk = await verifyProofWalk(leaves[1], 1, bad, tree.root);
    expect(walk.ok).toBe(false);
    expect(walk.failure).toBe("malformed");
  });
});

describe("leaves", () => {
  it("hides the ceiling behind the salt", async () => {
    expect(await slotLeafHash("c", 0, "TOKEN", 1200, "aa".repeat(16))).not.toBe(
      await slotLeafHash("c", 0, "TOKEN", 1200, "bb".repeat(16)),
    );
  });

  it("binds every field", async () => {
    const base = ["c", 0, "TOKEN", 1200, "aa".repeat(16)] as const;
    const baseline = await slotLeafHash(...base);
    const mutated: (readonly [string, number, string, number, string])[] = [
      ["d", 0, "TOKEN", 1200, "aa".repeat(16)],
      ["c", 1, "TOKEN", 1200, "aa".repeat(16)],
      ["c", 0, "TOKEM", 1200, "aa".repeat(16)],
      ["c", 0, "TOKEN", 1201, "aa".repeat(16)],
      ["c", 0, "TOKEN", 1200, "ab".repeat(16)],
    ];
    for (const m of mutated) expect(await slotLeafHash(...m)).not.toBe(baseline);
  });

  it("keeps field boundaries unambiguous", () => {
    expect(leafPreimage("c", 1, "AB", 1200, "ff")).not.toEqual(leafPreimage("c", 1, "A", 1200, "ff"));
  });

  it.each([-1, 10001])("rejects an out-of-range ceiling (%i)", (bad) => {
    expect(() => leafPreimage("c", 0, "T", bad, "ff")).toThrow();
  });

  it("rejects pipe injection", () => {
    expect(() => leafPreimage("c", 0, "TOK|EN", 1200, "ff")).toThrow();
  });
});

describe("the walk the UI animates", () => {
  it("returns one step per level, ending at the root", async () => {
    const leaves = await leavesFor(24);
    const tree = await buildTree(leaves);
    const walk = await verifyProofWalk(leaves[7], 7, proofFor(tree, 7), tree.root, tree.treeSize);
    expect(walk.ok).toBe(true);
    expect(walk.steps).toHaveLength(5);
    expect(walk.steps[walk.steps.length - 1].computed).toBe(tree.root);
    expect(walk.computedRoot).toBe(tree.root);
  });
});
