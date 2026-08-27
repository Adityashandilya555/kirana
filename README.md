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

## Layout

    sql/        schema, plpgsql functions, demo seed
    backend/    FastAPI + LangChain agent (Railway)
    frontend/   React + Vite + Tailwind v4 (Vercel)

## Quickstart

    cp backend/.env.example backend/.env      # fill in the blanks
    cp frontend/.env.example frontend/.env.local
    make db-reset                             # local Postgres via Docker
    make dev-api                              # :8000
    make dev-web                              # :5173

`make help` lists everything.

## Non-obvious decisions

- **Money is paise (`bigint`), rates are basis points (`int`). No floats.**
- **All Supabase access is server-side.** `*.supabase.co` was DNS-blocked by
  Indian ISPs for ~8 days in Feb–Mar 2026; the phone is the device whose network
  we control least.
- **`bind_tools`, never `with_structured_output`.** Ollama Cloud silently
  ignores JSON-Schema structured outputs (ollama/ollama#12362), so schema
  forcing returns plausible prose instead of raising.
- **The printed QR encodes a plain HTTPS URL**, not a payment QR. Razorpay QR
  codes only work in live mode.
