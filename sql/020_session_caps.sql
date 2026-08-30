-- Carry each product's committed cap into the session context.
--
-- get_session_context builds the catalogue the gate prices against. It already
-- carries price_paise and cost_paise, because bounds.check() needs both to
-- work out the margin ceiling live. It now also carries the cap that was
-- COMMITTED for that sku at commit time, so the gate can enforce the number a
-- customer can actually prove rather than one recomputed on the fly.
--
-- LEFT JOIN, and cap_bps is null for a campaign committed before caps existed.
-- Null means "not applied", not "zero" -- treating an absent cap as a cap of
-- zero would make every legacy campaign refuse every discount.
--
-- cost_paise is still returned and still must never leave the server; the
-- allowlist in app/api/routes/session.py:public_context is what enforces that,
-- and cap_bps is deliberately not added to it either. A shopper who learns the
-- product cap has learned the ceiling.

create or replace function public.get_session_context(p_session_id uuid)
returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'session',    to_jsonb(se) - 'transcript',
    'transcript', se.transcript,
    'slot',     jsonb_build_object('id',sl.id,'slot_token',sl.slot_token,
                  'status',sl.status,'ceiling_bps',sl.ceiling_bps,
                  'leaf_index',sl.leaf_index,
                  'bound_sku',sl.bound_sku,'shelf_id',sl.shelf_id,
                  'shelf_name',(select sh.name from shelves sh where sh.id = sl.shelf_id)),
    'campaign', jsonb_build_object('id',c.id,'name',c.name,'status',c.status,
                  'budget_paise',c.budget_paise,'spent_paise',c.spent_paise,
                  'reserved_paise',c.reserved_paise,
                  'max_discount_bps',c.max_discount_bps,
                  'margin_floor_bps',c.margin_floor_bps,'max_turns',c.max_turns,
                  'slot_binding',c.slot_binding,
                  'ceiling_mode',c.ceiling_mode,
                  'base_cap_fraction_bps',c.base_cap_fraction_bps),
    'merchant', jsonb_build_object('id',m.id,'name',m.name,'store_line',m.store_line),
    'catalog',  (select coalesce(jsonb_agg(jsonb_build_object(
                    'sku',ci.sku,'name',ci.name,'unit',ci.unit,
                    'price_paise',ci.price_paise,'cost_paise',ci.cost_paise,
                    'cap_bps',pc.cap_bps)),'[]'::jsonb)
                   from catalog_items ci
                   left join campaign_product_caps pc
                          on pc.campaign_id = c.id and pc.sku = ci.sku
                  where ci.merchant_id = m.id and ci.active
                    and (
                      sl.bound_sku is not null and ci.sku = sl.bound_sku
                      or sl.bound_sku is null and sl.shelf_id is not null
                         and exists (select 1 from shelf_items si
                                      where si.shelf_id = sl.shelf_id and si.sku = ci.sku)
                      or sl.bound_sku is null and sl.shelf_id is null
                    ))
  )
  from sessions se
  join slots     sl on sl.id = se.slot_id
  join campaigns c  on c.id  = se.campaign_id
  join merchants m  on m.id  = c.merchant_id
  where se.id = p_session_id;
$$;

-- =============================== privileges ===============================
do $$
declare
  fn record;
  ours text[] := array['get_session_context'];
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
