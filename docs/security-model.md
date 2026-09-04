# Security model

The claim is narrow and worth stating precisely:

> A shopkeeper can hand price negotiation to a language model without being able
> to lose more than they agreed to in advance — and both they and the customer
> can prove afterwards that the promise held.

Everything below is how that is enforced, and where.

## The commitment

Before a single customer scans anything, the merchant fixes four numbers:

| | |
|---|---|
| `budget_paise` | the most the campaign may ever give away in total |
| `max_discount_bps` | the most any one code may reach |
| `margin_floor_bps` | the margin, on the sale price, below which no sale may go |
| per-slot `ceiling_bps` | what *this* printed sticker may reach |

`commit_campaign` hashes every slot's leaf into one Merkle root and stores it.
The leaf preimage is `(campaign_id, leaf_index, slot_token, ceiling_bps,
salt_hex)`; the tree is RFC 6962 — SHA-256 with domain separation, `0x00` on
leaves and `0x01` on interior nodes, the same construction Certificate
Transparency uses. Committing flips the campaign out of `draft`, and
`ck_commit_integrity` makes a non-draft campaign without a root a database
error.

To raise a ceiling afterwards and still present the same root, you would need a
second preimage for SHA-256.

The salt is per slot, so publishing the root before redemption does not leak
what any individual ceiling is; opening one leaf at redemption reveals only
that leaf.

## Four guarantees

**1 · One discount per shopper, per campaign.**
`uq_payment_captured_per_customer` — a unique index on
`(campaign_id, customer_id) where status='captured'`. Scanning five stickers
gets you one discount. For a one-shot sticker,
`uq_payment_captured_once_slot` additionally allows one captured payment per
slot, ever.

**2 · Storing an over-ceiling grant is a database error.**
`slots.ck_granted_le_ceiling` — `granted_bps is null or granted_bps <=
ceiling_bps`. Not a check in application code that a future refactor can route
around; a constraint the write itself fails.

**3 · The budget envelope is enforced by Postgres.**
`campaigns.ck_budget_envelope` — `spent_paise + reserved_paise <=
budget_paise`. Reservations and settlements both move through it.

**4 · Every outcome is an append-only row.**
`decisions` records approvals, clamps, refusals, blocked injections, tool
calls, quotes, settlements and verifications. Refusals are the product, not an
error path.

## What the model is never given

Three values are read from `OfferContext` on the server and appear in no tool
result and no prompt:

- `cost_paise` — the shop's margins
- the campaign's remaining budget
- the slot's `ceiling_bps`, and the per-product `cap_bps`

An injection cannot leak or raise a bound that was never in the context window.
`CatalogItem.public()` is the allowlist that enforces it, and neither field
appears there. `tests/test_tools.py` asserts that no tool result contains a cost
and that the rendered prompt contains no cost *value*.

The one number the model does see is `max_allowed_bps`, returned by
`propose_offer` when the gate clamps — because the refuse-and-explain loop is
what makes it re-propose inside the bound instead of arguing. That number stays
server-side of the customer: `chat_service._offer_payload` is a closed
allowlist that deliberately omits it.

> **The leak this closed.** For a typical sticker `max_allowed_bps` *is* the
> committed ceiling. While it was in the customer payload, a shopper could ask
> for 2%, be approved as proposed — no clamp, nothing on screen suggesting
> anything sensitive had happened — and read their ceiling straight out of the
> response body. From then on the haggling is theatre: they ask for the number
> they were shown. `test_the_payload_is_a_closed_allowlist` fails if a field is
> ever added without that decision being made deliberately.

## Prompt injection

`app/core/sanitize.py` screens the message **before any provider is
constructed**. A blocked message therefore provably costs zero tokens, and the
audit row it writes has `llm_provider` NULL — machine-checkable evidence the
model was never invoked, rather than evidence it resisted.

That is the first layer. The second is that there is nothing useful to leak
(above). The third is that even a fully compromised model can only *ask* — the
gate decides, and Postgres refuses to store the answer if the gate were somehow
wrong.

`tests/test_injection_suite.py` holds the corpus.

## Trust boundaries

| surface | credential | notes |
|---|---|---|
| customer chat, cart, checkout | `session_id` | a uuid, handed out only in exchange for a slot token |
| the slot token itself | possession | printed on one physical sticker; scanning it is the proof |
| merchant console, verify | `X-Merchant-Key` | see the tradeoff below |
| agent discovery, catalog, quote | none | public by design; moves no money, reserves nothing |
| Razorpay webhook | HMAC, webhook secret | a *different* secret from the key secret |
| checkout callback | HMAC, API key secret | with an upstream fallback, below |

`payments/{order_id}/status` and `/release` both require the `session_id` and
not merely the order id. An order id appears in the checkout sheet and in
Razorpay's dashboard; it is not a credential. `release` is the sharper of the
two — it nulls `sessions.rzp_order_id`, and `settle_payment` finds its session
*by* that column, so releasing someone else's in-flight order would mean their
payment captures at Razorpay and settles nowhere.

## Signature verification, and the fallback

The checkout callback is signed with the **API key secret**; the webhook body
is signed with the **webhook secret**. They are different values, and mixing
them up produces a failure that reads exactly like tampering.

The Razorpay SDK's verify calls *raise*; they never return `False`. `if not
verify(...)` is therefore always falsy-negative and silently accepts every
forged signature. Both are wrapped in `app/core/rzp.py` to return a bool.

When the HMAC does not verify, `/payments/confirm` asks Razorpay directly
whether that payment id was captured against that order id before refusing.
This is a *stricter* check, not a softer one — asking the payment provider
beats recomputing a hash over ids the client just supplied. It exists because
the failure it replaces is total and invisible: money gone, callback rejected,
and if the webhook is also misconfigured, nothing ever settles.

## Machine buyers

`/.well-known/agent-commerce.json` is public and credential-free, because that
is what "transactable by an AI buyer" means. What makes it safe is that none of
it moves money:

- **Quotes reserve nothing.** Reserving on quote would let an unauthenticated
  caller drain a campaign by asking for prices it never intends to pay — a
  denial of service that costs the attacker nothing. Quotes are advisory,
  live 120 seconds, and are re-gated at accept.
- **Ed25519, not HMAC.** An HMAC needs the shared secret to verify, so only we
  could check it, which defeats the point. A public key in the discovery
  document lets any buyer verify cold.
- **Canonical serialisation.** The signature covers sorted-key JSON with no
  floats. A signature over a dict whose key order varies fails intermittently
  for reasons nobody can reproduce, and a money path is the last place to
  accept that.
- `cost_paise` appears nowhere. The agent catalogue is projected in SQL by
  `get_agent_catalog`, which cannot leak it because it does not select it.

A buyer's three checks — recompute the leaf, walk the proof to the root, confirm
the grant is inside the opened ceiling — are what `agent_commerce.self_check`
runs, and `/api/v1/agent/verify` exposes it as a convenience. It asserts
nothing a buyer could not check themselves.

## Database privileges

Every function is `SECURITY DEFINER` and therefore bypasses RLS, so an EXECUTE
grant to `anon` would be a direct path to `nuke_demo()` over PostgREST. Every
table has RLS enabled with zero policies — deny-all to `anon` and
`authenticated` — and every function is revoked from both.

**Postgres re-grants EXECUTE to PUBLIC on every `create or replace`.** Every
migration therefore ends with a privileges block, and
`tests/test_privileges.py` is the regression guard, because this can silently
regress any time someone edits a function body.

## Known tradeoffs

Named here rather than discovered later.

- **`VITE_MERCHANT_API_KEY` ships in the public JS bundle.** It gates campaign
  create and commit. Unavoidable while the merchant console is a page in the
  same single-page app; a proper fix (server-side merchant session, or a token
  exchange) costs about a day and buys nothing for a test-mode demo. Rotate it
  if this outlives the demo.
- **One hardcoded merchant, one shared key.** Multi-tenant auth is explicitly
  out of scope.
- **Razorpay runs in test mode.** Test-mode QR codes are not scannable by real
  UPI apps, which is exactly why the printed sticker encodes a plain HTTPS URL
  and payment happens in Standard Checkout inside the browser.
- **`ALLOW_STUB_PAYMENTS` bypasses signature verification entirely.** It
  requires an explicit opt-in and refuses in production regardless. The
  previous form keyed off variables that both defaulted to the unsafe value, so
  any deploy that merely forgot the Razorpay keys accepted an arbitrary
  `{order_id, payment_id, signature}` at the unauthenticated `/payments/confirm`
  and minted a real burn-once token for a payment that never happened. A guard
  whose default is "off" is not a guard.
