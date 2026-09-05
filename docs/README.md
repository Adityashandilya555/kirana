# Kirana Agent — documentation

An offline shop's printed QR sticker, turned into an AI that negotiates prices
inside bounds the shopkeeper committed to cryptographically before the sticker
was printed.

Start here, then follow whichever door you came in through.

| | |
|---|---|
| [architecture.md](architecture.md) | The parts, and how a request moves through them. |
| [security-model.md](security-model.md) | The four guarantees and where each is enforced. Read this one. |
| [api.md](api.md) | Every HTTP endpoint, grouped by who is allowed to call it. |
| [database.md](database.md) | Tables, plpgsql functions, migrations, and the drift they carry. |
| [operations.md](operations.md) | Deploying, and the failures that have actually happened. |

## The sixty-second version

A shopkeeper opens a campaign in the merchant console and sets four numbers: a
budget, a maximum discount, a margin floor, and — per printed sticker — a
ceiling. Pressing **commit** hashes every sticker's ceiling into one Merkle
root and prints the QR sheet. From that moment the promise is fixed: changing a
ceiling and still showing the same root would mean finding a second preimage
for SHA-256.

A customer scans a sticker and lands in a chat. They haggle. The language model
cannot grant anything — it can only call `propose_offer`, and a pure function
with no model in it decides what is actually given. The model never sees the
ceiling, so no prompt injection can leak or raise it. Approved lines go into a
basket, several items at their own rates, paid for once through Razorpay.

At the counter the shopkeeper scans the customer's redemption QR. It is green
once and red forever after, and it shows who is standing there and what they
bought. The customer's own phone has already recomputed the leaf and walked the
proof to the published root, so the promise is verified by the buyer rather
than asserted by the seller.

The same shop is readable by a machine: a discovery document, an agent
catalogue, and Ed25519-signed quotes that carry their own inclusion proof.

## Layout

```
sql/         schema, plpgsql functions, seed. Numbered migrations, applied in order.
backend/     FastAPI + LangChain. Deployed on Railway (Singapore).
frontend/    React 19 + Vite + Tailwind 4. Deployed on Vercel.
docs/        this.
```

## Running it

```
cp backend/.env.example backend/.env        # fill in the blanks
cp frontend/.env.example frontend/.env.local
make db-reset                               # local Postgres via Docker, on :5433
make dev-api                                # :8000
make dev-web                                # :5173
```

`make help` lists the rest. `make test` is the no-database suite; `make
test-all` adds the integration tests and needs `make db-reset` first.

## House style, so the comments make sense

Comments in this codebase explain **why**, not what. Where one is unusually
long it is because the decision behind it was expensive — a leak, an outage, or
a race that took a day to find. Several of them name the exact failure they
prevent. They are load-bearing; if you change the code under one, change the
comment or delete it, but do not leave it lying.

Two conventions worth knowing before reading any of it:

- **Money is paise (`bigint`). Rates are basis points (`int`).** 100 bps = 1%.
  There are no floats in any money path, anywhere, on purpose.
- **Every atomic write is a plpgsql function.** Not because plpgsql is nice,
  but because Supabase is reached over PostgREST where each call is its own
  transaction — so "increment spent, mark the slot redeemed, insert the
  payment, mint the token" cannot be made atomic from Python.
