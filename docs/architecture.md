# Architecture

## The parts

```
         printed sticker                          merchant's phone
                │                                        │
                ▼                                        ▼
   ┌────────────────────────┐               ┌────────────────────────┐
   │  SlotPage (customer)   │               │  VerifyPage (counter)  │
   │  chat · basket · pay   │               │  scan · proof · bill   │
   └───────────┬────────────┘               └───────────┬────────────┘
               │                                        │
               └──────────────┬─────────────────────────┘
                              ▼
                   ┌─────────────────────┐
                   │   FastAPI backend   │
                   │                     │
                   │  sanitize           │  ← screening, before any provider exists
                   │  agent loop         │  ← LangChain, tools, 3-tier failover
                   │  bounds.check()     │  ← THE GATE. pure function, no model
                   │  decision_log       │  ← every outcome, including refusals
                   └──────────┬──────────┘
                              │  rpc(name, params) — the entire data layer
                              ▼
                   ┌─────────────────────┐
                   │      Postgres       │
                   │  plpgsql functions  │  ← atomicity lives here
                   │  CHECK constraints  │  ← the last line, unbypassable
                   └─────────────────────┘
```

The browser never talks to Postgres. Every read and write goes through the
backend, which holds the service-role key. That is not only a layering
preference: `*.supabase.co` was DNS-blocked by Indian ISPs for roughly eight
days in Feb–Mar 2026, and the phone is the device whose network we control
least.

## The one rule everything else follows

**The model asks. The gate decides.**

`app/core/bounds.py:check()` is a pure function — no network, no clock, no
randomness, no model. It takes what the model proposed as an *input* and
returns a `Decision`. Everything a customer can act on is built from that
`Decision`, never parsed out of the model's prose. If the sentence and the gate
ever disagree, the number on the button is the gate's.

The gate runs in four places, and they must not diverge:

| where | why |
|---|---|
| `tools.propose_offer` | the negotiation |
| `tools.suggest_addon` | an upsell is another thing that may be refused |
| `payment_service.checkout` | re-gated at pay time, per basket line |
| `agent_commerce.build_quote` | the machine buyer gets the same rules |

There is deliberately no second rule engine for agents. A divergence would be a
discount an AI buyer could obtain that a human could not, which is the exact
failure this project exists to prevent. `OfferContext.caps_for()` exists so the
two committed ceilings are derived once and read by all four.

## A chat turn, end to end

`app/services/chat_service.py:chat_turn()`

```
1  is the session already paid?   → say so, stop. Nothing more can be sold.
2  sanitize                       → blocked? write a row with llm_provider NULL, stop.
3  turn limit                     → reached? the gate's own sentence, no model call.
4  load the basket                → degrades to empty if unreadable; never raises.
5  run_agent                      → the loop below.
6  audit                          → tool calls, gate decision, upsell, fallback.
7  flush the basket               → after the audit, before the reply.
8  persist + reply                → the assistant turn, the offer card, the cart.
```

Steps 2 and 3 happen **before any provider is constructed**, which is what
makes "this message cost zero tokens" a machine-checkable claim rather than an
assertion: the audit row has a NULL `llm_provider`.

Step 7 is after step 6 on purpose. An approval written to the decision log but
not to the basket would be a discount the shopper was told about, that the
merchant can prove was granted, and that nobody can actually buy.

## The agent loop

`app/core/agent.py`

```
bind_tools → ainvoke → tool_calls? → run each, append a ToolMessage → round again
                          │
                          └── no calls → that reply is the answer
```

Capped by `AGENT_MAX_STEPS` and a global deadline. Roughly forty lines of
control flow, no framework agent executor, because it has to be debuggable on a
stage.

Three tiers, tried in order:

1. **Ollama Cloud** (`gpt-oss:120b`)
2. **Groq** — same interface, different base URL; failover is a list, not a branch
3. **A deterministic responder with no model at all** — it resolves an item,
   calls `propose_offer`, and reports the gate's own sentence. Not an error
   path and not a canned string: the demo degrades to worse prose and keeps
   every guarantee.

A circuit breaker skips a tier that has failed recently. `bind_tools` is used
rather than `with_structured_output` because Ollama Cloud silently ignores
JSON-Schema structured outputs and returns plausible prose instead of raising.

## The basket

One open cart per session, one row per sku, each carrying the rate **that line**
was granted. A line enters only after the gate approved it.

The tools cannot write to it — they are synchronous and the database is not.
Approvals are collected on `OfferContext.cart_ops` and flushed by
`chat_service` once the turn is known to have produced a reply.

At checkout the basket **aggregates into** the session's existing `offer_bps` /
`offer_amount_paise` columns rather than replacing them, so `settle_payment`,
the webhook and the poller are untouched. The aggregate rate is a weighted mean
of per-line rates that are each already inside the ceiling, so it is inside it
too, and `slots.ck_granted_le_ceiling` holds unweakened.

## Settlement

Three paths reach it — the checkout callback, the Razorpay webhook, and the
phone's polling fallback — and all three call exactly one plpgsql function,
`settle_payment`, which serialises on a row lock and is idempotent on
`rzp_payment_id`. Whichever arrives second gets the winner's row back verbatim.

That is not defensive coding; it is forced. Over PostgREST each `.execute()` is
its own transaction, so the four writes settlement needs cannot be made atomic
from Python.

## Two front-end surfaces

**The customer** (`SlotPage`) holds no derived state. Every reply carries the
whole cart and the page renders what it was handed — no local add, no
optimistic line, no arithmetic. The totals on screen are the ones Postgres
summed, which is the same function the checkout re-derives from and the bill
prints from.

**The merchant** (`MerchantShell` and pages under `pages/merchant/`) is the
console: catalogue, shelves, campaign planning with a simulator, commit, the QR
sheet, the audit feed, and the post-mortem.

`frontend/src/lib/merkle.ts` is a deliberate twin of `app/core/merkle.py`, with
a shared parity fixture, so the customer's phone can verify the proof
independently rather than being told the answer.
