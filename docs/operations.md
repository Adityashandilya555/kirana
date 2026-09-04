# Operations

Three services. Supabase holds the data, Railway runs the backend (Singapore —
there is no India region and that is where the round trips happen), Vercel
serves the frontend.

## The one deploy rule

**Migrations go first. Always.**

Railway auto-deploys on a push to `main`. Postgres does not. If a merge
contains a new `sql/` file, apply it to Supabase *before* the merge, or you
will have a backend calling functions the database does not have.

This is not hypothetical. A backend shipped ahead of `sql/025_cart.sql`, and
because the cart is read at the top of every chat turn, PostgREST answered

```
PGRST202: Could not find the function public.get_cart(p_session_id)
```

on every single message. Every reply to every shopper became *"The shop's
system did not respond."* The whole negotiation was down because a basket was
missing.

Two things now guard it. `cart_service.load` degrades to an empty basket and
logs at ERROR rather than propagating — a shop that cannot remember what you
picked up should still be able to quote a price. And any PR touching `sql/`
should say so in its description, because nothing else will remind you.

**Before merging anything, check:** does this PR add a file under `sql/`?
If yes, apply it first and confirm with `select proname from pg_proc ...`.

## Environment

### Railway (backend)

```
APP_ENV=production
DEMO_MODE=true
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service_role, not anon>
MERCHANT_API_KEY=<long random string>
BACKEND_CORS_ORIGINS=<the Vercel origin>
PUBLIC_APP_BASE_URL=<the Vercel origin>
PUBLIC_API_BASE_URL=<the Railway origin>
OLLAMA_API_KEY=...
OLLAMA_MODEL=gpt-oss:120b
GROQ_API_KEY=...
RAZORPAY_KEY_ID=<test mode>
RAZORPAY_KEY_SECRET=<test mode>
RAZORPAY_WEBHOOK_SECRET=<test mode, a DIFFERENT value>
AGENT_SIGNING_SECRET_KEY=<base64 ed25519 seed>
```

🔴 **`DATABASE_URL` must NOT be set on Railway.** The backend picks its backend
at startup: `DATABASE_URL` present means asyncpg against a local Postgres,
absent means Supabase over PostgREST. Setting it in production points the app
at a database that is not there.

🔴 **`ALLOW_STUB_PAYMENTS` must be unset or false.** It bypasses signature
verification entirely. It refuses in production regardless, but do not rely on
that alone.

**Serverless must be OFF**, and verify it actually persisted. Railway sleeps on
outbound silence after ten minutes and a slept service returns 502 on the first
request, which is a demo-killer.

### Vercel (frontend)

```
VITE_API_BASE_URL=<the Railway origin>
VITE_MERCHANT_API_KEY=<the same value as MERCHANT_API_KEY>
```

The merchant key ships in the public bundle. That is a known and documented
tradeoff — see [security-model.md](security-model.md).

### Ordering

Railway and Vercel each need the other's URL, so:
**Supabase → Railway (get URL) → Vercel (needs it) → back to Railway
(`BACKEND_CORS_ORIGINS`) → redeploy both.**

## Runbook

Every entry below has actually happened.

### "The shop's system did not respond" on every message

A missing plpgsql function. Check the deploy logs for `PGRST202`. Apply the
missing migration; PostgREST caches its schema, so follow with
`notify pgrst, 'reload schema';`.

### A screen frozen on "Opening checkout…", and no redemption QR

The customer has paid and no code was minted. Look for `confirm REFUSED` or
`confirm FAILED` at ERROR in the backend logs — those lines name the order, the
payment and the reason. Common causes: the key secret and the checkout `key_id`
are from different Razorpay accounts, or the callback arrived without a
signature.

The customer's phone now shows a **Check again** strip rather than a dead
spinner, and `/payments/confirm` asks Razorpay directly before refusing.

### Stickers that say "This code has already been used" but were never redeemed

Abandoned checkouts left the slot `locked` and the budget reserved.

```sql
select s.slot_token, s.status, se.rzp_order_id
  from slots s join sessions se on se.slot_id = s.id
 where s.status = 'locked';
```

Confirm no captured payment exists for those orders, then
`select release_reservation('<order_id>', 'stranded');` for each. The slot
returns to `offered` and the budget comes back.

### The agent refuses everything

Not a bug — check the campaign's margin floor against the catalogue:

```sql
select ci.sku, ci.name,
       public.margin_bps_after(ci.price_paise, ci.cost_paise, 0) as margin_bps,
       (public.margin_bps_after(ci.price_paise, ci.cost_paise, 0) < c.margin_floor_bps)
         as blocked
  from catalog_items ci, campaigns c
 where c.id = '<campaign>' and ci.active
 order by margin_bps;
```

The floor is measured on the **sale** price, so an item whose margin at list
price is below the floor can be given no discount at all. A floor of 20% against
a catalogue whose median margin is 15% blocks most of the shop.

### A screen 500s and the browser blames CORS

CORS is almost never the problem. An unhandled 500 is produced by Starlette's
outermost error middleware, which sits *outside* `CORSMiddleware` and therefore
sends no `Access-Control-Allow-Origin` header — the browser has no vocabulary
for that except "CORS". Look for the real 500 in the logs. If the same route
returns a 401 *with* the header, CORS is fine.

### `300 Multiple Choices` from PostgREST

A duplicated function signature. See the two rules in
[database.md](database.md#two-rules-for-editing-any-of-them).

## Before a demo or a recording

- [ ] `select count(*) from slots where status='locked'` → **0**
- [ ] `select reserved_paise from campaigns where status='live'` → **0**
- [ ] The campaign's margin floor leaves room on the items you will ask for
- [ ] At least two unused stickers, and you know their codes
- [ ] Customer phone: clear site data, or use **not you?** in the chat header —
      the number is remembered in `localStorage` and the doorstep is skipped
- [ ] Merchant phone: logged in, camera permission already granted
- [ ] Razorpay test mode. Card `4111 1111 1111 1111`, any future expiry, any
      CVV, OTP `1234`. Tap **Card** — UPI Collect is blocked on Android
- [ ] Send one throwaway message first; the first request of the day pays the
      model's cold start

## Tests

There is no CI. Run them.

```
make test        # no database, no network
make db-reset    # then:
make test-all    # adds the integration suite against the local container
make typecheck   # frontend tsc + production build
```

Currently 527 backend tests (including integration) and 97 frontend tests.
