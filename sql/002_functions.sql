-- Every atomicity guarantee in the system lives in this file.
-- supabase-py cannot span a transaction across calls, so anything that must
-- be all-or-nothing is one function invoked via rpc().

-- --------------------------------------------------------------- ping ----
create or replace function public.ping() returns text
language sql stable as $$ select 'pong'::text $$;

-- ---------------------------------------------------- commit_campaign ----
-- Irreversible. Inserts every slot and freezes the Merkle root in one shot.
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
                     ceiling_bps, leaf_hash, proof)
  select p_campaign_id, s.leaf_index, s.slot_token, s.salt_hex,
         s.ceiling_bps, s.leaf_hash, s.proof
    from jsonb_to_recordset(p_slots) as s(
           leaf_index int, slot_token text, salt_hex text,
           ceiling_bps int, leaf_hash text, proof jsonb);
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
          format('Campaign committed: %s slots, root %s, tree size %s.',
                 v_n, left(p_merkle_root,12), p_tree_size),
          jsonb_build_object('merkle_root',p_merkle_root,'policy_hash',p_policy_hash));

  return jsonb_build_object('ok',true,'slots',v_n,'merkle_root',p_merkle_root,
                            'tree_size',p_tree_size,'committed_at',now());
end $$;

-- ------------------------------------------------- get_session_context ----
-- One round trip for everything a chat turn needs. Note cost_paise IS
-- included here -- bounds.check() needs it for the margin floor. It is the
-- caller's job never to put it in a prompt.
create or replace function public.get_session_context(p_session_id uuid)
returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'session',    to_jsonb(se) - 'transcript',
    'transcript', se.transcript,
    'slot',     jsonb_build_object('id',sl.id,'slot_token',sl.slot_token,
                  'status',sl.status,'ceiling_bps',sl.ceiling_bps,
                  'leaf_index',sl.leaf_index),
    'campaign', jsonb_build_object('id',c.id,'name',c.name,'status',c.status,
                  'budget_paise',c.budget_paise,'spent_paise',c.spent_paise,
                  'reserved_paise',c.reserved_paise,
                  'max_discount_bps',c.max_discount_bps,
                  'margin_floor_bps',c.margin_floor_bps,'max_turns',c.max_turns),
    'merchant', jsonb_build_object('id',m.id,'name',m.name,'store_line',m.store_line),
    'catalog',  (select coalesce(jsonb_agg(jsonb_build_object(
                    'sku',ci.sku,'name',ci.name,'unit',ci.unit,
                    'price_paise',ci.price_paise,'cost_paise',ci.cost_paise)),'[]'::jsonb)
                   from catalog_items ci
                  where ci.merchant_id = m.id and ci.active)
  )
  from sessions se
  join slots     sl on sl.id = se.slot_id
  join campaigns c  on c.id  = se.campaign_id
  join merchants m  on m.id  = c.merchant_id
  where se.id = p_session_id;
$$;

-- -------------------------------------------------------- reserve_slot ----
-- Called when the customer accepts. Re-checks every bound server-side; the
-- model's earlier approval is NOT trusted here.
create or replace function public.reserve_slot(
  p_session_id uuid, p_sku text, p_qty int, p_discount_bps int,
  p_discount_paise bigint, p_amount_paise bigint, p_rzp_order_id text
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_sess sessions%rowtype; v_slot slots%rowtype; v_camp campaigns%rowtype;
begin
  select * into v_sess from sessions where id = p_session_id for update;
  if not found              then raise exception 'SESSION_NOT_FOUND';    end if;
  if v_sess.status = 'paid' then raise exception 'SESSION_ALREADY_PAID'; end if;

  select * into v_slot from slots where id = v_sess.slot_id for update;
  if v_slot.status = 'redeemed' then raise exception 'SLOT_ALREADY_REDEEMED'; end if;
  if v_slot.status = 'void'     then raise exception 'SLOT_VOID';             end if;
  if p_discount_bps > v_slot.ceiling_bps then raise exception 'CEILING_VIOLATION'; end if;
  if p_amount_paise < 100 then raise exception 'BELOW_MIN_ORDER_AMOUNT'; end if;

  select * into v_camp from campaigns where id = v_sess.campaign_id for update;
  if v_camp.status <> 'live' then raise exception 'CAMPAIGN_NOT_LIVE'; end if;
  if p_discount_bps > v_camp.max_discount_bps
    then raise exception 'CAMPAIGN_MAX_VIOLATION'; end if;
  if v_camp.spent_paise + v_camp.reserved_paise
       - v_slot.reserved_paise + p_discount_paise > v_camp.budget_paise
    then raise exception 'BUDGET_EXCEEDED'; end if;

  update campaigns
     set reserved_paise = reserved_paise - v_slot.reserved_paise + p_discount_paise
   where id = v_camp.id;

  update slots
     set status='locked', granted_bps=p_discount_bps, discount_paise=p_discount_paise,
         reserved_paise=p_discount_paise, locked_at=now()
   where id = v_slot.id;

  update sessions
     set status='offer_locked', current_sku=p_sku, current_qty=p_qty,
         offer_bps=p_discount_bps, offer_discount_paise=p_discount_paise,
         offer_amount_paise=p_amount_paise, rzp_order_id=p_rzp_order_id,
         updated_at=now()
   where id = p_session_id;

  insert into decisions (campaign_id, slot_id, session_id, kind, code,
                         granted_bps, human_reason, customer_reason, meta)
  values (v_camp.id, v_slot.id, p_session_id, 'order_created', 'P01_ORDER_CREATED',
          p_discount_bps,
          format('Order %s created: %s x%s at %s bps off, %s payable.',
                 p_rzp_order_id, p_sku, p_qty, p_discount_bps,
                 (p_amount_paise/100.0)::numeric(12,2)),
          'Offer locked. Opening checkout.',
          jsonb_build_object('rzp_order_id',p_rzp_order_id));

  return jsonb_build_object('ok',true,'slot_id',v_slot.id,'campaign_id',v_camp.id,
                            'reserved_paise',p_discount_paise);
end $$;

-- ------------------------------------------------------ settle_payment ----
-- THE atomic settlement. All three paths -- checkout handler, webhook, and
-- polling -- call exactly this. Idempotent on rzp_payment_id.
create or replace function public.settle_payment(
  p_rzp_order_id text, p_rzp_payment_id text, p_signature text,
  p_amount_paise bigint, p_source text
) returns jsonb
-- search_path includes `extensions` because this is the one function that
-- calls gen_random_bytes(). Supabase installs pgcrypto into `extensions`,
-- not public, so `set search_path = public` alone cannot see it and every
-- settlement fails with "function gen_random_bytes(integer) does not exist".
-- gen_random_uuid() masked this: it is also a pg_catalog builtin, so the
-- table defaults resolved fine while the token mint did not. A bare local
-- Postgres has no `extensions` schema and Postgres ignores missing schemas
-- in search_path, so this is safe in both places.
language plpgsql security definer set search_path = public, extensions as $$
declare v_sess sessions%rowtype; v_slot slots%rowtype;
        v_camp campaigns%rowtype; v_pay payments%rowtype; v_token text;
begin
  if p_source not in ('checkout_handler','webhook','poll','manual')
    then raise exception 'BAD_SOURCE'; end if;

  select * into v_sess from sessions where rzp_order_id = p_rzp_order_id;
  if not found then raise exception 'ORDER_NOT_FOUND'; end if;

  -- Every settlement path for this slot serialises on this one row lock.
  select * into v_slot from slots where id = v_sess.slot_id for update;

  -- READ COMMITTED gives a fresh snapshot once the lock is acquired, so the
  -- loser of a race sees the winner's committed row and returns it verbatim.
  select * into v_pay from payments where rzp_payment_id = p_rzp_payment_id;
  if found then
    return jsonb_build_object('settled',true,'already',true,
      'redemption_token',v_slot.redemption_token,'slot_id',v_slot.id,
      'session_id',v_sess.id,'campaign_id',v_sess.campaign_id,
      'discount_bps',v_slot.granted_bps,'discount_paise',v_slot.discount_paise,
      'amount_paise',v_pay.amount_paise,'settled_via',v_pay.settled_via);
  end if;

  if v_slot.status = 'redeemed' then raise exception 'SLOT_ALREADY_REDEEMED'; end if;
  if v_slot.status <> 'locked'  then raise exception 'SLOT_NOT_LOCKED';       end if;
  if v_sess.offer_amount_paise <> p_amount_paise
    then raise exception 'AMOUNT_MISMATCH'; end if;

  select * into v_camp from campaigns where id = v_sess.campaign_id for update;
  v_token := encode(gen_random_bytes(12), 'hex');

  update campaigns
     set spent_paise    = spent_paise + v_slot.discount_paise,
         reserved_paise = greatest(reserved_paise - v_slot.reserved_paise, 0)
   where id = v_camp.id;

  update slots
     set status='redeemed', reserved_paise=0,
         redemption_token=v_token, redeemed_at=now()
   where id = v_slot.id;

  insert into payments (session_id, slot_id, campaign_id, rzp_order_id,
                        rzp_payment_id, rzp_signature, amount_paise,
                        discount_paise, discount_bps, status, settled_via)
  values (v_sess.id, v_slot.id, v_camp.id, p_rzp_order_id, p_rzp_payment_id,
          p_signature, p_amount_paise, v_slot.discount_paise, v_slot.granted_bps,
          'captured', p_source);

  update sessions set status='paid', updated_at=now() where id = v_sess.id;

  insert into decisions (campaign_id, slot_id, session_id, kind, code,
                         granted_bps, human_reason, customer_reason, meta)
  values (v_camp.id, v_slot.id, v_sess.id, 'settled',
          'S01_SETTLED_' || upper(p_source), v_slot.granted_bps,
          format('Settled %s via %s: %s bps, %s discount. Spent now %s of %s.',
                 p_rzp_payment_id, p_source, v_slot.granted_bps,
                 (v_slot.discount_paise/100.0)::numeric(12,2),
                 ((v_camp.spent_paise + v_slot.discount_paise)/100.0)::numeric(12,2),
                 (v_camp.budget_paise/100.0)::numeric(12,2)),
          'Payment received.',
          jsonb_build_object('rzp_payment_id',p_rzp_payment_id,
                             'amount_paise',p_amount_paise));

  return jsonb_build_object('settled',true,'already',false,'redemption_token',v_token,
    'slot_id',v_slot.id,'session_id',v_sess.id,'campaign_id',v_camp.id,
    'discount_bps',v_slot.granted_bps,'discount_paise',v_slot.discount_paise,
    'amount_paise',p_amount_paise,'settled_via',p_source);
end $$;

-- ------------------------------------------------- release_reservation ----
create or replace function public.release_reservation(
  p_rzp_order_id text, p_reason text
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_sess sessions%rowtype; v_slot slots%rowtype;
begin
  select * into v_sess from sessions where rzp_order_id = p_rzp_order_id;
  if not found then
    return jsonb_build_object('released',false,'code','ORDER_NOT_FOUND'); end if;
  select * into v_slot from slots where id = v_sess.slot_id for update;
  if v_slot.status <> 'locked' then
    return jsonb_build_object('released',false,'code','SLOT_NOT_LOCKED'); end if;
  update campaigns set reserved_paise = greatest(reserved_paise - v_slot.reserved_paise, 0)
    where id = v_sess.campaign_id;
  update slots set status='offered', reserved_paise=0, locked_at=null
    where id = v_slot.id;
  update sessions set status='open', rzp_order_id=null, updated_at=now()
    where id = v_sess.id;
  insert into decisions (campaign_id, slot_id, session_id, kind, code,
                         human_reason, customer_reason)
  values (v_sess.campaign_id, v_slot.id, v_sess.id, 'payment_failed',
          'P02_RESERVATION_RELEASED',
          format('Reservation on order %s released: %s.', p_rzp_order_id, p_reason),
          'Payment did not go through. Your offer is still open.');
  return jsonb_build_object('released',true);
end $$;

-- ------------------------------------------------- verify_redemption -----
-- Burn-once: the first scan is green, every later scan is red.
create or replace function public.verify_redemption(p_redemption_token text)
returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_slot slots%rowtype; v_camp campaigns%rowtype;
        v_sess sessions%rowtype; v_m merchants%rowtype;
        v_first boolean; v_when timestamptz;
begin
  select * into v_slot from slots
    where redemption_token = p_redemption_token for update;
  if not found then
    insert into decisions (kind, code, human_reason)
    values ('verify_rejected','V04_UNKNOWN_TOKEN',
            format('Unknown redemption token presented (%s).',
                   left(p_redemption_token,8)));
    return jsonb_build_object('valid',false,'code','V04_UNKNOWN_TOKEN');
  end if;

  select * into v_camp from campaigns where id = v_slot.campaign_id;
  select * into v_m    from merchants where id = v_camp.merchant_id;
  select * into v_sess from sessions
    where slot_id = v_slot.id and status='paid' order by created_at desc limit 1;

  v_first := (v_slot.verified_at is null);
  v_when  := coalesce(v_slot.verified_at, now());
  if v_first then update slots set verified_at = v_when where id = v_slot.id; end if;

  insert into decisions (campaign_id, slot_id, session_id, kind, code,
                         granted_bps, human_reason, customer_reason)
  values (v_camp.id, v_slot.id, v_sess.id,
          case when v_first then 'verified' else 'verify_rejected' end,
          case when v_first then 'V01_VALID_FIRST_USE' else 'V02_ALREADY_VERIFIED' end,
          v_slot.granted_bps,
          case when v_first then
            format('Verified slot %s: granted %s bps <= committed ceiling %s bps. Root %s.',
                   v_slot.slot_token, v_slot.granted_bps, v_slot.ceiling_bps,
                   left(v_camp.merkle_root,12))
          else
            format('REJECTED slot %s: already verified at %s.', v_slot.slot_token, v_when)
          end,
          case when v_first then 'Discount verified.' else 'Already used.' end);

  return jsonb_build_object(
    'valid', v_first,
    'code',  case when v_first then 'V01_VALID_FIRST_USE' else 'V02_ALREADY_VERIFIED' end,
    'store', v_m.name, 'campaign_name', v_camp.name,
    'sku', v_sess.current_sku, 'qty', v_sess.current_qty,
    'slot_token', v_slot.slot_token, 'leaf_index', v_slot.leaf_index,
    'salt_hex', v_slot.salt_hex, 'ceiling_bps', v_slot.ceiling_bps,
    'granted_bps', v_slot.granted_bps, 'discount_paise', v_slot.discount_paise,
    'final_amount_paise', v_sess.offer_amount_paise,
    'leaf_hash', v_slot.leaf_hash, 'proof', v_slot.proof,
    'merkle_root', v_camp.merkle_root, 'policy_hash', v_camp.policy_hash,
    'tree_size', v_camp.tree_size, 'committed_at', v_camp.committed_at,
    'first_verified_at', v_when);
end $$;

-- ----------------------------------------------------------- reset_demo --
-- Between rehearsals. Keeps slot_token / salt / ceiling / proof / root, so
-- the printed QR sheet on the table stays valid. Do NOT reprint.
create or replace function public.reset_demo(p_campaign_id uuid)
returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_slots int;
begin
  delete from payments  where campaign_id = p_campaign_id;
  delete from decisions where campaign_id = p_campaign_id and kind <> 'campaign_committed';
  delete from sessions  where campaign_id = p_campaign_id;
  delete from webhook_events;
  update slots set status='unused', granted_bps=null, discount_paise=null,
                   reserved_paise=0, redemption_token=null,
                   locked_at=null, redeemed_at=null, verified_at=null
    where campaign_id = p_campaign_id;
  get diagnostics v_slots = row_count;
  update campaigns set spent_paise=0, reserved_paise=0, status='live'
    where id = p_campaign_id;
  return jsonb_build_object('ok',true,'slots_reset',v_slots,
                            'note','QR sheet still valid - do not reprint.');
end $$;

-- ------------------------------------------------------------ nuke_demo --
create or replace function public.nuke_demo(p_merchant_id uuid)
returns jsonb
language plpgsql security definer set search_path = public as $$
begin
  delete from webhook_events;
  delete from campaigns where merchant_id = p_merchant_id;  -- cascades
  return jsonb_build_object('ok',true,'note','REPRINT THE QR SHEET.');
end $$;

-- ===================== read + create helpers ==============================
-- Everything the API needs is a named database function. There is no ad-hoc
-- SQL in the application, which keeps the data layer to exactly rpc().

create or replace function public.create_campaign(
  p_merchant_id uuid, p_name text, p_budget_paise bigint,
  p_max_discount_bps int, p_margin_floor_bps int,
  p_max_turns int, p_slot_count int
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_id uuid;
begin
  if not exists (select 1 from merchants where id = p_merchant_id)
    then raise exception 'MERCHANT_NOT_FOUND'; end if;

  insert into campaigns (merchant_id, name, budget_paise, max_discount_bps,
                         margin_floor_bps, max_turns, slot_count)
  values (p_merchant_id, p_name, p_budget_paise, p_max_discount_bps,
          p_margin_floor_bps, p_max_turns, p_slot_count)
  returning id into v_id;

  return get_campaign(v_id);
end $$;

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
    'merkle_root', c.merkle_root, 'policy_hash', c.policy_hash,
    'tree_size', c.tree_size, 'committed_at', c.committed_at,
    'created_at', c.created_at,
    'slots_total',    (select count(*) from slots s where s.campaign_id = c.id),
    'slots_redeemed', (select count(*) from slots s where s.campaign_id = c.id and s.status = 'redeemed'),
    'slots_verified', (select count(*) from slots s where s.campaign_id = c.id and s.verified_at is not null)
  )
  from campaigns c join merchants m on m.id = c.merchant_id
  where c.id = p_campaign_id;
$$;

-- Slot tokens and ceilings for the printable sheet. Deliberately omits the
-- salt: the sheet is a physical object that leaves the merchant's hands.
create or replace function public.list_campaign_slots(p_campaign_id uuid)
returns jsonb
language sql stable security definer set search_path = public as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'leaf_index', s.leaf_index, 'slot_token', s.slot_token,
           'ceiling_bps', s.ceiling_bps, 'status', s.status,
           'leaf_hash', s.leaf_hash
         ) order by s.leaf_index), '[]'::jsonb)
  from slots s where s.campaign_id = p_campaign_id;
$$;

create or replace function public.get_audit_feed(
  p_campaign_id uuid, p_after_id bigint default 0, p_limit int default 50
) returns jsonb
language sql stable security definer set search_path = public as $$
  with rows as (
    select d.* from decisions d
     where d.campaign_id = p_campaign_id and d.id > coalesce(p_after_id, 0)
     order by d.id asc limit least(coalesce(p_limit, 50), 200)
  )
  select jsonb_build_object(
    'cursor', coalesce((select max(id) from rows), coalesce(p_after_id, 0)),
    'campaign', get_campaign(p_campaign_id),
    'items', coalesce((select jsonb_agg(to_jsonb(r) order by r.id) from rows r), '[]'::jsonb)
  );
$$;

create or replace function public.get_merchant_by_name(p_name text)
returns jsonb
language sql stable security definer set search_path = public as $$
  select to_jsonb(m) from merchants m where m.name = p_name limit 1;
$$;

create or replace function public.list_merchant_campaigns(p_merchant_id uuid)
returns jsonb
language sql stable security definer set search_path = public as $$
  select coalesce(jsonb_agg(get_campaign(c.id) order by c.created_at desc), '[]'::jsonb)
  from campaigns c where c.merchant_id = p_merchant_id;
$$;

-- ============================== health_check ==============================
-- Deliberately NOT the old ping(). ping() was `language sql stable` returning
-- a literal, so it needed no privilege at all: a backend misconfigured with
-- the anon key reported a full green while every table read silently returned
-- [] under RLS. This one reads a real table, so a wrong key fails loudly.
create or replace function public.health_check()
returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_items int; v_campaigns int;
begin
  select count(*) into v_items     from catalog_items;
  select count(*) into v_campaigns from campaigns;
  return jsonb_build_object(
    'ok', v_items > 0,
    'catalog_items', v_items,
    'campaigns', v_campaigns,
    'server_time', now()
  );
end $$;

-- =============================== privileges ===============================
-- MUST be the last thing in this file. Postgres grants EXECUTE to PUBLIC on
-- every newly created function, so any `create or replace` above silently
-- re-opens everything -- including nuke_demo -- to the anon role. Re-running
-- this file therefore has to re-close it.
--
-- Scoped to OUR functions by name, never `all functions in schema public`:
-- pgcrypto also lives in public, and gen_random_uuid() is a column default on
-- six tables. Revoking that from PUBLIC would break inserts for every role
-- that is not service_role, which is a much larger blast radius than the hole
-- being closed.
--
-- Only service_role executes these. The browser never talks to Postgres; all
-- access goes through the FastAPI backend, which holds the service key.
do $$
declare
  fn record;
  ours text[] := array[
    'ping','health_check','create_campaign','commit_campaign','get_campaign',
    'list_campaign_slots','list_merchant_campaigns','get_merchant_by_name',
    'get_session_context','get_audit_feed','reserve_slot','settle_payment',
    'release_reservation','verify_redemption','reset_demo','nuke_demo'
  ];
begin
  for fn in
    select p.oid::regprocedure as sig
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.proname = any(ours)
  loop
    execute format('revoke execute on function %s from public', fn.sig);
    -- These roles only exist on Supabase, not on a bare local Postgres.
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
