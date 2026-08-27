# Handoff prompt — deploy Kirana Agent

Paste everything below the line into the terminal Claude session running in
`~/RAZORX/kirana-agent`.

---

You are deploying an existing, working project. **Do not rewrite application code.**
The repo is complete through Phase 1 and all 148 tests pass. Your job is
infrastructure: GitHub, Supabase schema, Railway, Vercel, and wiring them together.

Read `docs/DEPLOY_PROMPT.md` (this file) and `README.md` first. The plan this was
built from explains the *why* behind the constraints below.

## What already exists

- `sql/001_schema.sql`, `002_functions.sql`, `003_seed.sql` — validated against
  Postgres 16. 8 tables, ~15 plpgsql functions.
- `backend/` — FastAPI, Python 3.11, `uv`. `railway.json` is already correct.
- `frontend/` — React 19 + Vite + Tailwind 4. `vercel.json` SPA rewrite already committed.
- 4 commits on `main`, clean tree, **no git remote yet**.

## Ordering matters

Railway and Vercel each need the other's URL, so the sequence is:
**GitHub → Supabase → Railway (get URL) → Vercel (needs Railway URL) → back to
Railway (needs Vercel URL) → redeploy both.** Do not try to shortcut it.

---

## Task 1 — GitHub

```
git remote add origin https://github.com/Adityashandilya555/kirana.git
git push -u origin main
```

If the remote has commits already, report what they are and stop — do not force-push.

## Task 2 — Supabase schema

The project `skulxvsbzaepfoxkkulc` currently has **0 tables and 0 migrations**.
Apply the three files as three separate named migrations via the pinned Supabase
MCP's `apply_migration`:

| file | migration name |
|---|---|
| `sql/001_schema.sql` | `001_schema` |
| `sql/002_functions.sql` | `002_functions` |
| `sql/003_seed.sql` | `003_seed` |

**Use the individual files, not `sql/all_in_one.sql`** — that one wraps everything in
an explicit `begin;/commit;` for hand-pasting into the SQL editor, which conflicts
with `apply_migration`'s own transaction handling.

Verify with `list_tables` (expect 8) and:
```sql
select public.ping();                     -- expect 'pong'
select count(*) from catalog_items;       -- expect 6
```
Then run `get_advisors` for `security` and report anything it flags. Expect notices
about RLS — every table has RLS enabled with zero policies, which is deliberate:
the backend uses the service role and the browser never touches Postgres directly.

## Task 3 — Railway

Project `jubilant-happiness` exists with no services. Create one from the GitHub repo.

- **Root directory: `backend/`** — not the repo root.
- **Region: Singapore** (`asia-southeast1-eqsg3a`). There is no India region; Singapore
  is nearest and this is where the DB round-trips happen.
- **Serverless: OFF.** Verify it actually persisted — toggle on, off, then deploy.
  Railway sleeps on *outbound* silence after 10 minutes, and a slept service returns
  502 on the first request. That is a demo-killer.
- `railway.json` already sets the start command, `healthcheckPath: /health`, and the
  builder. Do not override them in the dashboard; config-as-code wins anyway.
- **Generate a public domain.** Services are private by default and a working deploy
  with no domain looks identical to a broken one.

### Railway environment variables

```
APP_ENV=production
DEMO_MODE=true
SUPABASE_URL=https://skulxvsbzaepfoxkkulc.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<from Supabase → Project Settings → API → service_role>
MERCHANT_API_KEY=<invent a long random string>
BACKEND_CORS_ORIGINS=<set in Task 5>
PUBLIC_APP_BASE_URL=<set in Task 5>
OLLAMA_API_KEY=<from ollama.com/settings/keys>
OLLAMA_MODEL=gpt-oss:120b
GROQ_API_KEY=<from console.groq.com/keys>
RAZORPAY_KEY_ID=<test mode>
RAZORPAY_KEY_SECRET=<test mode>
RAZORPAY_WEBHOOK_SECRET=<test mode>
```

🔴 **`DATABASE_URL` must NOT be set on Railway.** The backend picks its database
backend at startup: if `DATABASE_URL` is present it uses asyncpg against a local
Postgres, otherwise it uses Supabase/PostgREST. Setting it in production points the
service at a database that does not exist there. Leave it absent.

Verify: `curl https://<railway-domain>/health` → `{"ok":true,...}` and
`curl https://<railway-domain>/health/deep` → `{"ok":true,"db":"up","backend":"supabase"}`.
**`"backend":"supabase"` is the assertion that matters** — `"postgres"` means
`DATABASE_URL` leaked into the environment.

## Task 4 — Vercel

- **Root directory: `frontend/`**.
- Framework preset: Vite (auto-detected). Output `dist`. Do not override the build.
- 🔴 **Disable Deployment Protection → Vercel Authentication.** On Hobby it is ON by
  default and auth-walls preview deployments. A phone scanning a QR that points at a
  protected URL lands on a Vercel login page. If you would rather keep protection on,
  then every QR must point at the *production* domain only — but disabling is simpler
  and this is test-mode payments with no real money.

### Vercel environment variables

Everything here is baked into the public bundle at build time. Never put a secret in one.

```
VITE_API_BASE_URL=https://<railway-domain>
VITE_MERCHANT_API_KEY=<same value as MERCHANT_API_KEY>
VITE_RAZORPAY_KEY_ID=<test key_id — publishable by design>
```

`RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `SUPABASE_SERVICE_ROLE_KEY`,
`OLLAMA_API_KEY` and `GROQ_API_KEY` must **never** appear with a `VITE_` prefix.

Changing any `VITE_` var requires a rebuild — they are inlined at build time, not read
at runtime. Budget a redeploy, do not plan to change one live.

## Task 5 — Wire them together, then redeploy

Back on Railway, now that the Vercel domain exists:

```
BACKEND_CORS_ORIGINS=https://<vercel-production-domain>
PUBLIC_APP_BASE_URL=https://<vercel-production-domain>
```

`PUBLIC_APP_BASE_URL` is what gets encoded into every printed QR code, so it must be
the domain a customer's phone will actually reach. Redeploy Railway after setting it.

(The backend also allows `https://*.vercel.app` via a regex, so preview deployments
work without another backend redeploy. The explicit origin above is still worth setting.)

## Task 6 — Verify end to end

```bash
BASE=https://<railway-domain>
KEY=<MERCHANT_API_KEY>

curl -s $BASE/health/deep                       # expect backend":"supabase"

CID=$(curl -s -X POST $BASE/api/v1/campaigns -H "X-Merchant-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"name":"Diwali Haggle","budget_paise":500000,"max_discount_bps":2000,
       "margin_floor_bps":1200,"max_turns":6,"slot_count":24}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -s -X POST $BASE/api/v1/campaigns/$CID/commit -H "X-Merchant-Key: $KEY"
curl -s -X POST $BASE/api/v1/campaigns/$CID/commit -H "X-Merchant-Key: $KEY"   # expect 409
```

Then open `$BASE/api/v1/campaigns/$CID/qr-sheet?k=$KEY` in a browser. Expect 24 QR
codes, each with a 10-character token, and the Merkle root printed at the top.

**The real acceptance test, and it must be done on the actual Android phone, on mobile
data, with wifi off:** open `https://<vercel-domain>/s/PHASE0TEST`. Three things must
hold — the page renders (proving the SPA rewrite works on a *deep link*, not just `/`),
it shows the backend health JSON including `"db":"up"` (proving CORS + domain + DB in
one shot), and tapping "Start camera" shows a live rear-facing preview.

Testing `/` instead of a deep link proves nothing. Testing from the laptop proves
nothing about CORS or the Vercel auth wall.

## Do not

- Do not modify anything under `backend/app/`, `frontend/src/`, or `sql/`. If something
  genuinely needs a code change, say so and stop rather than editing.
- Do not run `sql/all_in_one.sql` through `apply_migration`.
- Do not set `DATABASE_URL` on Railway.
- Do not commit `backend/.env` or `frontend/.env.local`.
- Do not use the account-wide Supabase connector — use only the pinned project server.

## Report back

The Railway domain, the Vercel domain, whether `/health/deep` says `"backend":"supabase"`,
the campaign id and Merkle root from Task 6, and the result of the phone test.
