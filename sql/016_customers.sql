-- Customer identity: a phone number, asked before the haggling starts.
--
-- NUMBERING. This file is 016, not 012, because the directory and the applied
-- migrations had drifted. Supabase has fifteen migrations applied; sql/ had
-- eleven files under different names, and four applied migrations
-- (004_health_check, 011_fix_upsert_shelf, 012_create_campaign_binding,
-- 014_agent_commerce) were never written down here at all. 011_scope_snapshot
-- contains no SQL, only a note saying its statements went in as 015. Numbering
-- from 016 stops the drift growing even though it does not undo it; from here
-- the file name and the applied migration name are the same thing.
--
-- WHY A CUSTOMER EXISTS NOW. Two things need one, and neither can be built
-- without it:
--
--   * A sticker is a shelf fixture, not a coupon. Many people scan the same
--     one. "Used up" therefore has to mean "used by this person", which needs
--     a person.
--   * Ceilings are about to depend on how much someone has shopped here.
--
-- This migration adds the identity and nothing that spends it. No pricing
-- behaviour changes, and customer_id is nullable everywhere, so every existing
-- session, payment and slot keeps working exactly as before.

create table if not exists customers (
  id            uuid primary key default gen_random_uuid(),
  merchant_id   uuid not null references merchants(id) on delete cascade,
  -- E.164. Normalisation happens in Python (app/services/customer_service.py)
  -- where it is pure and testable; this constraint is the backstop that keeps
  -- a bad value out of the column if some other caller ever appears.
  phone_e164    text not null check (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
  -- Denormalised so the counter can show "…4821" without the console ever
  -- selecting the full number.
  phone_last4   text not null check (phone_last4 ~ '^[0-9]{4}$'),
  display_name  text not null default '',
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  created_at    timestamptz not null default now(),
  -- One customer per phone per shop. Deliberately scoped to the merchant:
  -- two shops on this platform must not be able to learn they share a shopper.
  constraint uq_customer_phone unique (merchant_id, phone_e164)
);
create index if not exists ix_customers_merchant
  on customers (merchant_id, last_seen_at desc);
alter table customers enable row level security;

alter table sessions
  add column if not exists customer_id uuid references customers(id) on delete set null;
create index if not exists ix_sessions_customer
  on sessions (customer_id, created_at desc) where customer_id is not null;

-- ------------------------------------------------------------------------
-- The live-session index, split.
--
-- uq_one_live_session_per_slot was `unique (slot_id) where status in
-- ('open','offer_locked')` -- one live negotiation per sticker, full stop.
-- That was right when a sticker was a one-shot coupon. It is a LEAK the moment
-- stickers are shared shelf fixtures: customer B scanning the same sticker
-- while A is mid-negotiation does not start a session, it RESUMES A's, and is
-- handed A's transcript.
--
-- Two partial indexes rather than `nulls not distinct`: no Postgres-version
-- assumption, and the legacy semantics stay visible in the predicate instead
-- of being implied by a modifier.
-- ------------------------------------------------------------------------
drop index if exists uq_one_live_session_per_slot;

-- Unidentified scans keep exactly today's behaviour, byte for byte.
create unique index if not exists uq_one_live_anon_session_per_slot
  on sessions (slot_id)
  where customer_id is null and status in ('open','offer_locked');

-- Identified scans: one live negotiation per (sticker, customer).
create unique index if not exists uq_one_live_session_per_slot_customer
  on sessions (slot_id, customer_id)
  where customer_id is not null and status in ('open','offer_locked');

-- ------------------------------------------------------------------------
-- upsert_customer -- idempotent by (merchant, phone).
-- ------------------------------------------------------------------------
create or replace function public.upsert_customer(
  p_merchant_id uuid, p_phone_e164 text, p_display_name text default ''
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_cust customers%rowtype;
begin
  if p_phone_e164 is null or p_phone_e164 = '' then
    raise exception 'PHONE_REQUIRED';
  end if;

  insert into customers (merchant_id, phone_e164, phone_last4, display_name)
  values (p_merchant_id, p_phone_e164, right(p_phone_e164, 4),
          coalesce(nullif(trim(p_display_name), ''), ''))
  on conflict (merchant_id, phone_e164) do update
    set last_seen_at = now(),
        -- Never overwrite a known name with a blank one.
        display_name = case
          when nullif(trim(excluded.display_name), '') is not null
            then excluded.display_name
          else customers.display_name
        end
  returning * into v_cust;

  return jsonb_build_object(
    'id', v_cust.id,
    'phone_last4', v_cust.phone_last4,
    'display_name', v_cust.display_name,
    'first_seen_at', v_cust.first_seen_at,
    'returning', v_cust.first_seen_at < v_cust.last_seen_at
  );
end $$;

-- ------------------------------------------------------------------------
-- open_session_by_token, now customer-aware.
--
-- DROP FIRST. `create or replace` with a new parameter list creates an
-- OVERLOAD, it does not replace; PostgREST then cannot choose between the two
-- and answers 300 Multiple Choices on every single scan. This is the highest
-- probability outage in the whole change, so it gets an explicit drop.
--
-- The scope-snapshot block below is carried over verbatim from migration 015.
-- It is not new here and must not be simplified: it records what the model
-- could see AT THE TIME, and reconstructing it later from mutable shelves is
-- what that migration exists to stop.
-- ------------------------------------------------------------------------
drop function if exists public.open_session_by_token(text, text, text);

create or replace function public.open_session_by_token(
  p_slot_token text,
  p_transport text default 'web',
  p_transport_ref text default null,
  p_phone_e164 text default null
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_slot slots%rowtype; v_camp campaigns%rowtype; v_sess sessions%rowtype;
        v_resumed boolean := true; v_scope jsonb;
        v_customer_id uuid; v_customer jsonb;
begin
  select * into v_slot from slots where slot_token = upper(trim(p_slot_token));
  if not found then raise exception 'SLOT_NOT_FOUND'; end if;

  select * into v_camp from campaigns where id = v_slot.campaign_id;
  if v_camp.status <> 'live' then raise exception 'CAMPAIGN_NOT_LIVE'; end if;

  if p_phone_e164 is not null and p_phone_e164 <> '' then
    v_customer := upsert_customer(v_camp.merchant_id, p_phone_e164);
    v_customer_id := (v_customer->>'id')::uuid;
  end if;

  -- Resume THIS customer's negotiation, not whoever touched the sticker last.
  if v_customer_id is not null then
    select * into v_sess from sessions
     where slot_id = v_slot.id and customer_id = v_customer_id
       and status in ('open','offer_locked')
     order by created_at desc limit 1;
  else
    select * into v_sess from sessions
     where slot_id = v_slot.id and customer_id is null
       and status in ('open','offer_locked')
     order by created_at desc limit 1;
  end if;

  if not found then
    if v_slot.status not in ('unused','offered') then
      raise exception 'SLOT_NOT_OPEN';
    end if;
    v_resumed := false;

    -- The same predicate get_session_context uses. Computed once, here, so
    -- the record and the thing it records cannot drift.
    select jsonb_build_object(
      'visible', coalesce(jsonb_agg(ci.sku order by ci.sku)
                   filter (where ci.in_scope), '[]'::jsonb),
      'withheld', coalesce(jsonb_agg(ci.sku order by ci.sku)
                    filter (where not ci.in_scope), '[]'::jsonb),
      'captured_at', now()
    ) into v_scope
    from (
      select c.sku,
             (v_slot.bound_sku is not null and c.sku = v_slot.bound_sku
              or v_slot.bound_sku is null and v_slot.shelf_id is not null
                 and exists (select 1 from shelf_items si
                              where si.shelf_id = v_slot.shelf_id and si.sku = c.sku)
              or v_slot.bound_sku is null and v_slot.shelf_id is null) as in_scope
        from catalog_items c
       where c.merchant_id = v_camp.merchant_id and c.active
    ) ci;

    insert into sessions (slot_id, campaign_id, transport, transport_ref,
                          scope_snapshot, customer_id)
    values (v_slot.id, v_slot.campaign_id, p_transport, p_transport_ref,
            v_scope, v_customer_id)
    returning * into v_sess;

    update slots set status = 'offered'
     where id = v_slot.id and status = 'unused';

    insert into decisions (campaign_id, slot_id, session_id, kind, code,
                           human_reason, customer_reason, meta)
    values (v_camp.id, v_slot.id, v_sess.id, 'session_opened', 'C02_SESSION_OPENED',
            format('Session opened on slot %s (ceiling %s bps, leaf %s). Scope: %s visible, %s withheld.%s',
                   v_slot.slot_token, v_slot.ceiling_bps, v_slot.leaf_index,
                   jsonb_array_length(v_scope->'visible'),
                   jsonb_array_length(v_scope->'withheld'),
                   case when v_customer_id is null then ' Customer not identified.'
                        else format(' Customer …%s.', v_customer->>'phone_last4') end),
            'Welcome! Ask me about anything on the shelf.',
            -- The phone itself is NOT written here. This row is rendered in the
            -- merchant console and read by whoever is debugging; the last four
            -- digits identify a customer at a counter without putting a full
            -- number into an audit feed.
            jsonb_build_object('scope', v_scope,
                               'customer_identified', v_customer_id is not null,
                               'phone_last4', v_customer->>'phone_last4'));
  end if;

  return jsonb_build_object(
    'session_id', v_sess.id,
    'resumed', v_resumed,
    'customer', v_customer,
    'context', get_session_context(v_sess.id)
  );
end $$;

-- =============================== privileges ===============================
-- MUST be last: create-or-replace re-grants EXECUTE to PUBLIC.
do $$
declare
  fn record;
  ours text[] := array[
    'ping','health_check','create_campaign','commit_campaign','get_campaign',
    'list_campaign_slots','list_merchant_campaigns','get_merchant_by_name',
    'get_session_context','get_audit_feed','reserve_slot','settle_payment',
    'release_reservation','verify_redemption','reset_demo','nuke_demo',
    'open_session_by_token','append_session_turn','log_decision','get_redemption',
    'get_payment_status','log_webhook_event','mark_webhook_processed',
    'upsert_catalog','list_catalog','upsert_shelf','list_shelves','delete_shelf',
    'get_session_audit','upsert_customer'
  ];
begin
  for fn in
    select p.oid::regprocedure as sig
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.proname = any(ours)
  loop
    execute format('revoke execute on function %s from public', fn.sig);
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke execute on function %s from anon', fn.sig);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke execute on function %s from authenticated', fn.sig);
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
      execute format('grant execute on function %s to service_role', fn.sig);
    end if;
  end loop;
end $$;
