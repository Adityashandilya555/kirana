# HTTP API

Grouped by who may call it. Errors are always
`{"detail": {"code": "...", "message": "..."}}` — `code` is for branching,
`message` is safe to show a customer.

## Customer

No merchant key. The **session id** is the credential: a uuid handed out only
in exchange for a slot token.

| | |
|---|---|
| `POST /api/v1/sessions` | Open or resume a session for a scanned slot token. 200, not 201 — a reload returns the same session and its transcript. Body: `slot_token`, optional `phone`, optional `transport`. |
| `GET /api/v1/sessions/{id}` | The public projection of the session. |
| `POST /api/v1/sessions/{id}/chat` | One negotiating turn. Body: `message`. Returns the reply, the offer card (or null), and the whole cart. |
| `GET /api/v1/sessions/{id}/cart` | The basket. Safe to poll. Cannot fail — degrades to an empty basket rather than 500ing. |
| `POST /api/v1/sessions/{id}/cart/remove` | Drop a line. Body: `sku`. |
| `POST /api/v1/sessions/{id}/checkout` | One Razorpay order for the whole basket. **No body** — the basket is server state and the client does not get to describe it. |
| `POST /api/v1/sessions/{id}/accept` | The older single-item form. Body: `sku`, `qty`, `discount_bps`. Still used by the machine-buyer flow. |
| `POST /api/v1/payments/confirm` | The checkout callback. Body: `order_id`, `payment_id`, `signature`. |
| `GET /api/v1/payments/{order_id}/status` | Polling fallback. **Requires `session_id`.** Settles at most once. |
| `POST /api/v1/payments/{order_id}/release` | Dismissed or failed checkout. **Requires `session_id`.** |
| `GET /api/v1/redemption/{token}` | The customer's own redemption screen. Read-only — it must never burn the code, which is why it is a different function from verify. |
| `GET /api/v1/redemption/{token}/qr.svg` | The QR the merchant scans. Rendered server-side; immutable, cached for a year. |

There is no route that writes a discount the client chose. Removing from the
basket is exposed; adding is not, because an add carries a rate and only the
gate may decide one.

## Merchant

All require `X-Merchant-Key`.

| | |
|---|---|
| `GET/POST /api/v1/campaigns` | List, create. |
| `GET /api/v1/campaigns/{id}` | One campaign with its counters. |
| `POST /api/v1/campaigns/{id}/commit` | Plan the ceilings, build the Merkle tree, write the slots. Irreversible. |
| `POST /api/v1/campaigns/{id}/tier` | The regular-customer rule. Draft-only: once stickers are printed the promise is fixed. |
| `GET /api/v1/campaigns/{id}/slots` | Every sticker with its status. |
| `GET /api/v1/campaigns/{id}/qr-sheet` | The printable sheet. |
| `GET /api/v1/campaigns/{id}/audit` | The decision feed — every outcome, including refusals. |
| `GET /api/v1/campaigns/{id}/sessions` | Conversations, with the scope each one actually saw. |
| `GET /api/v1/campaigns/{id}/postmortem` | What the campaign did, in words. |
| `POST /api/v1/campaigns/advise` | Suggested limits, from the model. Advisory only. |
| `POST /api/v1/simulate` | What these numbers will do, before commit makes them permanent. Uses the same pure functions the live gate uses, so the preview cannot drift from the enforcement it predicts. |
| `GET /api/v1/catalog` | The product list, costs included. |
| `GET /api/v1/catalog/template.csv` | A starter sheet. |
| `POST /api/v1/catalog/preview` | Parse and validate an upload. **Writes nothing.** |
| `POST /api/v1/catalog/import` | Parse, validate, then write. `replace=true` retires anything absent from the sheet — `active=false`, never deleted, because shelves and past sessions reference products by code. |
| `POST /api/v1/catalog/items` | Manual add/edit, for the shop with eleven products and no spreadsheet. |
| `GET/POST /api/v1/shelves`, `DELETE /api/v1/shelves/{id}` | Shelves, which scope a sticker to a subset of the catalogue. |
| `POST /api/v1/verify` | **Burns the code.** Green on first scan, red forever after. Returns the proof walk, the customer's standing, and the itemised bill. |

The import is two calls on purpose. An upload that silently half-applied would
leave a shop with a catalogue it did not choose, and the shopkeeper has no undo.

## Machine buyers

Public and credential-free, because that is what "transactable by an AI buyer"
means. None of it moves money or reserves anything.

| | |
|---|---|
| `GET /.well-known/agent-commerce.json` | What a buyer reads first: endpoints, the commitment root, the Ed25519 public key, and the quote policy. |
| `GET /api/v1/agent/catalog` | List prices. What a *sticker* can discount them to depends on the sticker, so that is `/quote`. |
| `POST /api/v1/agent/quote` | Price one line against one slot token, with an inclusion proof and a signature. Body: `slot_token`, `sku`, `qty`. A refusal is a structured 409, not a 500 — a refusal is a legitimate answer to a machine, same as to a person. |
| `POST /api/v1/agent/verify` | Re-run a buyer's three checks. A convenience for demos; it asserts nothing a buyer could not check themselves. |

A quote carries `"reserves_budget": false` and
`"revalidated_on_accept": true`, said plainly rather than left to be inferred
from a missing reservation and discovered at accept time.

## Webhooks and health

| | |
|---|---|
| `POST /api/v1/webhooks/razorpay` | Signed with the **webhook** secret. Deduped on Razorpay's event id, because it fires both `order.paid` and `payment.captured` and retries on any non-2xx. |
| `GET /health` | Liveness. |
| `GET /health/deep` | Proves it can actually *read* — an earlier version returned a literal from an unprivileged function, so a backend misconfigured with the anon key reported full green while every table read silently returned `[]` under RLS. |
| `GET /health/llm` | Provider reachability. |
