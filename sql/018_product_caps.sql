-- Per-product ceilings, committed.
--
-- Until now the only committed number was per STICKER: "this code can never
-- exceed X%". True and checkable, but product-agnostic -- the same sticker
-- allowed the same percentage on sugar, whose margin is thin, as on tea, whose
-- margin is not. The number that actually protects a shopkeeper is per
-- PRODUCT, and it was computed fresh on every single proposal by a binary
-- search in bounds.max_discount_for_margin() and then thrown away.
--
-- Now it is computed once at commit, stored, and committed to its own Merkle
-- root. The formula is unchanged: it comes from simulate.item_headroom(), the
-- same function that has always drawn the preview a shopkeeper reads before
-- pressing commit. The number they were shown and the number the gate will
-- enforce are now provably the same number, because there is only one of them.
--
-- A SECOND TREE, NOT A WIDER LEAF. Folding sku or cap into the slot leaf
-- preimage would change every hash in the system: it breaks the
-- Python/TypeScript parity fixture and invalidates the campaign already
-- committed in production, whose QR sheet is a physical object on a table.
-- sql/009_shelves.sql made this exact argument when bound_sku was added and
-- declined for the same reason. The slot leaf is untouched here.
--
-- NOTHING IS ENFORCED YET. The caps are written and provable; bounds.check()
-- does not read them. That is the next change, kept separate so this one can
-- be reverted without unpricing anything.

create table if not exists campaign_product_caps (
  campaign_id      uuid not null references campaigns(id) on delete cascade,
  row_index        int  not null check (row_index >= 0),
  sku              text not null,
  cap_bps          int  not null check (cap_bps between 0 and 10000),
  -- The inputs the cap was derived from, frozen alongside it. NOT in the leaf
  -- preimage: leaves get opened at redemption, and opening one would publish
  -- item cost to every shopper who verifies a code. Stored so a merchant can
  -- show an auditor the derivation without showing it to a customer.
  price_paise      bigint not null,
  cost_paise       bigint not null,
  margin_floor_bps int not null,
  salt_hex         text not null,
  leaf_hash        text not null,
  proof            jsonb not null default '[]'::jsonb,
  created_at       timestamptz not null default now(),
  primary key (campaign_id, sku),
  constraint uq_cap_row unique (campaign_id, row_index)
);
create index if not exists ix_caps_campaign on campaign_product_caps (campaign_id);
alter table campaign_product_caps enable row level security;

alter table campaigns
  -- 'tiered' is the old world: per-sticker ceilings from plan_ceilings, no
  -- product caps. Defaulting to it is what leaves the live campaign, every
  -- existing test, and plan_ceilings itself completely untouched. Only
  -- campaigns committed through the new path get 'margin'.
  add column if not exists ceiling_mode text not null default 'tiered'
    check (ceiling_mode in ('tiered','margin')),
  add column if not exists cap_root text,
  add column if not exists cap_tree_size int,
  add column if not exists tier_hash text;

-- ------------------------------------------------------------------------
-- commit_campaign, now writing three commitments instead of one.
--
-- DROP FIRST. Adding parameters via create-or-replace makes an OVERLOAD, and
-- PostgREST then answers 300 Multiple Choices -- every commit would fail.
-- ------------------------------------------------------------------------
drop function if exists public.commit_campaign(uuid, text, text, int, jsonb);

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
  p_ceiling_mode text default 'tiered'
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_camp campaigns%rowtype; v_n int; v_caps int := 0;
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

  -- Caps are optional so a 'tiered' commit is byte-identical to before.
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

    -- The same guarantee the slot tree has, one level down: a cap above the
    -- campaign maximum would be a promise the envelope does not contain.
    if exists (select 1 from campaign_product_caps
                where campaign_id = p_campaign_id
                  and cap_bps > v_camp.max_discount_bps)
      then raise exception 'CAP_ABOVE_CAMPAIGN_MAX'; end if;
  end if;

  -- Margin mode without a cap root would be a campaign claiming to price by
  -- product with nothing committed to prove it.
  if p_ceiling_mode = 'margin' and (p_cap_root is null or v_caps = 0) then
    raise exception 'CAPS_REQUIRED_FOR_MARGIN_MODE';
  end if;

  update campaigns
     set status='live', merkle_root=p_merkle_root, policy_hash=p_policy_hash,
         tree_size=p_tree_size, committed_at=now(),
         cap_root=p_cap_root, cap_tree_size=p_cap_tree_size,
         tier_hash=p_tier_hash,
         ceiling_mode=coalesce(p_ceiling_mode, 'tiered')
   where id = p_campaign_id;

  insert into decisions (campaign_id, kind, code, human_reason, meta)
  values (p_campaign_id, 'campaign_committed', 'C01_COMMITTED',
          format('Campaign committed: %s slots, root %s, tree size %s, binding %s, mode %s, %s product caps%s.',
                 v_n, left(p_merkle_root,12), p_tree_size, v_camp.slot_binding,
                 coalesce(p_ceiling_mode,'tiered'), v_caps,
                 case when p_cap_root is null then ''
                      else format(', cap root %s', left(p_cap_root,12)) end),
          jsonb_build_object('merkle_root',p_merkle_root,'policy_hash',p_policy_hash,
                             'slot_binding',v_camp.slot_binding,
                             'ceiling_mode',coalesce(p_ceiling_mode,'tiered'),
                             'cap_root',p_cap_root,'cap_tree_size',p_cap_tree_size,
                             'tier_hash',p_tier_hash,'cap_count',v_caps));

  return jsonb_build_object('ok',true,'slots',v_n,'merkle_root',p_merkle_root,
                            'tree_size',p_tree_size,'committed_at',now(),
                            'caps',v_caps,'cap_root',p_cap_root,
                            'ceiling_mode',coalesce(p_ceiling_mode,'tiered'));
end $$;

-- ------------------------------------------------------------------------
-- list_campaign_caps -- what each product was committed to, with its proof.
--
-- The proof is included so the console and a customer's phone can replay the
-- walk to cap_root without a second round trip. salt_hex is included for the
-- same reason: a proof cannot be checked without the leaf's preimage.
-- cost_paise is NOT returned. It is stored, but opening a leaf must not
-- publish what the shop pays for its stock.
-- ------------------------------------------------------------------------
create or replace function public.list_campaign_caps(p_campaign_id uuid)
returns jsonb
language sql stable security definer set search_path = public as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'row_index', c.row_index, 'sku', c.sku, 'cap_bps', c.cap_bps,
           'price_paise', c.price_paise, 'margin_floor_bps', c.margin_floor_bps,
           'salt_hex', c.salt_hex, 'leaf_hash', c.leaf_hash, 'proof', c.proof
         ) order by c.row_index), '[]'::jsonb)
    from campaign_product_caps c where c.campaign_id = p_campaign_id;
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
    'get_session_audit','upsert_customer','get_customer_stats','set_campaign_tier',
    'list_campaign_caps'
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
