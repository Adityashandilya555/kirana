# Database

Postgres 16. Money is `bigint` paise, rates are `int` basis points, and there
are no floats in any money path.

## Tables

| table | what it holds |
|---|---|
| `merchants` | one row, for now. Multi-tenant is out of scope. |
| `catalog_items` | products. `cost_paise` lives here and must never leave the server. `ck_cost_lt_price`. |
| `campaigns` | budget, max discount, margin floor, turn limit, the Merkle root, the tier rule, sticker sharing. |
| `slots` | one row per printed sticker: its ceiling, salt, leaf hash, inclusion proof, and status. |
| `shelves`, `shelf_items` | a named subset of the catalogue. A slot bound to a shelf can only ever price what is on it. |
| `campaign_product_caps` | the per-product ceiling committed at commit time, with its own Merkle root. |
| `customers` | a phone number, scoped per merchant, plus the last four digits denormalised so the counter can show `…4821` without ever selecting the full number. |
| `sessions` | one conversation. Transcript, turn count, the scope and tier snapshots, and the aggregated offer. |
| `carts`, `cart_items` | the basket. One open cart per session, one row per sku, each carrying the rate that line was granted. |
| `payments` | one row per settlement. The redemption token lives here. |
| `decisions` | append-only. Every outcome, including refusals and blocked injections. |
| `webhook_events` | Razorpay deliveries, deduped on event id. |

### Constraints that are the security model

- `slots.ck_granted_le_ceiling` — an over-ceiling grant cannot be stored.
- `campaigns.ck_budget_envelope` — `spent + reserved <= budget`.
- `campaigns.ck_commit_integrity` — a non-draft campaign always has a root.
- `uq_payment_captured_per_customer` — one discount per shopper per campaign.
- `uq_payment_captured_once_slot` — and, for a one-shot sticker, one per slot.
- `uq_payment_rzp_payment_id` — the idempotency anchor for settlement.

### Snapshots, and why they exist

`sessions.scope_snapshot` and `sessions.tier_key` / `tier_cap_fraction_bps` are
written when the session opens and never recomputed.

The console presents scope as *evidence* — "these products were absent from the
model's context". Evidence derived from mutable state is not evidence: adding a
sku to a shelf after a conversation used to make it disappear from that
conversation's withheld list. Same argument for the tier: a band that flips
mid-negotiation is not a rule, it is a mood.

## Functions

Every atomic write is plpgsql, because over PostgREST each call is its own
transaction. The entire data layer in Python is `rpc(name, params)`.

| | |
|---|---|
| `open_session_by_token` | scan → session. Idempotent; resumes rather than forking. Evaluates the tier and writes both snapshots. |
| `get_session_context` | everything the gate and the prompt need, in one round trip. |
| `append_session_turn` | one message onto the transcript. |
| `commit_campaign` | writes the slots and the caps, stamps the root. Draft-only. |
| `upsert_catalog` | import, with `p_replace` retiring what is absent. |
| `get_cart`, `upsert_cart_item`, `remove_cart_item`, `clear_cart` | the basket. |
| `reserve_cart` | re-checks **every** line against every ceiling, recomputes the amounts from the live catalogue, then reserves. |
| `settle_payment` | the single settlement call. Row lock, idempotent on payment id. |
| `release_reservation` | hands the budget and the basket back. |
| `verify_redemption` | burn-once, plus the customer's standing and the bill. |
| `get_redemption` | the customer's read-only view. Must never burn. |
| `get_agent_catalog`, `get_slot_quote_context` | the machine-buyer projections. |
| `get_audit_feed`, `get_session_audit` | the console. |

### Two rules for editing any of them

**1. `create or replace` with a changed parameter list creates an OVERLOAD, it
does not replace.** PostgREST then cannot choose and answers `300 Multiple
Choices`. This has happened twice. Changing a signature means
`drop function if exists public.name(exact, arg, types);` first — by exact
signature, because dropping by bare name is itself ambiguous once the overload
exists.

**2. Postgres re-grants EXECUTE to PUBLIC on every `create or replace`.** Every
migration must end with a privileges block that revokes from `public`, `anon`
and `authenticated` and grants to `service_role`. `tests/test_privileges.py`
guards it.

## Migrations

`sql/NNN_name.sql`, applied in filename order. `make db-apply` globs them, so a
new file is picked up without editing the Makefile.

They are applied to Supabase as named migrations. **The file name and the
applied migration name are the same thing** — that is a rule, not a
coincidence, and it exists because the two drifted badly once.

### The drift, written down

Four migrations were applied directly to Supabase and never written into
`sql/`: `004_health_check`, `011_fix_upsert_shelf`, `012_create_campaign_binding`
and `014_agent_commerce`. A fifth, `015_scope_snapshot`, was described in
`sql/011_scope_snapshot.sql` — a file containing prose and no SQL.

The consequence was invisible for as long as nobody replayed the directory:
`sessions.scope_snapshot` existed in production and in no local database, so
`make db-reset` died at file 24 — the first one to reference the column from a
SQL-language body. The plpgsql ones in 016 and 017 are not parsed until they
run, so they "succeeded" against a schema that could not execute them.

`sql/015_scope_snapshot_column.sql` closes the one gap that actually blocked a
replay. The rest of that drift is not undone, and the numbering starts from 016
rather than 012 so it at least stops growing.

`sql/all_in_one.sql` used to sit here as a hand-pasteable snapshot. It was a
stale copy that later migrations supersede — replaying it would *undo* them —
so it has been removed.

### Local

```
make db-reset     # fresh container on :5433, every migration in order
make db-psql      # a shell on it
make test-all     # unit + integration, pointed at that container
```

The container publishes a port so things other than `docker exec` can reach it;
without that, the integration suite skipped silently and `make test-all`
reported a clean pass having run nothing.
