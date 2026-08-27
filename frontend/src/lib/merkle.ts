/**
 * RFC-6962 Merkle verification, in the browser.
 *
 * A line-for-line twin of `backend/app/core/merkle.py`. It exists so the
 * customer's own phone can check the merchant's proof without trusting our
 * server: the redemption page recomputes the root from the leaf and the
 * proof, and shows a badge only if it matches the root committed before the
 * campaign opened.
 *
 * Two implementations of one hashing scheme is a liability unless they are
 * pinned together, so both test suites assert against the shared fixture at
 * `backend/tests/fixtures/merkle_vectors.json`. If they drift, a test fails
 * rather than a demo.
 *
 * The security notes in the Python module apply verbatim: 0x00/0x01 domain
 * separation, power-of-two padding (never duplicate-last -- CVE-2012-2459),
 * and salted leaves.
 */

export const DOMAIN = "kirana.v1";

const LEAF_PREFIX = 0x00;
const NODE_PREFIX = 0x01;

export type Position = "left" | "right";

export interface ProofStep {
  hash: string;
  /** Where the *sibling* sits, so the walk can be replayed for the UI. */
  position: Position;
}

export interface MerkleTree {
  /** `levels[0]` is the padded leaf layer, `levels[levels.length - 1]` the root. */
  levels: string[][];
  leafCount: number;
  treeSize: number;
  depth: number;
  root: string;
}

const HEX = /^[0-9a-f]*$/;

function hexToBytes(hex: string): Uint8Array {
  if (hex.length % 2 !== 0 || !HEX.test(hex)) {
    throw new Error(`not lowercase hex: ${hex.slice(0, 16)}`);
  }
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(hex.substr(i * 2, 2), 16);
  }
  return out;
}

function bytesToHex(bytes: Uint8Array): string {
  let out = "";
  for (const b of bytes) out += b.toString(16).padStart(2, "0");
  return out;
}

async function sha256Hex(data: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data as BufferSource);
  return bytesToHex(new Uint8Array(digest));
}

function prefixed(prefix: number, ...chunks: Uint8Array[]): Uint8Array {
  const total = chunks.reduce((n, c) => n + c.length, 1);
  const out = new Uint8Array(total);
  out[0] = prefix;
  let at = 1;
  for (const c of chunks) {
    out.set(c, at);
    at += c.length;
  }
  return out;
}

export function hashLeaf(preimage: Uint8Array): Promise<string> {
  return sha256Hex(prefixed(LEAF_PREFIX, preimage));
}

export function hashNode(leftHex: string, rightHex: string): Promise<string> {
  return sha256Hex(prefixed(NODE_PREFIX, hexToBytes(leftHex), hexToBytes(rightHex)));
}

let emptyLeafCache: string | undefined;

/** The padding sentinel. No real slot preimage is empty, so none collides. */
export async function emptyLeaf(): Promise<string> {
  if (emptyLeafCache === undefined) {
    emptyLeafCache = await hashLeaf(new Uint8Array(0));
  }
  return emptyLeafCache;
}

export function nextPowerOfTwo(n: number): number {
  if (!Number.isInteger(n) || n < 0) throw new Error(`n must be a non-negative integer: ${n}`);
  if (n <= 1) return 1;
  return 1 << (32 - Math.clz32(n - 1));
}

/** log2 of a power of two -- the expected proof length for that tree size. */
function log2Exact(n: number): number {
  return 31 - Math.clz32(n);
}

export function leafPreimage(
  campaignId: string,
  leafIndex: number,
  slotToken: string,
  ceilingBps: number,
  saltHex: string,
): Uint8Array {
  if (!(ceilingBps >= 0 && ceilingBps <= 10000)) {
    throw new Error(`ceilingBps out of range: ${ceilingBps}`);
  }
  if (leafIndex < 0) throw new Error(`leafIndex must be non-negative: ${leafIndex}`);
  for (const [name, value] of [
    ["campaignId", campaignId],
    ["slotToken", slotToken],
    ["saltHex", saltHex],
  ] as const) {
    if (value.includes("|")) throw new Error(`${name} must not contain a pipe`);
  }
  // Pipe-delimited, not JSON: key order and unicode escaping differ between
  // Python and JavaScript, and these bytes have to match exactly.
  const joined = [DOMAIN, campaignId, String(leafIndex), slotToken, String(ceilingBps), saltHex].join("|");
  return new TextEncoder().encode(joined);
}

export function slotLeafHash(
  campaignId: string,
  leafIndex: number,
  slotToken: string,
  ceilingBps: number,
  saltHex: string,
): Promise<string> {
  return hashLeaf(leafPreimage(campaignId, leafIndex, slotToken, ceilingBps, saltHex));
}

export async function buildTree(leafHashes: string[]): Promise<MerkleTree> {
  if (leafHashes.length === 0) throw new Error("cannot build a tree over zero leaves");

  const leafCount = leafHashes.length;
  const treeSize = nextPowerOfTwo(leafCount);
  const pad = await emptyLeaf();
  const padded = [...leafHashes, ...Array<string>(treeSize - leafCount).fill(pad)];

  const levels: string[][] = [padded];
  while (levels[levels.length - 1].length > 1) {
    const current = levels[levels.length - 1];
    const next: string[] = [];
    for (let i = 0; i < current.length; i += 2) {
      next.push(await hashNode(current[i], current[i + 1]));
    }
    levels.push(next);
  }

  return {
    levels,
    leafCount,
    treeSize,
    depth: levels.length - 1,
    root: levels[levels.length - 1][0],
  };
}

export function proofFor(tree: MerkleTree, leafIndex: number): ProofStep[] {
  if (!(leafIndex >= 0 && leafIndex < tree.leafCount)) {
    throw new Error(`leafIndex ${leafIndex} outside [0, ${tree.leafCount})`);
  }
  const steps: ProofStep[] = [];
  let idx = leafIndex;
  for (let level = 0; level < tree.levels.length - 1; level++) {
    const sibling = idx ^ 1;
    steps.push({
      hash: tree.levels[level][sibling],
      position: sibling < idx ? "left" : "right",
    });
    idx = Math.floor(idx / 2);
  }
  return steps;
}

/**
 * Replay a proof and check it lands on `root`.
 *
 * The recorded positions must agree with the ones implied by `leafIndex`.
 * Trusting the recorded positions alone would let a valid proof be replayed
 * at a different index -- exactly the claim the verify page makes.
 */
export async function verifyProof(
  leafHash: string,
  leafIndex: number,
  proof: ProofStep[],
  root: string,
  treeSize?: number,
): Promise<boolean> {
  return (await verifyProofWalk(leafHash, leafIndex, proof, root, treeSize)).ok;
}

export interface WalkStep extends ProofStep {
  /** Running hash after folding this sibling in -- what the UI animates. */
  computed: string;
}

export interface WalkResult {
  ok: boolean;
  steps: WalkStep[];
  computedRoot: string;
  /** Set when the walk failed, so the UI can say *why*, not just "invalid". */
  failure?: "index" | "tree_size" | "proof_length" | "position" | "malformed" | "root_mismatch";
}

export async function verifyProofWalk(
  leafHash: string,
  leafIndex: number,
  proof: ProofStep[],
  root: string,
  treeSize?: number,
): Promise<WalkResult> {
  const fail = (failure: WalkResult["failure"], steps: WalkStep[] = []): WalkResult => ({
    ok: false,
    steps,
    computedRoot: leafHash,
    failure,
  });

  if (!Number.isInteger(leafIndex) || leafIndex < 0) return fail("index");
  if (treeSize !== undefined) {
    if (treeSize !== nextPowerOfTwo(treeSize) || leafIndex >= treeSize) return fail("tree_size");
    if (proof.length !== log2Exact(Math.max(1, treeSize))) return fail("proof_length");
  }
  if (!HEX.test(leafHash) || leafHash.length !== 64) return fail("malformed");

  const steps: WalkStep[] = [];
  let computed = leafHash;
  let idx = leafIndex;

  for (const step of proof) {
    const expected: Position = idx % 2 === 1 ? "left" : "right";
    if (step.position !== expected) return fail("position", steps);
    try {
      computed =
        step.position === "left"
          ? await hashNode(step.hash, computed)
          : await hashNode(computed, step.hash);
    } catch {
      return fail("malformed", steps);
    }
    steps.push({ ...step, computed });
    idx = Math.floor(idx / 2);
  }

  // A proof shorter than the tree is deep stops on an interior node, which
  // must not be mistaken for a root.
  if (idx !== 0) return fail("proof_length", steps);
  if (computed !== root) return { ok: false, steps, computedRoot: computed, failure: "root_mismatch" };
  return { ok: true, steps, computedRoot: computed };
}

export function policyHash(
  campaignId: string,
  maxDiscountBps: number,
  marginFloorBps: number,
  budgetPaise: number,
  maxTurns: number,
  slotCount: number,
): Promise<string> {
  // Key order here must match Python's `sort_keys=True`.
  const payload = JSON.stringify({
    budget_paise: budgetPaise,
    campaign_id: campaignId,
    domain: DOMAIN,
    margin_floor_bps: marginFloorBps,
    max_discount_bps: maxDiscountBps,
    max_turns: maxTurns,
    slot_count: slotCount,
  });
  return sha256Hex(new TextEncoder().encode(payload));
}
