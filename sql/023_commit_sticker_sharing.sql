-- commit_campaign learns about sticker sharing, and stamps it onto the slots.
--
-- The value is denormalised from the campaign onto every slot at commit time
-- because the partial unique index on payments references payments.slot_sharing,
-- and a partial index cannot reach into another table to find it.
--
-- Default 'once' throughout, so a commit that does not mention sharing behaves
-- exactly as it always has.

drop function if exists public.commit_campaign(uuid, text, text, int, jsonb, jsonb, text, int, text, text);

create or replace function public.commit_campaign(
  p_campaign_id uuid,
  p_merkle_root text,
  p_policy_hash text,
  p_tree_size int,
  p_slots jsonb,
  p_caps jsonb default '[]'::jsonb,
  p_cap_root text default null,
  p_cap_tree_size int default null,
  p_tier_hash text default null,
  p_ceiling_mode text default 'tiered',
  p_sticker_sharing text default 'once'
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_camp campaigns%rowtype; v_n int; v_caps int := 0;
        v_sharing text := coalesce(p_sticker_sharing, 'once');
begin
  select * into v_camp from campaigns where id = p_campaign_id for update;
  if not found                then raise exception 'CAMPAIGN_NOT_FOUND'; end if;
  if v_camp.status <> 'draft' then raise exception 'ALREADY_COMMITTED';  end if;
  if v_sharing not in ('once','shared') then raise exception 'BAD_STICKER_SHARING'; end if;

  insert into slots (campaign_id, leaf_index, slot_token, salt_hex,
                     ceiling_bps, leaf_hash, proof, bound_sku, shelf_id, sharing)
  select p_campaign_id, s.leaf_index, s.slot_token, s.salt_hex,
         s.ceiling_bps, s.leaf_hash, s.proof,
         nullif(upper(trim(coalesce(s.bound_sku, ''))), ''), s.shelf_id, v_sharing
    from jsonb_to_recordset(p_slots) as s(
           leaf_index int, slot_token text, salt_hex text,
           ceiling_bps int, leaf_hash text, proof jsonb,
           bound_sku text, shelf_id uuid);
  get diagnostics v_n = row_count;

  if v_n <> v_camp.slot_count then raise exception 'SLOT_COUNT_MISMATCH'; end if;
  if exists (select 1 from slots
              where campaign_id = p_campaign_id
                and ceiling_bps > v_camp.max_discount_bps)
    then raise exception 'CEILING_ABOVE_CAMPAIGN_MAX'; end if;

  if p_caps is not null and jsonb_array_length(p_caps) > 0 then
    insert into campaign_product_caps (campaign_id, row_index, sku, cap_bps,
                                       price_paise, cost_paise,
                                       margin_floor_bps, salt_hex,
                                       leaf_hash, proof)
    select p_campaign_id, c.row_index, upper(trim(c.sku)), c.cap_bps,
           c.price_paise, c.cost_paise, c.margin_floor_bps, c.salt_hex,
           c.leaf_hash, c.proof
      from jsonb_to_recordset(p_caps) as c(
             row_index int, sku text, cap_bps int,
             price_paise bigint, cost_paise bigint, margin_floor_bps int,
             salt_hex text, leaf_hash text, proof jsonb);
    get diagnostics v_caps = row_count;

    if exists (select 1 from campaign_product_caps
                where campaign_id = p_campaign_id
                  and cap_bps > v_camp.max_discount_bps)
      then raise exception 'CAP_ABOVE_CAMPAIGN_MAX'; end if;
  end if;

  if p_ceiling_mode = 'margin' and (p_cap_root is null or v_caps = 0) then
    raise exception 'CAPS_REQUIRED_FOR_MARGIN_MODE';
  end if;

  update campaigns
     set status='live', merkle_root=p_merkle_root, policy_hash=p_policy_hash,
         tree_size=p_tree_size, committed_at=now(),
         cap_root=p_cap_root, cap_tree_size=p_cap_tree_size,
         tier_hash=p_tier_hash,
         ceiling_mode=coalesce(p_ceiling_mode, 'tiered'),
         sticker_sharing=v_sharing
   where id = p_campaign_id;

  insert into decisions (campaign_id, kind, code, human_reason, meta)
  values (p_campaign_id, 'campaign_committed', 'C01_COMMITTED',
          format('Campaign committed: %s slots, root %s, tree size %s, binding %s, mode %s, stickers %s, %s product caps%s.',
                 v_n, left(p_merkle_root,12), p_tree_size, v_camp.slot_binding,
                 coalesce(p_ceiling_mode,'tiered'), v_sharing, v_caps,
                 case when p_cap_root is null then ''
                      else format(', cap root %s', left(p_cap_root,12)) end),
          jsonb_build_object('merkle_root',p_merkle_root,'policy_hash',p_policy_hash,
                             'slot_binding',v_camp.slot_binding,
                             'ceiling_mode',coalesce(p_ceiling_mode,'tiered'),
                             'sticker_sharing',v_sharing,
                             'cap_root',p_cap_root,'cap_tree_size',p_cap_tree_size,
                             'tier_hash',p_tier_hash,'cap_count',v_caps));

  return jsonb_build_object('ok',true,'slots',v_n,'merkle_root',p_merkle_root,
                            'tree_size',p_tree_size,'committed_at',now(),
                            'caps',v_caps,'cap_root',p_cap_root,
                            'ceiling_mode',coalesce(p_ceiling_mode,'tiered'),
                            'sticker_sharing',v_sharing);
end $$;

-- =============================== privileges ===============================
do $$
declare
  fn record;
  ours text[] := array['commit_campaign'];
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
