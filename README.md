# Kirana Agent

An offline merchant's QR becomes an agentic commerce surface. An AI negotiates
discounts inside cryptographically pre-committed bounds, and every decision —
including every refusal — is provable after the fact.

Razorpay hackathon, Track 01 (AI Growth & Agentic Commerce).

## The one-paragraph argument

The merchant commits to a per-code discount ceiling before a single customer
scans anything, and that commitment is a Merkle root — 32 bytes that cannot be
changed afterwards without changing the root. The language model never sees the
ceiling; it is not in the context window, so no prompt injection can leak or
raise it. The model can only ever *ask*, by calling a `propose_offer` tool. A
deterministic function with no model in it decides what is actually granted, and
Postgres carries a `CHECK` constraint that makes storing an over-ceiling grant a
database error rather than a bug. Every outcome is an append-only row. When the
customer redeems, their own phone recomputes the Merkle path in the browser and
confirms the discount they got was inside the number the merchant promised
before the conversation started.

## Documentation

Full documentation is in [`docs/`](docs/README.md):

- [architecture.md](docs/architecture.md) — the parts, and how a request moves
- [security-model.md](docs/security-model.md) — the guarantees and where each is enforced
- [api.md](docs/api.md) — every endpoint, grouped by who may call it
- [database.md](docs/database.md) — tables, functions, migrations
- [operations.md](docs/operations.md) — deploying, and the failures that have actually happened

## Layout

    sql/        schema, plpgsql functions, demo seed
    backend/    FastAPI + LangChain agent (Railway)
    frontend/   React + Vite + Tailwind v4 (Vercel)
    docs/       the above

## Quickstart

    cp backend/.env.example backend/.env      # fill in the blanks
    cp frontend/.env.example frontend/.env.local
    make db-reset                             # local Postgres via Docker, :5433
    make dev-api                              # :8000
    make dev-web                              # :5173

`make help` lists everything. `make test` is the no-database suite; `make
test-all` adds the integration tests, which need `make db-reset` first and
point themselves at the container it starts.

## Non-obvious decisions

- **Money is paise (`bigint`), rates are basis points (`int`). No floats.**
- **A conversation fills a basket, not a single offer.** Every approved
  `propose_offer` becomes a line in `cart_items` at the rate the gate granted
  *that product*, and there is one Pay button, on the basket. At checkout the
  gate re-runs per line in Python and then again per line in SQL inside
  `reserve_cart`, which is the transaction that moves the budget. The basket
  aggregates into the session's existing `offer_bps` / `offer_amount_paise`
  columns rather than replacing them, so `settle_payment`, the webhook and the
  poller are untouched — and because the aggregate rate is a weighted mean of
  per-line rates already inside the ceiling, `slots.ck_granted_le_ceiling`
  still holds unweakened. See the header of `sql/025_cart.sql`.
- **The assistant never asks for payment.** It quotes an item and asks what
  else is needed; the shopper decides when they are done. An assistant with a
  Pay button attached to its last sentence is one that has decided on the
  shopper's behalf that they have finished shopping.
- **All Supabase access is server-side.** `*.supabase.co` was DNS-blocked by
  Indian ISPs for ~8 days in Feb–Mar 2026; the phone is the device whose network
  we control least.
- **`bind_tools`, never `with_structured_output`.** Ollama Cloud silently
  ignores JSON-Schema structured outputs (ollama/ollama#12362), so schema
  forcing returns plausible prose instead of raising.
- **The printed QR encodes a plain HTTPS URL**, not a payment QR. Razorpay QR
  codes only work in live mode.

## Known demo-only tradeoffs

Named here rather than discovered later.

- **`VITE_MERCHANT_API_KEY` ships in the public JS bundle.** It gates campaign
  create and commit. This is unavoidable while the merchant console is a page in
  the same single-page app, and a proper fix (server-side merchant session, or a
  token exchange) would cost about a day and buys nothing for a test-mode demo
  with no real money. Rotate it if this ever outlives the demo.
- **One hardcoded merchant, one shared key.** Multi-tenant auth is explicitly out
  of scope.
- **Razorpay runs in test mode.** Test-mode QR codes are not scannable by real UPI
  apps, which is exactly why the printed sticker encodes a plain HTTPS URL and
  payment happens in Standard Checkout inside the browser.

What is *not* a tradeoff: every one of our database functions is revoked from
`anon` and `authenticated` (`sql/004_grants.sql`). They are all `SECURITY DEFINER`
and therefore bypass RLS, so an EXECUTE grant to `anon` would be a direct path to
`nuke_demo()` over PostgREST. `backend/tests/test_privileges.py` guards it, because
Postgres re-grants EXECUTE to PUBLIC on every `create or replace`.
