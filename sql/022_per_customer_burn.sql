-- A sticker is used up for the PERSON who used it, not for everyone.
--
-- The premise this whole line of work rests on: a shelf sticker is a fixture.
-- Many people scan the same one over its life. "Already used" therefore has to
-- mean "already used by this shopper", and until now it meant "dead for
-- everyone" -- slots.status went offered -> locked -> redeemed and the second
-- customer to scan got SLOT_NOT_OPEN.
--
-- WHY THERE IS NO `redemptions` TABLE. The obvious shape is a new table, one
-- row per use. But `payments` is ALREADY exactly that: one row per settlement,
-- carrying session, slot, campaign and amounts. Adding a new table would mean
-- moving settlement state onto it and rewriting every function that reads
-- slots.redemption_token / granted_bps / discount_paise -- a full rewrite of
-- the money path, with a half-migrated window where one webhook path writes
-- one place and the poller reads another. Putting the per-use fields on
-- payments is the same feature with a fraction of the blast radius.
--
-- GUARANTEE 1 IS NOT WEAKENED. uq_payment_one_captured_per_slot said "one
-- captured payment per slot, ever". It is replaced by two narrower indexes,
-- neither of which relaxes the other:
--
--   once slots   -> one captured payment per slot, exactly as before
--   identified   -> one captured payment per (campaign, customer)
--
-- Legacy slots are all 'once', so every historical row keeps the guarantee it
-- was written under.
--
-- Applied when there are zero locked slots and zero reserved budget, so there
-- is no in-flight settlement to migrate. Verified before writing this.

-- ------------------------------------------------------------- new columns --
alter table campaigns
  -- The merchant's choice, frozen at commit alongside the ceilings. 'once' is
  -- the default so nothing existing changes and a shopkeeper opts in.
  add column if not exists sticker_sharing text not null default 'once'
    check (sticker_sharing in ('once','shared'));

alter table slots
  -- Denormalised from the campaign at commit so a partial index can reference
  -- it -- a partial index cannot reach into another table.
  add column if not exists sharing text not null default 'once'
    check (sharing in ('once','shared'));

alter table sessions
  -- The reservation moves from the slot to the session. On a shared sticker
  -- several people can be mid-negotiation at once, and a single
  -- slots.reserved_paise would let one shopper's checkout wipe out another's
  -- hold on the budget.
  add column if not exists reserved_paise bigint not null default 0
    check (reserved_paise >= 0);

alter table payments
  add column if not exists customer_id uuid references customers(id) on delete set null,
  add column if not exists slot_sharing text not null default 'once'
    check (slot_sharing in ('once','shared')),
  -- The redemption token moves here too. One slot, many uses, many tokens.
  add column if not exists redemption_token text,
  add column if not exists verified_at timestamptz;

-- ---------------------------------------------------------------- backfill --
-- Every historical payment predates customers and shared stickers, so it is
-- 'once' with no customer. The token comes off the slot it was minted on.
update payments p
   set slot_sharing = 'once',
       redemption_token = coalesce(p.redemption_token, s.redemption_token),
       verified_at = coalesce(p.verified_at, s.verified_at),
       customer_id = coalesce(p.customer_id, se.customer_id)
  from slots s, sessions se
 where s.id = p.slot_id and se.id = p.session_id;

-- ----------------------------------------------------------------- indexes --
create unique index if not exists uq_payment_redemption_token
  on payments (redemption_token) where redemption_token is not null;

drop index if exists uq_payment_one_captured_per_slot;

-- GUARANTEE 1, unchanged in meaning for every slot that is not shared.
create unique index if not exists uq_payment_captured_once_slot
  on payments (slot_id)
  where status = 'captured' and slot_sharing = 'once';

-- The new burn: one discount per customer per campaign. This is what makes a
-- shared sticker safe -- and it is also what closes sticker-shopping, since a
-- shopper who scans five stickers still only gets one discount.
create unique index if not exists uq_payment_captured_per_customer
  on payments (campaign_id, customer_id)
  where status = 'captured' and customer_id is not null;

create index if not exists ix_payments_customer
  on payments (customer_id, created_at desc) where customer_id is not null;

-- ------------------------------------------------------------ reserve_slot --
drop function if exists public.reserve_slot(uuid, text, int, int, bigint, bigint, text);

create or replace function public.reserve_slot(
  p_session_id uuid, p_sku text, p_qty int, p_discount_bps int,
  p_discount_paise bigint, p_amount_paise bigint, p_rzp_order_id text
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_sess sessions%rowtype; v_slot slots%rowtype; v_camp campaigns%rowtype;
        v_cap int; v_item catalog_items%rowtype; v_customer_cap int;
begin
  select * into v_sess from sessions where id = p_session_id for update;
  if not found              then raise exception 'SESSION_NOT_FOUND';    end if;
  if v_sess.status = 'paid' then raise exception 'SESSION_ALREADY_PAID'; end if;

  select * into v_slot from slots where id = v_sess.slot_id for update;

  -- A shared sticker is never "used up" by someone else. What IS checked is
  -- whether this shopper already took their one discount from this campaign --
  -- caught here so they get a sentence rather than a unique-violation at
  -- settlement.
  if v_slot.sharing = 'shared' then
    if v_sess.customer_id is not null and exists (
      select 1 from payments
       where campaign_id = v_sess.campaign_id
         and customer_id = v_sess.customer_id
         and status = 'captured'
    ) then raise exception 'CUSTOMER_ALREADY_REDEEMED'; end if;
  else
    if v_slot.status = 'redeemed' then raise exception 'SLOT_ALREADY_REDEEMED'; end if;
  end if;
  if v_slot.status = 'void' then raise exception 'SLOT_VOID'; end if;

  if p_discount_bps > v_slot.ceiling_bps then raise exception 'CEILING_VIOLATION'; end if;
  if p_amount_paise < 100 then raise exception 'BELOW_MIN_ORDER_AMOUNT'; end if;

  select * into v_camp from campaigns where id = v_sess.campaign_id for update;
  if v_camp.status <> 'live' then raise exception 'CAMPAIGN_NOT_LIVE'; end if;
  if p_discount_bps > v_camp.max_discount_bps
    then raise exception 'CAMPAIGN_MAX_VIOLATION'; end if;

  select cap_bps into v_cap from campaign_product_caps
   where campaign_id = v_camp.id and sku = upper(trim(p_sku));
  if v_cap is not null and p_discount_bps > v_cap
    then raise exception 'PRODUCT_CAP_VIOLATION'; end if;

  if v_cap is not null and v_sess.tier_cap_fraction_bps is not null then
    v_customer_cap := (v_cap * v_sess.tier_cap_fraction_bps) / 10000;
    if p_discount_bps > v_customer_cap
      then raise exception 'CUSTOMER_TIER_VIOLATION'; end if;
  end if;

  select * into v_item from catalog_items
   where merchant_id = v_camp.merchant_id and sku = upper(trim(p_sku));
  if found and margin_bps_after(v_item.price_paise, v_item.cost_paise,
                                p_discount_bps) < v_camp.margin_floor_bps
    then raise exception 'MARGIN_FLOOR_VIOLATION'; end if;

  -- Replace THIS session's prior hold, not the slot's. On a shared sticker the
  -- slot's hold is meaningless because several sessions have one.
  if v_camp.spent_paise + v_camp.reserved_paise
       - v_sess.reserved_paise + p_discount_paise > v_camp.budget_paise
    then raise exception 'BUDGET_EXCEEDED'; end if;

  update campaigns
     set reserved_paise = reserved_paise - v_sess.reserved_paise + p_discount_paise
   where id = v_camp.id;

  -- A shared sticker stays 'offered' forever: it is a fixture on a shelf, and
  -- the next shopper must find it open.
  if v_slot.sharing = 'once' then
    update slots
       set status='locked', granted_bps=p_discount_bps,
           discount_paise=p_discount_paise, reserved_paise=p_discount_paise,
           locked_at=now()
     where id = v_slot.id;
  end if;

  update sessions
     set status='offer_locked', current_sku=p_sku, current_qty=p_qty,
         offer_bps=p_discount_bps, offer_discount_paise=p_discount_paise,
         offer_amount_paise=p_amount_paise, rzp_order_id=p_rzp_order_id,
         reserved_paise=p_discount_paise, updated_at=now()
   where id = p_session_id;

  insert into decisions (campaign_id, slot_id, session_id, kind, code,
                         granted_bps, human_reason, customer_reason, meta)
  values (v_camp.id, v_slot.id, p_session_id, 'order_created', 'P01_ORDER_CREATED',
          p_discount_bps,
          format('Order %s created: %s x%s at %s bps off, %s payable.',
                 p_rzp_order_id, p_sku, p_qty, p_discount_bps,
                 (p_amount_paise/100.0)::numeric(12,2)),
          'Offer locked. Opening checkout.',
          jsonb_build_object('rzp_order_id',p_rzp_order_id,
                             'product_cap_bps',v_cap,
                             'customer_cap_bps',v_customer_cap,
                             'sharing',v_slot.sharing));

  return jsonb_build_object('ok',true,'slot_id',v_slot.id,'campaign_id',v_camp.id,
                            'reserved_paise',p_discount_paise);
end $$;

-- ----------------------------------------------------------- settle_payment --
drop function if exists public.settle_payment(text, text, text, bigint, text);

create or replace function public.settle_payment(
  p_rzp_order_id text, p_rzp_payment_id text, p_signature text,
  p_amount_paise bigint, p_source text
) returns jsonb
language plpgsql security definer set search_path = public, extensions as $$
declare v_sess sessions%rowtype; v_slot slots%rowtype;
        v_camp campaigns%rowtype; v_pay payments%rowtype; v_token text;
begin
  if p_source not in ('checkout_handler','webhook','poll','manual')
    then raise exception 'BAD_SOURCE'; end if;

  select * into v_sess from sessions where rzp_order_id = p_rzp_order_id;
  if not found then raise exception 'ORDER_NOT_FOUND'; end if;

  -- Every settlement path for this slot still serialises on this row lock.
  select * into v_slot from slots where id = v_sess.slot_id for update;

  -- READ COMMITTED gives a fresh snapshot once the lock is acquired, so the
  -- loser of a race sees the winner's committed row and returns it verbatim.
  select * into v_pay from payments where rzp_payment_id = p_rzp_payment_id;
  if found then
    return jsonb_build_object('settled',true,'already',true,
      'redemption_token',v_pay.redemption_token,'slot_id',v_slot.id,
      'session_id',v_sess.id,'campaign_id',v_sess.campaign_id,
      'discount_bps',v_pay.discount_bps,'discount_paise',v_pay.discount_paise,
      'amount_paise',v_pay.amount_paise,'settled_via',v_pay.settled_via);
  end if;

  if v_slot.sharing = 'once' and v_slot.status = 'redeemed'
    then raise exception 'SLOT_ALREADY_REDEEMED'; end if;
  if v_sess.status <> 'offer_locked' then raise exception 'SLOT_NOT_LOCKED'; end if;
  if v_sess.offer_amount_paise <> p_amount_paise
    then raise exception 'AMOUNT_MISMATCH'; end if;

  select * into v_camp from campaigns where id = v_sess.campaign_id for update;
  v_token := encode(gen_random_bytes(12), 'hex');

  update campaigns
     set spent_paise    = spent_paise + v_sess.offer_discount_paise,
         reserved_paise = greatest(reserved_paise - v_sess.reserved_paise, 0)
   where id = v_camp.id;

  -- Only a one-shot sticker dies here. A shared one goes back to being a
  -- fixture the next shopper can scan.
  if v_slot.sharing = 'once' then
    update slots
       set status='redeemed', reserved_paise=0,
           granted_bps=v_sess.offer_bps, discount_paise=v_sess.offer_discount_paise,
           redemption_token=v_token, redeemed_at=now()
     where id = v_slot.id;
  end if;

  -- The unique index on (campaign_id, customer_id) is what actually enforces
  -- one discount per shopper; this insert is where it bites.
  insert into payments (session_id, slot_id, campaign_id, rzp_order_id,
                        rzp_payment_id, rzp_signature, amount_paise,
                        discount_paise, discount_bps, status, settled_via,
                        customer_id, slot_sharing, redemption_token)
  values (v_sess.id, v_slot.id, v_camp.id, p_rzp_order_id, p_rzp_payment_id,
          p_signature, p_amount_paise, v_sess.offer_discount_paise,
          v_sess.offer_bps, 'captured', p_source,
          v_sess.customer_id, v_slot.sharing, v_token);

  update sessions set status='paid', reserved_paise=0, updated_at=now()
   where id = v_sess.id;

  insert into decisions (campaign_id, slot_id, session_id, kind, code,
                         granted_bps, human_reason, customer_reason, meta)
  values (v_camp.id, v_slot.id, v_sess.id, 'settled',
          'S01_SETTLED_' || upper(p_source), v_sess.offer_bps,
          format('Settled %s via %s: %s bps, %s discount. Spent now %s of %s.',
                 p_rzp_payment_id, p_source, v_sess.offer_bps,
                 (v_sess.offer_discount_paise/100.0)::numeric(12,2),
                 ((v_camp.spent_paise + v_sess.offer_discount_paise)/100.0)::numeric(12,2),
                 (v_camp.budget_paise/100.0)::numeric(12,2)),
          'Payment received.',
          jsonb_build_object('rzp_payment_id',p_rzp_payment_id,
                             'amount_paise',p_amount_paise,
                             'sharing',v_slot.sharing));

  return jsonb_build_object('settled',true,'already',false,'redemption_token',v_token,
    'slot_id',v_slot.id,'session_id',v_sess.id,'campaign_id',v_camp.id,
    'discount_bps',v_sess.offer_bps,'discount_paise',v_sess.offer_discount_paise,
    'amount_paise',p_amount_paise,'settled_via',p_source);
end $$;

-- ------------------------------------------------------ release_reservation --
drop function if exists public.release_reservation(text, text);

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
  -- Keyed off the SESSION now, not the slot: a shared slot is never 'locked'.
  if v_sess.status <> 'offer_locked' then
    return jsonb_build_object('released',false,'code','SLOT_NOT_LOCKED'); end if;

  update campaigns set reserved_paise = greatest(reserved_paise - v_sess.reserved_paise, 0)
    where id = v_sess.campaign_id;
  if v_slot.sharing = 'once' then
    update slots set status='offered', reserved_paise=0, locked_at=null
      where id = v_slot.id;
  end if;
  update sessions set status='open', rzp_order_id=null, reserved_paise=0,
         updated_at=now()
    where id = v_sess.id;

  insert into decisions (campaign_id, slot_id, session_id, kind, code,
                         human_reason, customer_reason)
  values (v_sess.campaign_id, v_slot.id, v_sess.id, 'payment_failed',
          'P02_RESERVATION_RELEASED',
          format('Reservation on order %s released: %s.', p_rzp_order_id, p_reason),
          'Payment did not go through. Your offer is still open.');
  return jsonb_build_object('released',true);
end $$;

-- ------------------------------------------------------- verify_redemption --
drop function if exists public.verify_redemption(text);

create or replace function public.verify_redemption(p_redemption_token text)
returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_pay payments%rowtype; v_slot slots%rowtype; v_camp campaigns%rowtype;
        v_sess sessions%rowtype; v_m merchants%rowtype;
        v_first boolean; v_when timestamptz;
begin
  -- Tokens live on payments now, one per settlement. Legacy tokens were minted
  -- onto the slot and backfilled across, so this one lookup covers both.
  select * into v_pay from payments
    where redemption_token = p_redemption_token for update;
  if not found then
    insert into decisions (kind, code, human_reason)
    values ('verify_rejected','V04_UNKNOWN_TOKEN',
            format('Unknown redemption token presented (%s).',
                   left(p_redemption_token,8)));
    return jsonb_build_object('valid',false,'code','V04_UNKNOWN_TOKEN');
  end if;

  select * into v_slot from slots     where id = v_pay.slot_id;
  select * into v_camp from campaigns where id = v_pay.campaign_id;
  select * into v_m    from merchants where id = v_camp.merchant_id;
  select * into v_sess from sessions  where id = v_pay.session_id;

  -- Burn-once, now per PAYMENT rather than per slot. On a shared sticker two
  -- shoppers hold two different tokens, and one being spent must not spend the
  -- other.
  v_first := (v_pay.verified_at is null);
  v_when  := coalesce(v_pay.verified_at, now());
  if v_first then
    update payments set verified_at = v_when where id = v_pay.id;
    -- Kept in step so get_campaign's slots_verified counter and the QR sheet
    -- keep reporting what they always did.
    if v_slot.sharing = 'once' and v_slot.verified_at is null then
      update slots set verified_at = v_when where id = v_slot.id;
    end if;
  end if;

  insert into decisions (campaign_id, slot_id, session_id, kind, code,
                         granted_bps, human_reason, customer_reason)
  values (v_camp.id, v_slot.id, v_sess.id,
          case when v_first then 'verified' else 'verify_rejected' end,
          case when v_first then 'V01_VALID_FIRST_USE' else 'V02_ALREADY_VERIFIED' end,
          v_pay.discount_bps,
          case when v_first then
            format('Verified slot %s: granted %s bps <= committed ceiling %s bps. Root %s.',
                   v_slot.slot_token, v_pay.discount_bps, v_slot.ceiling_bps,
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
    'granted_bps', v_pay.discount_bps, 'discount_paise', v_pay.discount_paise,
    'final_amount_paise', v_pay.amount_paise,
    'leaf_hash', v_slot.leaf_hash, 'proof', v_slot.proof,
    'merkle_root', v_camp.merkle_root, 'policy_hash', v_camp.policy_hash,
    'tree_size', v_camp.tree_size, 'committed_at', v_camp.committed_at,
    'first_verified_at', v_when);
end $$;

-- =============================== privileges ===============================
do $$
declare
  fn record;
  ours text[] := array['reserve_slot','settle_payment','release_reservation',
                       'verify_redemption'];
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
