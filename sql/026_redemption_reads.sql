-- The redemption reads, corrected -- and told who is standing at the counter.
--
-- TWO BUGS, BOTH FROM MIGRATION 022.
--
-- 022 moved the redemption token from `slots` to `payments`, because a shared
-- sticker is redeemed many times and one slot column cannot hold many tokens.
-- It updated settle_payment and verify_redemption. It did NOT update the two
-- read paths, which still say `sl.redemption_token`:
--
--   * get_payment_status is what the phone POLLS while it waits for the
--     webhook. On a shared sticker settle_payment never writes the slot
--     column, so the poll answered `settled: true, redemption_token: null`.
--     SlotPage reads that null, cannot navigate to /r/<token>, and prints
--     "Paid, but the receipt is taking a moment" -- while the Pay button sits
--     on "Opening checkout..." for the full 90-second poll. The customer has
--     paid and there is no QR for the merchant to scan. That is the stuck
--     screen.
--
--   * get_redemption is the customer's own redemption page. Same null lookup:
--     the page 404s on a shared sticker. Its `final_amount_paise` was also
--     read through a lateral join picking the LATEST paid session on the slot,
--     which on a shared sticker is somebody else's basket.
--
-- Both now resolve through payments, which is where a token has lived since
-- 022. Legacy tokens were backfilled onto payments by that same migration, so
-- one lookup covers both eras.
--
-- AND THE THING THE COUNTER ACTUALLY NEEDED. verify_redemption returned the
-- product and the proof and nothing at all about the person holding the
-- phone -- so a shopkeeper scanning a code learned that 5kg of basmati was
-- discounted 6% and not that this is their eleventh visit this month. The
-- whole point of asking for a number at the door is that it comes back out at
-- the counter. It now returns the shopper's standing and the itemised basket,
-- which is also the bill.

-- ------------------------------------------------------- get_payment_status --
create or replace function public.get_payment_status(p_rzp_order_id text)
returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'settled',          p.id is not null,
    'rzp_order_id',     p_rzp_order_id,
    'rzp_payment_id',   p.rzp_payment_id,
    'amount_paise',     p.amount_paise,
    'discount_bps',     p.discount_bps,
    'discount_paise',   p.discount_paise,
    'settled_via',      p.settled_via,
    -- From the PAYMENT. slots.redemption_token is written only for
    -- sharing='once' and is null for every shared sticker.
    'redemption_token', p.redemption_token,
    'slot_token',       sl.slot_token,
    'slot_status',      sl.status,
    'session_id',       se.id,
    'session_status',   se.status,
    'campaign_id',      se.campaign_id
  )
  from sessions se
  left join slots sl    on sl.id = se.slot_id
  left join payments p  on p.rzp_order_id = se.rzp_order_id
                       and p.status = 'captured'
  where se.rzp_order_id = p_rzp_order_id
  limit 1;
$$;

-- ------------------------------------------------------------- cart_summary --
-- The basket behind one session, as the bill renders it. Falls back to the
-- session's single current_sku for anything bought before carts existed, so an
-- old redemption still prints a line rather than an empty bill.
create or replace function public.cart_summary(p_session_id uuid)
returns jsonb
language sql stable security definer set search_path = public as $$
  with lines as (
    select ci.sku, ci.name, ci.unit, ci.qty, ci.unit_price_paise,
           ci.granted_bps, ci.discount_paise, ci.line_total_paise,
           ci.binding_constraint, ci.added_at
      from cart_items ci
      join carts c on c.id = ci.cart_id
     where c.session_id = p_session_id
  ), legacy as (
    select se.current_sku as sku,
           coalesce(ci.name, se.current_sku) as name,
           coalesce(ci.unit, 'pc') as unit,
           se.current_qty as qty,
           coalesce(ci.price_paise, 0) as unit_price_paise,
           coalesce(se.offer_bps, 0) as granted_bps,
           coalesce(se.offer_discount_paise, 0) as discount_paise,
           coalesce(se.offer_amount_paise, 0) as line_total_paise,
           null::text as binding_constraint,
           se.created_at as added_at
      from sessions se
      join campaigns c on c.id = se.campaign_id
      left join catalog_items ci on ci.merchant_id = c.merchant_id
                                and ci.sku = se.current_sku
     where se.id = p_session_id
       and se.current_sku is not null
       and not exists (select 1 from lines)
  ), all_lines as (
    select * from lines union all select * from legacy
  )
  select jsonb_build_object(
    'items', coalesce(jsonb_agg(jsonb_build_object(
               'sku', sku, 'name', name, 'unit', unit, 'qty', qty,
               'unit_price_paise', unit_price_paise,
               'gross_paise', unit_price_paise * qty,
               'granted_bps', granted_bps,
               'discount_paise', discount_paise,
               'line_total_paise', line_total_paise,
               'binding_constraint', binding_constraint)
             order by added_at), '[]'::jsonb),
    'count', count(*),
    'gross_paise', coalesce(sum(unit_price_paise * qty), 0),
    'discount_paise', coalesce(sum(discount_paise), 0),
    'total_paise', coalesce(sum(line_total_paise), 0)
  )
  from all_lines;
$$;

-- --------------------------------------------------------- customer_profile --
-- Who this is, in the words a shopkeeper uses. Scoped to ONE merchant: two
-- shops on this platform must not be able to learn they share a shopper, which
-- is the same rule uq_customer_phone encodes.
--
-- p_exclude_payment_id keeps the purchase being verified right now out of the
-- history, so "8 previous visits" means eight BEFORE this one rather than a
-- number that ticks over while the code is being scanned.
create or replace function public.customer_profile(
  p_customer_id uuid, p_merchant_id uuid, p_exclude_payment_id uuid default null
) returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'identified',    true,
    'phone_last4',   cu.phone_last4,
    'display_name',  cu.display_name,
    'first_seen_at', cu.first_seen_at,
    'last_seen_at',  cu.last_seen_at,
    'visits',        coalesce(h.visits, 0),
    'spend_paise',   coalesce(h.spend_paise, 0),
    'saved_paise',   coalesce(h.saved_paise, 0),
    'last_visit_at', h.last_visit_at,
    'returning',     coalesce(h.visits, 0) > 0
  )
  from customers cu
  left join lateral (
    select count(*) as visits,
           sum(p.amount_paise) as spend_paise,
           sum(p.discount_paise) as saved_paise,
           max(p.created_at) as last_visit_at
      from payments p
      join campaigns c on c.id = p.campaign_id
     where p.customer_id = cu.id
       and p.status = 'captured'
       and c.merchant_id = p_merchant_id
       and (p_exclude_payment_id is null or p.id <> p_exclude_payment_id)
  ) h on true
  where cu.id = p_customer_id and cu.merchant_id = p_merchant_id;
$$;

-- ---------------------------------------------------------- get_redemption --
-- Resolved through payments, and carrying the basket so the customer's own
-- screen can show what they actually bought rather than one sku.
drop function if exists public.get_redemption(text);

create or replace function public.get_redemption(p_redemption_token text)
returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'slot_token',         sl.slot_token,
    'leaf_index',         sl.leaf_index,
    'salt_hex',           sl.salt_hex,
    'ceiling_bps',        sl.ceiling_bps,
    -- Per PAYMENT, not per slot: on a shared sticker the slot's own
    -- granted_bps belongs to whoever redeemed it last.
    'granted_bps',        p.discount_bps,
    'discount_paise',     p.discount_paise,
    'leaf_hash',          sl.leaf_hash,
    'proof',              sl.proof,
    'redeemed_at',        p.created_at,
    'verified_at',        p.verified_at,
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
    'final_amount_paise', p.amount_paise,
    'bill',               cart_summary(se.id)
  )
  from payments p
  join slots     sl on sl.id = p.slot_id
  join campaigns c  on c.id  = p.campaign_id
  join merchants m  on m.id  = c.merchant_id
  join sessions  se on se.id = p.session_id
  where p.redemption_token = p_redemption_token;
$$;

-- ------------------------------------------------------- verify_redemption --
-- Burn-once, unchanged in every respect that matters: the first scan flips
-- payments.verified_at under a row lock and every later scan comes back red.
-- What is added is everything a person at a counter needs and did not have --
-- who is in front of them, and what is in the bag.
drop function if exists public.verify_redemption(text);

create or replace function public.verify_redemption(p_redemption_token text)
returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_pay payments%rowtype; v_slot slots%rowtype; v_camp campaigns%rowtype;
        v_sess sessions%rowtype; v_m merchants%rowtype;
        v_first boolean; v_when timestamptz;
        v_customer jsonb; v_bill jsonb;
begin
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

  v_first := (v_pay.verified_at is null);
  v_when  := coalesce(v_pay.verified_at, now());
  if v_first then
    update payments set verified_at = v_when where id = v_pay.id;
    -- Kept in step so get_campaign's slots_verified counter and the QR sheet
    -- keep reporting what they always did.
    if v_slot.sharing = 'once' and v_slot.verified_at is null then
      update slots set verified_at = v_when where id = v_slot.id;
    end if;
    -- The basket is settled and scanned. Nothing else will be added to it.
    update carts set status = 'paid', updated_at = now()
     where session_id = v_sess.id and status <> 'paid';
  end if;

  -- The purchase being verified is excluded from the history, so "6 previous
  -- visits" does not become 7 the instant the code is scanned.
  if v_pay.customer_id is not null then
    v_customer := customer_profile(v_pay.customer_id, v_m.id, v_pay.id);
  end if;
  v_customer := coalesce(
    v_customer,
    jsonb_build_object('identified', false, 'visits', 0, 'spend_paise', 0,
                       'saved_paise', 0, 'returning', false));
  -- The band the shopper was placed in when this conversation opened. Read off
  -- the session snapshot, never recomputed: it is what actually priced this
  -- basket, and a number recomputed now would be a different claim.
  v_customer := v_customer || jsonb_build_object(
    'band', coalesce(v_sess.tier_key, 'new'),
    'band_at_purchase', v_sess.tier_evaluated_at);

  v_bill := cart_summary(v_sess.id);

  insert into decisions (campaign_id, slot_id, session_id, kind, code,
                         granted_bps, human_reason, customer_reason)
  values (v_camp.id, v_slot.id, v_sess.id,
          case when v_first then 'verified' else 'verify_rejected' end,
          case when v_first then 'V01_VALID_FIRST_USE' else 'V02_ALREADY_VERIFIED' end,
          v_pay.discount_bps,
          case when v_first then
            format('Verified slot %s: granted %s bps <= committed ceiling %s bps. %s lines, %s payable. Customer %s. Root %s.',
                   v_slot.slot_token, v_pay.discount_bps, v_slot.ceiling_bps,
                   v_bill->>'count', (v_pay.amount_paise/100.0)::numeric(12,2),
                   coalesce('...' || (v_customer->>'phone_last4'), 'not identified'),
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
    'first_verified_at', v_when,
    'paid_at', v_pay.created_at,
    'customer', v_customer,
    'bill', v_bill);
end $$;

-- =============================== privileges ===============================
do $$
declare
  fn record;
  ours text[] := array['get_payment_status','get_redemption','verify_redemption',
                       'cart_summary','customer_profile'];
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
