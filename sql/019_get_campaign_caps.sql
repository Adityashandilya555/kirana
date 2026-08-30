-- get_campaign, told about the columns the last two migrations added.
--
-- It builds an explicit jsonb_build_object rather than to_jsonb(c), which is
-- the right choice -- it is the allowlist that keeps internal columns out of a
-- merchant response -- but it means every new column is invisible until it is
-- named here. Two were:
--
--   * the tier rule (017). The console could SET it and then had no way to
--     read it back, so a shopkeeper could not see the rule they had configured.
--   * the cap commitment (018). commit_campaign writes cap_root and tier_hash,
--     and campaign_service.commit_campaign reads tier_min_txn_count et al back
--     out of get_campaign to compute tier_hash -- so without this it hashes the
--     DEFAULT rule rather than the real one, and the committed hash would not
--     describe the campaign it is attached to. That is the bug this fixes, and
--     it is silent: the hash is well-formed, just wrong.

create or replace function public.get_campaign(p_campaign_id uuid)
returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'id', c.id, 'name', c.name, 'status', c.status,
    'merchant', jsonb_build_object('id', m.id, 'name', m.name, 'store_line', m.store_line),
    'budget_paise', c.budget_paise, 'spent_paise', c.spent_paise,
    'reserved_paise', c.reserved_paise,
    'remaining_paise', c.budget_paise - c.spent_paise - c.reserved_paise,
    'max_discount_bps', c.max_discount_bps, 'margin_floor_bps', c.margin_floor_bps,
    'max_turns', c.max_turns, 'slot_count', c.slot_count,
    'slot_binding', c.slot_binding,
    'merkle_root', c.merkle_root, 'policy_hash', c.policy_hash,
    'tree_size', c.tree_size, 'committed_at', c.committed_at,
    'created_at', c.created_at,
    -- Who qualifies as a regular, and what everyone else gets.
    'tier_min_txn_count', c.tier_min_txn_count,
    'tier_min_spend_paise', c.tier_min_spend_paise,
    'tier_window_days', c.tier_window_days,
    'base_cap_fraction_bps', c.base_cap_fraction_bps,
    -- The per-product commitment. NULL on every campaign committed before
    -- caps existed, which the console must render as "this campaign predates
    -- per-product caps" rather than as a failed proof.
    'ceiling_mode', c.ceiling_mode,
    'cap_root', c.cap_root,
    'cap_tree_size', c.cap_tree_size,
    'tier_hash', c.tier_hash,
    'caps_total', (select count(*) from campaign_product_caps p
                    where p.campaign_id = c.id),
    'slots_total',    (select count(*) from slots s where s.campaign_id = c.id),
    'slots_redeemed', (select count(*) from slots s where s.campaign_id = c.id and s.status = 'redeemed'),
    'slots_verified', (select count(*) from slots s where s.campaign_id = c.id and s.verified_at is not null),
    'scopes', (
      select coalesce(jsonb_agg(x), '[]'::jsonb) from (
        select jsonb_build_object(
                 'bound_sku', s.bound_sku,
                 'shelf_id', s.shelf_id,
                 'shelf_name', (select sh.name from shelves sh where sh.id = s.shelf_id),
                 'slots', count(*)
               ) as x
          from slots s
         where s.campaign_id = c.id and (s.bound_sku is not null or s.shelf_id is not null)
         group by s.bound_sku, s.shelf_id
      ) t
    )
  )
  from campaigns c join merchants m on m.id = c.merchant_id
  where c.id = p_campaign_id;
$$;

-- =============================== privileges ===============================
do $$
declare
  fn record;
  ours text[] := array['get_campaign'];
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
