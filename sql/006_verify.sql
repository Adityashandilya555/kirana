-- Read-only redemption lookup for the customer's own screen.
--
-- verify_redemption() BURNS the code: the first call sets verified_at and
-- every later call comes back red. That is correct for the merchant scanning
-- at the counter, and completely wrong for the customer's own phone, which
-- renders the redemption QR and may be reloaded any number of times before
-- anyone scans it.
--
-- So this is a separate function rather than a flag on the existing one. A
-- boolean parameter that decides whether an irreversible thing happens is the
-- kind of API that eventually gets called with the wrong value.
--
-- It returns the opened commitment -- salt, leaf hash, proof, root -- because
-- the customer's browser replays the inclusion proof itself with the TS twin
-- of merkle.py. Revealing the salt here is deliberate: the commitment is
-- hiding only until redemption, and by this point this slot has been paid.

create or replace function public.get_redemption(p_redemption_token text)
returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'slot_token',         sl.slot_token,
    'leaf_index',         sl.leaf_index,
    'salt_hex',           sl.salt_hex,
    'ceiling_bps',        sl.ceiling_bps,
    'granted_bps',        sl.granted_bps,
    'discount_paise',     sl.discount_paise,
    'leaf_hash',          sl.leaf_hash,
    'proof',              sl.proof,
    'redeemed_at',        sl.redeemed_at,
    'verified_at',        sl.verified_at,
    'store',              m.name,
    'store_line',         m.store_line,
    'campaign_id',        c.id,
    'campaign_name',      c.name,
    'merkle_root',        c.merkle_root,
    'policy_hash',        c.policy_hash,
    'tree_size',          c.tree_size,
    'committed_at',       c.committed_at,
    'sku',                se.current_sku,
    'qty',                se.current_qty,
    'final_amount_paise', se.offer_amount_paise
  )
  from slots sl
  join campaigns c on c.id = sl.campaign_id
  join merchants m on m.id = c.merchant_id
  left join lateral (
    select * from sessions s
     where s.slot_id = sl.id and s.status = 'paid'
     order by s.created_at desc limit 1
  ) se on true
  where sl.redemption_token = p_redemption_token;
$$;

-- =============================== privileges ===============================
-- MUST be last: `create or replace` above re-granted EXECUTE to PUBLIC.
do $$
declare
  fn record;
  ours text[] := array[
    'ping','health_check','create_campaign','commit_campaign','get_campaign',
    'list_campaign_slots','list_merchant_campaigns','get_merchant_by_name',
    'get_session_context','get_audit_feed','reserve_slot','settle_payment',
    'release_reservation','verify_redemption','reset_demo','nuke_demo',
    'open_session_by_token','append_session_turn','log_decision','get_redemption'
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
