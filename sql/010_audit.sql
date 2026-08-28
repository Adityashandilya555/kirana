-- Conversation-level context for the audit console.
--
-- get_audit_feed returns decision rows, which is the right shape for a
-- database and the wrong one for a person: the interesting unit is one
-- shopper's negotiation, not one decision. The console groups rows into
-- threads client-side, but three things it wants are not in those rows at all
-- -- which slot the conversation was on, what that slot was scoped to, and
-- (the interesting one) which products the model was therefore never shown.
--
-- That last field is the point. Binding is enforced structurally: a bound
-- slot's get_session_context simply omits everything outside its scope, so the
-- model is not forbidden from quoting a forbidden item, it never learns the
-- item exists. That is a strong claim and until now nothing in the product
-- displayed it. `withheld_skus` is the evidence, computed the same way the
-- enforcement is.

create or replace function public.get_session_audit(p_campaign_id uuid)
returns jsonb
language sql stable security definer set search_path = public as $$
  select coalesce(jsonb_agg(x order by x->>'started_at' desc), '[]'::jsonb)
  from (
    select jsonb_build_object(
      'session_id',   se.id,
      'started_at',   se.created_at,
      'status',       se.status,
      'turn_count',   se.turn_count,
      'slot_token',   sl.slot_token,
      'ceiling_bps',  sl.ceiling_bps,
      'slot_status',  sl.status,
      'bound_sku',    sl.bound_sku,
      'shelf_name',   (select sh.name from shelves sh where sh.id = sl.shelf_id),
      'sku',          se.current_sku,
      'qty',          se.current_qty,
      'offer_bps',    se.offer_bps,
      'amount_paise', se.offer_amount_paise,

      -- What the slot's scope allowed the model to see.
      'visible_skus', (
        select coalesce(jsonb_agg(ci.sku order by ci.sku), '[]'::jsonb)
        from catalog_items ci
        where ci.merchant_id = c.merchant_id and ci.active
          and (
            sl.bound_sku is not null and ci.sku = sl.bound_sku
            or sl.bound_sku is null and sl.shelf_id is not null
               and exists (select 1 from shelf_items si
                            where si.shelf_id = sl.shelf_id and si.sku = ci.sku)
            or sl.bound_sku is null and sl.shelf_id is null
          )
      ),

      -- And its complement: the products that existed in the shop but were
      -- absent from the model's world for this conversation. Empty for an
      -- unbound slot, which is itself worth showing.
      'withheld_skus', (
        select coalesce(jsonb_agg(ci.sku order by ci.sku), '[]'::jsonb)
        from catalog_items ci
        where ci.merchant_id = c.merchant_id and ci.active
          and not (
            sl.bound_sku is not null and ci.sku = sl.bound_sku
            or sl.bound_sku is null and sl.shelf_id is not null
               and exists (select 1 from shelf_items si
                            where si.shelf_id = sl.shelf_id and si.sku = ci.sku)
            or sl.bound_sku is null and sl.shelf_id is null
          )
      )
    ) as x
    from sessions se
    join slots     sl on sl.id = se.slot_id
    join campaigns c  on c.id  = se.campaign_id
    where se.campaign_id = p_campaign_id
    order by se.created_at desc
    limit 100
  ) t;
$$;

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
    'get_session_audit'
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
