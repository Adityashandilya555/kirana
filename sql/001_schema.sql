-- Kirana Agent schema.
-- Money is paise (bigint). Rates are basis points (int). No floats, ever.
create extension if not exists pgcrypto;

create table merchants (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  store_line  text not null default '',
  created_at  timestamptz not null default now()
);

create table catalog_items (
  id           uuid primary key default gen_random_uuid(),
  merchant_id  uuid not null references merchants(id) on delete cascade,
  sku          text not null,
  name         text not null,
  unit         text not null default 'pc',
  price_paise  bigint not null check (price_paise >= 100),
  cost_paise   bigint not null check (cost_paise >= 0),
  active       boolean not null default true,
  created_at   timestamptz not null default now(),
  constraint uq_catalog_sku   unique (merchant_id, sku),
  constraint ck_cost_lt_price check (cost_paise < price_paise)
);

create table campaigns (
  id                uuid primary key default gen_random_uuid(),
  merchant_id       uuid not null references merchants(id) on delete cascade,
  name              text not null,
  status            text not null default 'draft'
                      check (status in ('draft','live','paused','closed')),
  budget_paise      bigint not null check (budget_paise > 0),
  spent_paise       bigint not null default 0 check (spent_paise    >= 0),
  reserved_paise    bigint not null default 0 check (reserved_paise >= 0),
  max_discount_bps  int not null check (max_discount_bps between 0 and 10000),
  margin_floor_bps  int not null check (margin_floor_bps between 0 and 9000),
  max_turns         int not null default 6 check (max_turns between 1 and 20),
  slot_count        int not null check (slot_count between 1 and 512),
  merkle_root       text,
  policy_hash       text,
  tree_size         int,
  committed_at      timestamptz,
  created_at        timestamptz not null default now(),
  -- GUARANTEE 3: the budget envelope is enforced by Postgres, not the app.
  constraint ck_budget_envelope
    check (spent_paise + reserved_paise <= budget_paise),
  -- A committed campaign always has a root; a draft never does.
  constraint ck_commit_integrity check (
       (status =  'draft' and merkle_root is null and committed_at is null)
    or (status <> 'draft' and merkle_root is not null and policy_hash is not null
        and tree_size is not null and committed_at is not null)
  )
);
create index ix_campaigns_merchant on campaigns (merchant_id, created_at desc);

create table slots (
  id                uuid primary key default gen_random_uuid(),
  campaign_id       uuid not null references campaigns(id) on delete cascade,
  leaf_index        int  not null check (leaf_index >= 0),
  slot_token        text not null,
  salt_hex          text not null,
  ceiling_bps       int  not null check (ceiling_bps between 0 and 10000),
  leaf_hash         text not null,
  proof             jsonb not null default '[]'::jsonb,
  status            text not null default 'unused'
                      check (status in ('unused','offered','locked','redeemed','void')),
  granted_bps       int,
  discount_paise    bigint,
  reserved_paise    bigint not null default 0 check (reserved_paise >= 0),
  redemption_token  text,
  locked_at         timestamptz,
  redeemed_at       timestamptz,
  verified_at       timestamptz,
  created_at        timestamptz not null default now(),
  constraint uq_slot_token unique (slot_token),
  constraint uq_slot_leaf  unique (campaign_id, leaf_index),
  -- GUARANTEE 2: storing an over-ceiling grant is a DATABASE ERROR.
  constraint ck_granted_le_ceiling
    check (granted_bps is null or granted_bps <= ceiling_bps)
);
create unique index uq_slot_redemption_token
  on slots (redemption_token) where redemption_token is not null;
create index ix_slots_campaign_status on slots (campaign_id, status);

create table sessions (
  id                    uuid primary key default gen_random_uuid(),
  slot_id               uuid not null references slots(id)     on delete cascade,
  campaign_id           uuid not null references campaigns(id) on delete cascade,
  status                text not null default 'open'
                          check (status in ('open','offer_locked','paid','abandoned','blocked')),
  transport             text not null default 'web'
                          check (transport in ('web','telegram')),
  transport_ref         text,
  turn_count            int  not null default 0 check (turn_count >= 0),
  transcript            jsonb not null default '[]'::jsonb,
  current_sku           text,
  current_qty           int  not null default 1 check (current_qty between 1 and 20),
  offer_bps             int,
  offer_discount_paise  bigint,
  offer_amount_paise    bigint,
  rzp_order_id          text,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);
-- A phone reload resumes the conversation; it never forks a second one.
create unique index uq_one_live_session_per_slot
  on sessions (slot_id) where status in ('open','offer_locked');
create unique index uq_sessions_rzp_order
  on sessions (rzp_order_id) where rzp_order_id is not null;
create index ix_sessions_slot on sessions (slot_id, created_at desc);

-- Append-only. EVERY outcome lands here: approvals, refusals, blocks.
-- Refusals are the product, not an error path.
create table decisions (
  id                  bigserial primary key,
  campaign_id         uuid references campaigns(id) on delete cascade,
  slot_id             uuid references slots(id)     on delete set null,
  session_id          uuid references sessions(id)  on delete set null,
  turn_index          int,
  kind                text not null check (kind in (
                        'campaign_committed','session_opened','injection_blocked',
                        'tool_call','proposal','approved','clamped','rejected',
                        'llm_fallback','llm_error','order_created','settled',
                        'payment_failed','verified','verify_rejected')),
  code                text not null,
  proposed_bps        int,
  granted_bps         int,
  binding_constraint  text,
  human_reason        text not null,
  customer_reason     text,
  -- NULL here on an injection_blocked row is the machine-checkable proof
  -- that the model was never invoked.
  llm_provider        text,
  llm_model           text,
  latency_ms          int,
  raw_user_message    text,
  raw_llm_output      text,
  meta                jsonb not null default '{}'::jsonb,
  created_at          timestamptz not null default now()
);
create index ix_decisions_feed    on decisions (campaign_id, id desc);
create index ix_decisions_session on decisions (session_id,  id desc);

create table payments (
  id              uuid primary key default gen_random_uuid(),
  session_id      uuid not null references sessions(id)  on delete cascade,
  slot_id         uuid not null references slots(id)     on delete cascade,
  campaign_id     uuid not null references campaigns(id) on delete cascade,
  rzp_order_id    text not null,
  rzp_payment_id  text not null,
  rzp_signature   text,
  amount_paise    bigint not null check (amount_paise >= 100),
  discount_paise  bigint not null check (discount_paise >= 0),
  discount_bps    int not null,
  status          text not null default 'captured'
                    check (status in ('created','captured','failed','refunded')),
  settled_via     text not null
                    check (settled_via in ('checkout_handler','webhook','poll','manual')),
  created_at      timestamptz not null default now(),
  -- Idempotency anchor: one row per Razorpay payment, ever.
  constraint uq_payment_rzp_payment_id unique (rzp_payment_id)
);
-- GUARANTEE 1: at most one captured payment per slot. Double redemption is
-- impossible at the storage layer, not merely checked in application code.
create unique index uq_payment_one_captured_per_slot
  on payments (slot_id) where status = 'captured';
create index ix_payments_order on payments (rzp_order_id);

create table webhook_events (
  id              uuid primary key default gen_random_uuid(),
  event_id        text not null,
  event_type      text not null,
  rzp_order_id    text,
  rzp_payment_id  text,
  signature_ok    boolean not null,
  processed       boolean not null default false,
  process_error   text,
  payload         jsonb not null,
  received_at     timestamptz not null default now(),
  -- Razorpay fires BOTH order.paid and payment.captured, and retries on
  -- non-2xx. Dedupe on the event id it gives us.
  constraint uq_webhook_event_id unique (event_id)
);
create index ix_webhook_pending on webhook_events (processed, received_at desc);

-- The backend uses service_role, which bypasses RLS. Enabling RLS with zero
-- policies therefore means: deny everything to anon/authenticated. The
-- browser never talks to Postgres directly.
alter table merchants      enable row level security;
alter table catalog_items  enable row level security;
alter table campaigns      enable row level security;
alter table slots          enable row level security;
alter table sessions       enable row level security;
alter table decisions      enable row level security;
alter table payments       enable row level security;
alter table webhook_events enable row level security;
