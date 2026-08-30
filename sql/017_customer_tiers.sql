-- Who counts as a regular, and by how much.
--
-- Two bands, deliberately. Not bronze/silver/gold: a shopkeeper has to be able
-- to explain the rule across a counter in one sentence, and a customer has to
-- be able to hear it and know whether it applies to them. "Ten visits or five
-- thousand rupees in the last month" is that sentence. Three overlapping
-- thresholds is not.
--
-- Four columns rather than a tiers table, for the same reason: with exactly two
-- bands and one dial, two rows is not a table.
--
--   qualifies  -> may reach the full per-product ceiling
--   everyone   -> capped at base_cap_fraction_bps of it
--
-- base_cap_fraction_bps defaults to 10000 -- "no reduction" -- so every
-- campaign that exists today keeps behaving exactly as it does now, with no
-- branch anywhere and nothing to backfill.
--
-- NOTHING IN THIS MIGRATION MOVES A PRICE. The tier is evaluated, recorded and
-- shown; bounds.check() does not read it yet. That happens in a later change,
-- deliberately, so the rule can be watched against real shoppers before it is
-- allowed to cost anyone money.

alter table campaigns
  add column if not exists tier_min_txn_count int not null default 0
    check (tier_min_txn_count >= 0),
  add column if not exists tier_min_spend_paise bigint not null default 0
    check (tier_min_spend_paise >= 0),
  -- NULL means lifetime. A window in days rather than an enum because "three
  -- weeks" and "last month" are just 21 and 30, and a shopkeeper who wants 45
  -- should not need a migration.
  add column if not exists tier_window_days int
    check (tier_window_days is null or tier_window_days between 1 and 3650),
  add column if not exists base_cap_fraction_bps int not null default 10000
    check (base_cap_fraction_bps between 0 and 10000);

-- The snapshot. Written once when the session opens and never recomputed.
--
-- Same argument migration 015 makes for scope_snapshot: a tier that can flip
-- mid-negotiation, or that is reconstructed afterwards from a payment history
-- that has since moved, is not evidence. The shopper is told what they are
-- entitled to at the start of the conversation and that has to stay true for
-- its duration.
alter table sessions
  add column if not exists tier_key text
    check (tier_key is null or tier_key in ('new','preferred')),
  -- The one number a later change needs: what fraction of each product's
  -- ceiling this shopper may reach. Snapshotted so it cannot drift.
  add column if not exists tier_cap_fraction_bps int
    check (tier_cap_fraction_bps is null
           or tier_cap_fraction_bps between 0 and 10000),
  add column if not exists tier_stats jsonb,
  add column if not exists tier_evaluated_at timestamptz;

-- ------------------------------------------------------------------------
-- get_customer_stats -- what this shopper has actually done here.
--
-- Joins payments through sessions rather than reading a customer_id off
-- payments, because payments does not carry one yet and does not need to: the
-- session already knows who it belonged to. A denormalised column on payments
-- arrives later, when a partial unique index needs to reference it.
--
-- Counts only captured payments. A reservation that was released is not a
-- purchase, and treating it as one would let a shopper earn regular status by
-- opening checkouts and abandoning them.
-- ------------------------------------------------------------------------
create or replace function public.get_customer_stats(
  p_customer_id uuid, p_window_days int default null
) returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'txn_count',   coalesce(count(*), 0),
    'spend_paise', coalesce(sum(p.amount_paise), 0),
    'window_days', p_window_days,
    'first_purchase_at', min(p.created_at),
    'last_purchase_at',  max(p.created_at)
  )
  from payments p
  join sessions s on s.id = p.session_id
 where s.customer_id = p_customer_id
   and p.status = 'captured'
   and (p_window_days is null
        or p.created_at >= now() - make_interval(days => p_window_days));
$$;

-- ------------------------------------------------------------------------
-- set_campaign_tier -- the merchant's rule, editable only before commit.
--
-- Draft-only for the same reason ceilings are: once stickers are printed the
-- promise is fixed. Changing who qualifies after the fact would silently
-- re-price codes that are already on shelves.
--
-- A separate function rather than four more parameters on create_campaign,
-- because changing that signature would create a PostgREST overload and break
-- every campaign creation until the old one was dropped.
-- ------------------------------------------------------------------------
create or replace function public.set_campaign_tier(
  p_campaign_id uuid,
  p_min_txn_count int,
  p_min_spend_paise bigint,
  p_window_days int,
  p_base_cap_fraction_bps int
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_camp campaigns%rowtype;
begin
  select * into v_camp from campaigns where id = p_campaign_id for update;
  if not found then raise exception 'CAMPAIGN_NOT_FOUND'; end if;
  if v_camp.status <> 'draft' then raise exception 'ALREADY_COMMITTED'; end if;

  update campaigns
     set tier_min_txn_count    = greatest(coalesce(p_min_txn_count, 0), 0),
         tier_min_spend_paise  = greatest(coalesce(p_min_spend_paise, 0), 0),
         tier_window_days      = p_window_days,
         base_cap_fraction_bps = least(greatest(
             coalesce(p_base_cap_fraction_bps, 10000), 0), 10000)
   where id = p_campaign_id
  returning * into v_camp;

  return jsonb_build_object(
    'campaign_id', v_camp.id,
    'tier_min_txn_count', v_camp.tier_min_txn_count,
    'tier_min_spend_paise', v_camp.tier_min_spend_paise,
    'tier_window_days', v_camp.tier_window_days,
    'base_cap_fraction_bps', v_camp.base_cap_fraction_bps
  );
end $$;

-- ------------------------------------------------------------------------
-- open_session_by_token, now evaluating the band at the door.
--
-- DROP FIRST: create-or-replace with a changed parameter list makes an
-- overload, and PostgREST then answers 300 Multiple Choices on every scan.
-- The signature is unchanged here, but the drop is kept as the house rule.
--
-- The rule itself is two comparisons, applied in SQL because that is where the
-- snapshot is written and a second implementation is a second thing to drift.
-- Python mirrors it only for the merchant-side preview ("how many of your
-- shoppers would qualify?"), which is a different question and never decides
-- anyone's price.
-- ------------------------------------------------------------------------
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
        v_stats jsonb; v_tier text; v_fraction int;
begin
  select * into v_slot from slots where slot_token = upper(trim(p_slot_token));
  if not found then raise exception 'SLOT_NOT_FOUND'; end if;

  select * into v_camp from campaigns where id = v_slot.campaign_id;
  if v_camp.status <> 'live' then raise exception 'CAMPAIGN_NOT_LIVE'; end if;

  if p_phone_e164 is not null and p_phone_e164 <> '' then
    v_customer := upsert_customer(v_camp.merchant_id, p_phone_e164);
    v_customer_id := (v_customer->>'id')::uuid;
  end if;

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

    -- An unidentified shopper cannot be a regular. That is not a punishment,
    -- it is the only honest answer: there is no history to read.
    if v_customer_id is null then
      v_stats := jsonb_build_object('txn_count', 0, 'spend_paise', 0,
                                    'window_days', v_camp.tier_window_days,
                                    'identified', false);
      v_tier := 'new';
    else
      v_stats := get_customer_stats(v_customer_id, v_camp.tier_window_days)
                 || jsonb_build_object('identified', true);
      v_tier := case
        when (v_stats->>'txn_count')::int   >= v_camp.tier_min_txn_count
         and (v_stats->>'spend_paise')::bigint >= v_camp.tier_min_spend_paise
        then 'preferred' else 'new' end;
    end if;

    v_fraction := case when v_tier = 'preferred'
                       then 10000 else v_camp.base_cap_fraction_bps end;

    insert into sessions (slot_id, campaign_id, transport, transport_ref,
                          scope_snapshot, customer_id,
                          tier_key, tier_cap_fraction_bps, tier_stats,
                          tier_evaluated_at)
    values (v_slot.id, v_slot.campaign_id, p_transport, p_transport_ref,
            v_scope, v_customer_id, v_tier, v_fraction, v_stats, now())
    returning * into v_sess;

    update slots set status = 'offered'
     where id = v_slot.id and status = 'unused';

    insert into decisions (campaign_id, slot_id, session_id, kind, code,
                           human_reason, customer_reason, meta)
    values (v_camp.id, v_slot.id, v_sess.id, 'session_opened', 'C02_SESSION_OPENED',
            format('Session opened on slot %s (ceiling %s bps, leaf %s). Scope: %s visible, %s withheld. Customer %s, band %s (%s visits, %s spent).',
                   v_slot.slot_token, v_slot.ceiling_bps, v_slot.leaf_index,
                   jsonb_array_length(v_scope->'visible'),
                   jsonb_array_length(v_scope->'withheld'),
                   case when v_customer_id is null then 'not identified'
                        else format('...%s', v_customer->>'phone_last4') end,
                   v_tier, v_stats->>'txn_count',
                   ((v_stats->>'spend_paise')::bigint / 100.0)::numeric(12,2)),
            'Welcome! Ask me about anything on the shelf.',
            jsonb_build_object('scope', v_scope,
                               'customer_identified', v_customer_id is not null,
                               'phone_last4', v_customer->>'phone_last4',
                               'tier_key', v_tier,
                               'tier_cap_fraction_bps', v_fraction,
                               'tier_stats', v_stats));
  end if;

  return jsonb_build_object(
    'session_id', v_sess.id,
    'resumed', v_resumed,
    'customer', v_customer,
    'tier', jsonb_build_object(
      'key', v_sess.tier_key,
      -- The fraction is NOT returned to the phone. It is a ceiling multiplier,
      -- and a shopper who knows it and can observe one granted number recovers
      -- the product's cap by division.
      'stats', v_sess.tier_stats
    ),
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
    'get_session_audit','upsert_customer','get_customer_stats','set_campaign_tier'
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
