-- Shelves, and binding a sticker to what it may discount.
--
-- Until now a slot carried a ceiling and nothing else: any sticker could be
-- used for any product, and the customer named the item in conversation. That
-- is fine for a single-aisle demo and wrong for a real shop, where a sticker
-- on the tea shelf should be about tea.
--
-- Three binding modes, chosen per campaign:
--
--   open     any active product. The original behaviour, still the default.
--   product  one sku. "10% off this atta, and only this atta."
--   shelf    a named set of skus the shopkeeper curates.
--
-- The enforcement is deliberately structural rather than a check bolted on
-- afterwards. get_session_context() returns only the products a slot is
-- allowed to sell, and every tool the agent has reads its catalog from that
-- context -- so the model is not *prevented* from quoting a forbidden item,
-- it simply never learns the item exists. accept() re-derives the same
-- filtered context server-side before reserving, so a crafted request cannot
-- reach around the model either.
--
-- NOT part of the Merkle commitment. The cryptographic promise is about the
-- ceiling: "this sticker can never exceed X%". Folding the sku into the leaf
-- preimage would strengthen it to "X% and only on tea", but it would also
-- change every hash in the system, break the Python/TypeScript parity
-- fixtures, and invalidate the campaign already committed in production.
-- Binding is operational scope; the ceiling is the promise.

-- =============================== shelves ==================================
create table if not exists shelves (
  id           uuid primary key default gen_random_uuid(),
  merchant_id  uuid not null references merchants(id) on delete cascade,
  name         text not null,
  note         text not null default '',
  created_at   timestamptz not null default now(),
  constraint uq_shelf_name unique (merchant_id, name)
);

create table if not exists shelf_items (
  shelf_id  uuid not null references shelves(id) on delete cascade,
  sku       text not null,
  primary key (shelf_id, sku)
);

alter table shelves     enable row level security;
alter table shelf_items enable row level security;

-- ========================= binding on campaigns/slots =====================
alter table campaigns add column if not exists slot_binding text not null default 'open';

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'ck_slot_binding') then
    alter table campaigns add constraint ck_slot_binding
      check (slot_binding in ('open','product','shelf'));
  end if;
end $$;

alter table slots add column if not exists bound_sku text;
alter table slots add column if not exists shelf_id uuid references shelves(id) on delete set null;

create index if not exists ix_slots_shelf on slots (shelf_id) where shelf_id is not null;

-- =============================== catalog ==================================
-- Bulk upsert behind one call, because a spreadsheet import is all-or-nothing
-- from the shopkeeper's point of view: a half-loaded catalog is worse than a
-- rejected one, and PostgREST gives each .execute() its own transaction.
create or replace function public.upsert_catalog(
  p_merchant_id uuid, p_items jsonb, p_replace boolean default false
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_seen text[]; v_count int; v_deactivated int := 0;
begin
  if not exists (select 1 from merchants where id = p_merchant_id) then
    raise exception 'MERCHANT_NOT_FOUND';
  end if;

  select array_agg(upper(trim(x.sku))) into v_seen
    from jsonb_to_recordset(p_items) as x(sku text);

  if v_seen is null or array_length(v_seen, 1) is null then
    raise exception 'NO_ITEMS';
  end if;

  insert into catalog_items (merchant_id, sku, name, unit, price_paise, cost_paise, active)
  select p_merchant_id, upper(trim(x.sku)), x.name,
         coalesce(nullif(trim(x.unit), ''), 'pc'),
         x.price_paise, x.cost_paise, true
    from jsonb_to_recordset(p_items) as x(
           sku text, name text, unit text, price_paise bigint, cost_paise bigint)
  on conflict (merchant_id, sku) do update
    set name = excluded.name, unit = excluded.unit,
        price_paise = excluded.price_paise, cost_paise = excluded.cost_paise,
        active = true;
  get diagnostics v_count = row_count;

  -- A "replace" import retires anything absent from the sheet rather than
  -- deleting it: catalog_items is referenced by shelves and by historical
  -- sessions, so deactivating is the only non-destructive option.
  if p_replace then
    update catalog_items set active = false
     where merchant_id = p_merchant_id and not (sku = any(v_seen)) and active;
    get diagnostics v_deactivated = row_count;
  end if;

  return jsonb_build_object('ok', true, 'upserted', v_count,
                            'deactivated', v_deactivated);
end $$;

create or replace function public.list_catalog(p_merchant_id uuid, p_all boolean default false)
returns jsonb
language sql stable security definer set search_path = public as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'sku', ci.sku, 'name', ci.name, 'unit', ci.unit,
           'price_paise', ci.price_paise, 'cost_paise', ci.cost_paise,
           'active', ci.active,
           -- Margin on the sale price, the convention a shopkeeper uses and
           -- the one margin_floor_bps is expressed in.
           'margin_bps', ((ci.price_paise - ci.cost_paise) * 10000 / nullif(ci.price_paise, 0))
         ) order by ci.sku), '[]'::jsonb)
  from catalog_items ci
  where ci.merchant_id = p_merchant_id and (p_all or ci.active);
$$;

-- =========================== shelf management =============================
create or replace function public.upsert_shelf(
  p_merchant_id uuid, p_name text, p_skus jsonb, p_note text default '',
  p_shelf_id uuid default null
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_id uuid;
begin
  if p_shelf_id is not null then
    update shelves set name = p_name, note = coalesce(p_note, '')
     where id = p_shelf_id and merchant_id = p_merchant_id
     returning id into v_id;
    if v_id is null then raise exception 'SHELF_NOT_FOUND'; end if;
  else
    insert into shelves (merchant_id, name, note)
    values (p_merchant_id, p_name, coalesce(p_note, ''))
    on conflict (merchant_id, name) do update set note = excluded.note
    returning id into v_id;
  end if;

  delete from shelf_items where shelf_id = v_id;
  insert into shelf_items (shelf_id, sku)
  select distinct v_id, upper(trim(s))
    from jsonb_array_elements_text(coalesce(p_skus,'[]'::jsonb)) as s
   where trim(s) <> '';

  return jsonb_build_object('ok', true, 'shelf_id', v_id);
end $$;

create or replace function public.list_shelves(p_merchant_id uuid)
returns jsonb
language sql stable security definer set search_path = public as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'id', s.id, 'name', s.name, 'note', s.note,
           'skus', (select coalesce(jsonb_agg(si.sku order by si.sku), '[]'::jsonb)
                      from shelf_items si where si.shelf_id = s.id),
           'item_count', (select count(*) from shelf_items si where si.shelf_id = s.id)
         ) order by s.name), '[]'::jsonb)
  from shelves s where s.merchant_id = p_merchant_id;
$$;

create or replace function public.delete_shelf(p_merchant_id uuid, p_shelf_id uuid)
returns jsonb
language plpgsql security definer set search_path = public as $$
begin
  delete from shelves where id = p_shelf_id and merchant_id = p_merchant_id;
  if not found then raise exception 'SHELF_NOT_FOUND'; end if;
  return jsonb_build_object('ok', true);
end $$;

-- ===================== commit, now carrying the binding ===================
create or replace function public.commit_campaign(
  p_campaign_id uuid, p_merkle_root text, p_policy_hash text,
  p_tree_size int, p_slots jsonb
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_camp campaigns%rowtype; v_n int;
begin
  select * into v_camp from campaigns where id = p_campaign_id for update;
  if not found                then raise exception 'CAMPAIGN_NOT_FOUND'; end if;
  if v_camp.status <> 'draft' then raise exception 'ALREADY_COMMITTED';  end if;

  insert into slots (campaign_id, leaf_index, slot_token, salt_hex,
                     ceiling_bps, leaf_hash, proof, bound_sku, shelf_id)
  select p_campaign_id, s.leaf_index, s.slot_token, s.salt_hex,
         s.ceiling_bps, s.leaf_hash, s.proof,
         nullif(upper(trim(coalesce(s.bound_sku, ''))), ''), s.shelf_id
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

  update campaigns
     set status='live', merkle_root=p_merkle_root, policy_hash=p_policy_hash,
         tree_size=p_tree_size, committed_at=now()
   where id = p_campaign_id;

  insert into decisions (campaign_id, kind, code, human_reason, meta)
  values (p_campaign_id, 'campaign_committed', 'C01_COMMITTED',
          format('Campaign committed: %s slots, root %s, tree size %s, binding %s.',
                 v_n, left(p_merkle_root,12), p_tree_size, v_camp.slot_binding),
          jsonb_build_object('merkle_root',p_merkle_root,'policy_hash',p_policy_hash,
                             'slot_binding',v_camp.slot_binding));

  return jsonb_build_object('ok',true,'slots',v_n,'merkle_root',p_merkle_root,
                            'tree_size',p_tree_size,'committed_at',now());
end $$;

-- ============ session context, filtered to what the slot may sell =========
-- The catalog this returns IS the agent's whole world. Filtering here rather
-- than in the application is what makes the binding structural: there is no
-- code path where a tool sees a product the slot is not scoped to.
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
                  'slot_binding',c.slot_binding),
    'merchant', jsonb_build_object('id',m.id,'name',m.name,'store_line',m.store_line),
    'catalog',  (select coalesce(jsonb_agg(jsonb_build_object(
                    'sku',ci.sku,'name',ci.name,'unit',ci.unit,
                    'price_paise',ci.price_paise,'cost_paise',ci.cost_paise)),'[]'::jsonb)
                   from catalog_items ci
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
  ours text[] := array[
    'ping','health_check','create_campaign','commit_campaign','get_campaign',
    'list_campaign_slots','list_merchant_campaigns','get_merchant_by_name',
    'get_session_context','get_audit_feed','reserve_slot','settle_payment',
    'release_reservation','verify_redemption','reset_demo','nuke_demo',
    'open_session_by_token','append_session_turn','log_decision','get_redemption',
    'get_payment_status','log_webhook_event','mark_webhook_processed',
    'upsert_catalog','list_catalog','upsert_shelf','list_shelves','delete_shelf'
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
