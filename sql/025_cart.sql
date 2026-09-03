-- A cart, because a shop is not a vending machine.
--
-- WHAT WAS WRONG. A session carried exactly one offer: sessions.current_sku,
-- offer_bps, offer_amount_paise. Negotiating a second item did not add to a
-- basket, it OVERWROTE the first -- the shopper haggled atta to 5% off, then
-- asked about oil, and the atta silently ceased to exist. The only button on
-- screen was "Pay Rs 137.75" for whatever had been negotiated last. That is
-- not a kirana; it is a vending machine that forgets.
--
-- WHAT THIS ADDS. One open cart per session, one row per sku, each row
-- carrying the discount THAT LINE was granted. Nothing about the gate changes:
-- a line only ever enters the cart after bounds.check() approved it, and
-- reserve_cart below re-checks every line against the committed ceilings in
-- SQL before a paisa is reserved -- the same checks reserve_slot ran for one
-- item, now run for each.
--
-- WHY THE SESSION'S offer_* COLUMNS SURVIVE. settle_payment, the webhook, the
-- poller and verify_redemption all read them. Rewriting the money path to be
-- cart-shaped would mean touching every settlement route at once, with a
-- half-migrated window where the webhook writes one place and the poller reads
-- another -- exactly the argument migration 022 made for not creating a
-- `redemptions` table. So the cart AGGREGATES into those columns at checkout:
-- offer_amount_paise is the basket total, offer_discount_paise the total
-- saved, and offer_bps the effective rate across the basket. That rate is a
-- weighted mean of per-line rates each of which is already <= the slot
-- ceiling, so it is <= the ceiling too, and slots.ck_granted_le_ceiling still
-- holds without being weakened.
--
-- TWO BUGS FROM 022 ARE FIXED IN 026. That migration moved redemption_token
-- from slots to payments and writes it onto the slot only when sharing='once';
-- get_payment_status and get_redemption were never updated. See that file.

-- ------------------------------------------------------------------ tables --
create table if not exists carts (
  id           uuid primary key default gen_random_uuid(),
  session_id   uuid not null references sessions(id)   on delete cascade,
  campaign_id  uuid not null references campaigns(id)  on delete cascade,
  customer_id  uuid references customers(id) on delete set null,
  status       text not null default 'open'
                 check (status in ('open','ordered','paid','abandoned')),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
-- One live basket per conversation. A reload resumes it; it never forks.
create unique index if not exists uq_one_open_cart_per_session
  on carts (session_id) where status in ('open','ordered');
create index if not exists ix_carts_session on carts (session_id, created_at desc);
alter table carts enable row level security;

create table if not exists cart_items (
  id                 uuid primary key default gen_random_uuid(),
  cart_id            uuid not null references carts(id) on delete cascade,
  sku                text not null,
  name               text not null,
  unit               text not null default 'pc',
  qty                int  not null default 1 check (qty between 1 and 20),
  unit_price_paise   bigint not null check (unit_price_paise >= 0),
  -- What the GATE granted this line. Never what the model said.
  granted_bps        int not null default 0 check (granted_bps between 0 and 10000),
  discount_paise     bigint not null default 0 check (discount_paise >= 0),
  line_total_paise   bigint not null check (line_total_paise >= 0),
  -- Which rule held this line down, kept so the bill can explain itself months
  -- later without re-running anything.
  decision_code      text,
  binding_constraint text,
  added_at           timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  -- One row per product. Asking about atta twice raises the quantity or
  -- improves the rate; it does not create a second atta line.
  constraint uq_cart_item_sku unique (cart_id, sku)
);
create index if not exists ix_cart_items_cart on cart_items (cart_id, added_at);
alter table cart_items enable row level security;

-- ------------------------------------------------------------ cart_for_session --
-- Every writer needs the same "find the open cart, or start one", and
-- duplicating it is how two of them end up disagreeing.
create or replace function public.cart_for_session(p_session_id uuid)
returns uuid
language plpgsql security definer set search_path = public as $$
declare v_sess sessions%rowtype; v_cart_id uuid;
begin
  select * into v_sess from sessions where id = p_session_id;
  if not found then raise exception 'SESSION_NOT_FOUND'; end if;

  select id into v_cart_id from carts
   where session_id = p_session_id and status in ('open','ordered')
   order by created_at desc limit 1;
  if v_cart_id is not null then return v_cart_id; end if;

  insert into carts (session_id, campaign_id, customer_id)
  values (p_session_id, v_sess.campaign_id, v_sess.customer_id)
  returning id into v_cart_id;
  return v_cart_id;
end $$;

-- ----------------------------------------------------------------- get_cart --
-- The whole basket in one round trip, totals included. Totals are summed here
-- rather than in Python so the phone, the bill and the checkout cannot each
-- arrive at a slightly different number.
create or replace function public.get_cart(p_session_id uuid)
returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'cart_id', c.id,
    'status',  c.status,
    'items', coalesce((
      select jsonb_agg(jsonb_build_object(
               'sku', ci.sku, 'name', ci.name, 'unit', ci.unit, 'qty', ci.qty,
               'unit_price_paise', ci.unit_price_paise,
               'granted_bps', ci.granted_bps,
               'discount_paise', ci.discount_paise,
               'line_total_paise', ci.line_total_paise,
               'gross_paise', ci.unit_price_paise * ci.qty,
               'decision_code', ci.decision_code,
               'binding_constraint', ci.binding_constraint,
               'added_at', ci.added_at)
             order by ci.added_at)
        from cart_items ci where ci.cart_id = c.id), '[]'::jsonb),
    'count', (select count(*) from cart_items ci where ci.cart_id = c.id),
    'gross_paise', coalesce((select sum(ci.unit_price_paise * ci.qty)
                               from cart_items ci where ci.cart_id = c.id), 0),
    'discount_paise', coalesce((select sum(ci.discount_paise)
                                  from cart_items ci where ci.cart_id = c.id), 0),
    'total_paise', coalesce((select sum(ci.line_total_paise)
                               from cart_items ci where ci.cart_id = c.id), 0)
  )
  from carts c
  where c.session_id = p_session_id and c.status in ('open','ordered')
  order by c.created_at desc
  limit 1;
$$;

-- --------------------------------------------------------- upsert_cart_item --
-- Called only after bounds.check() approved this exact line. The amounts are
-- passed in rather than recomputed because the gate is the single place money
-- arithmetic happens, and a second implementation here is a second thing to
-- drift. reserve_cart re-derives and re-checks them at checkout, which is
-- where being wrong would actually cost something.
create or replace function public.upsert_cart_item(
  p_session_id uuid, p_sku text, p_qty int,
  p_granted_bps int, p_discount_paise bigint, p_line_total_paise bigint,
  p_decision_code text default null, p_binding_constraint text default null
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_sess sessions%rowtype; v_camp campaigns%rowtype;
        v_item catalog_items%rowtype; v_cart_id uuid; v_sku text;
begin
  select * into v_sess from sessions where id = p_session_id;
  if not found              then raise exception 'SESSION_NOT_FOUND';    end if;
  if v_sess.status = 'paid' then raise exception 'SESSION_ALREADY_PAID'; end if;

  select * into v_camp from campaigns where id = v_sess.campaign_id;
  v_sku := upper(trim(p_sku));
  select * into v_item from catalog_items
   where merchant_id = v_camp.merchant_id and sku = v_sku and active;
  if not found then raise exception 'ITEM_NOT_FOUND'; end if;

  if p_qty is null or p_qty < 1 or p_qty > 20 then
    raise exception 'QTY_OUT_OF_RANGE';
  end if;

  v_cart_id := cart_for_session(p_session_id);

  insert into cart_items (cart_id, sku, name, unit, qty, unit_price_paise,
                          granted_bps, discount_paise, line_total_paise,
                          decision_code, binding_constraint)
  values (v_cart_id, v_sku, v_item.name, v_item.unit, p_qty, v_item.price_paise,
          p_granted_bps, p_discount_paise, p_line_total_paise,
          p_decision_code, p_binding_constraint)
  on conflict (cart_id, sku) do update
    set qty = excluded.qty,
        unit_price_paise = excluded.unit_price_paise,
        -- Never take a line BACKWARDS. A shopper who negotiated 6% and then
        -- asks a question that happens to re-price at 5% keeps the 6% they
        -- were told they had; quietly taking it away is the one thing that
        -- would make the whole negotiation feel like a trick.
        granted_bps = greatest(cart_items.granted_bps, excluded.granted_bps),
        discount_paise = case
          when excluded.granted_bps >= cart_items.granted_bps
            then excluded.discount_paise
          else (excluded.unit_price_paise * excluded.qty
                * cart_items.granted_bps) / 10000 end,
        line_total_paise = case
          when excluded.granted_bps >= cart_items.granted_bps
            then excluded.line_total_paise
          else excluded.unit_price_paise * excluded.qty
               - (excluded.unit_price_paise * excluded.qty
                  * cart_items.granted_bps) / 10000 end,
        decision_code = coalesce(excluded.decision_code, cart_items.decision_code),
        binding_constraint = case
          when excluded.granted_bps >= cart_items.granted_bps
            then excluded.binding_constraint
          else cart_items.binding_constraint end,
        updated_at = now();

  update carts set updated_at = now() where id = v_cart_id;
  return get_cart(p_session_id);
end $$;

-- --------------------------------------------------------- remove_cart_item --
create or replace function public.remove_cart_item(p_session_id uuid, p_sku text)
returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_cart_id uuid;
begin
  select id into v_cart_id from carts
   where session_id = p_session_id and status in ('open','ordered')
   order by created_at desc limit 1;
  if v_cart_id is null then return get_cart(p_session_id); end if;
  delete from cart_items where cart_id = v_cart_id and sku = upper(trim(p_sku));
  update carts set updated_at = now() where id = v_cart_id;
  return get_cart(p_session_id);
end $$;

create or replace function public.clear_cart(p_session_id uuid)
returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_cart_id uuid;
begin
  select id into v_cart_id from carts
   where session_id = p_session_id and status in ('open','ordered')
   order by created_at desc limit 1;
  if v_cart_id is null then return get_cart(p_session_id); end if;
  delete from cart_items where cart_id = v_cart_id;
  update carts set updated_at = now() where id = v_cart_id;
  return get_cart(p_session_id);
end $$;

-- ------------------------------------------------------------- reserve_cart --
-- reserve_slot, for a basket.
--
-- Every check reserve_slot made for one item is made here for EVERY line,
-- inside one transaction, before any budget moves: the slot ceiling, the
-- campaign maximum, the committed per-product cap, the shopper's tier fraction
-- of that cap, and the margin floor. The totals are recomputed from
-- catalog_items and cart_items rather than trusted from the caller, so a
-- tampered checkout body cannot buy a basket at a price nobody granted.
--
-- The caller's amount is then compared to the recomputed one and a mismatch
-- raises. Python and SQL agreeing is the invariant; the day they do not is the
-- day to find out loudly rather than to charge someone the wrong number.
create or replace function public.reserve_cart(
  p_session_id uuid, p_rzp_order_id text, p_amount_paise bigint default null
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_sess sessions%rowtype; v_slot slots%rowtype; v_camp campaigns%rowtype;
        v_cart_id uuid; v_line record; v_item catalog_items%rowtype;
        v_cap int; v_customer_cap int;
        v_gross bigint := 0; v_discount bigint := 0; v_total bigint := 0;
        v_lines int := 0; v_eff_bps int := 0;
        v_primary text; v_primary_qty int := 1; v_best bigint := -1;
begin
  select * into v_sess from sessions where id = p_session_id for update;
  if not found              then raise exception 'SESSION_NOT_FOUND';    end if;
  if v_sess.status = 'paid' then raise exception 'SESSION_ALREADY_PAID'; end if;

  select id into v_cart_id from carts
   where session_id = p_session_id and status in ('open','ordered')
   order by created_at desc limit 1;
  if v_cart_id is null then raise exception 'CART_EMPTY'; end if;

  select * into v_slot from slots where id = v_sess.slot_id for update;

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

  select * into v_camp from campaigns where id = v_sess.campaign_id for update;
  if v_camp.status <> 'live' then raise exception 'CAMPAIGN_NOT_LIVE'; end if;

  for v_line in
    select * from cart_items where cart_id = v_cart_id order by added_at
  loop
    v_lines := v_lines + 1;

    if v_line.granted_bps > v_slot.ceiling_bps then
      raise exception 'CEILING_VIOLATION'; end if;
    if v_line.granted_bps > v_camp.max_discount_bps then
      raise exception 'CAMPAIGN_MAX_VIOLATION'; end if;

    select cap_bps into v_cap from campaign_product_caps
     where campaign_id = v_camp.id and sku = v_line.sku;
    if v_cap is not null and v_line.granted_bps > v_cap then
      raise exception 'PRODUCT_CAP_VIOLATION'; end if;
    if v_cap is not null and v_sess.tier_cap_fraction_bps is not null then
      v_customer_cap := (v_cap * v_sess.tier_cap_fraction_bps) / 10000;
      if v_line.granted_bps > v_customer_cap then
        raise exception 'CUSTOMER_TIER_VIOLATION'; end if;
    end if;

    -- Price and margin come from the live catalogue, not from the cart row. A
    -- price that moved while someone was shopping must re-price the basket,
    -- not be taken on the cart's word.
    select * into v_item from catalog_items
     where merchant_id = v_camp.merchant_id and sku = v_line.sku and active;
    if not found then raise exception 'ITEM_NOT_FOUND'; end if;
    if margin_bps_after(v_item.price_paise, v_item.cost_paise,
                        v_line.granted_bps) < v_camp.margin_floor_bps then
      raise exception 'MARGIN_FLOOR_VIOLATION'; end if;

    v_gross    := v_gross + v_item.price_paise * v_line.qty;
    v_discount := v_discount
                  + (v_item.price_paise * v_line.qty * v_line.granted_bps) / 10000;

    -- Write the recomputed amounts back onto the line.
    --
    -- The cart row holds what the gate said during the conversation; this is
    -- what the shopper is actually being charged, derived a moment ago from
    -- the live catalogue. They are normally identical. When they are not --
    -- the shopkeeper edited a price mid-conversation -- the bill must show the
    -- charge, not the memory, or the itemised lines will not add up to the
    -- amount on the receipt and nobody at the counter will be able to say why.
    update cart_items
       set unit_price_paise = v_item.price_paise,
           discount_paise = (v_item.price_paise * v_line.qty * v_line.granted_bps) / 10000,
           line_total_paise = v_item.price_paise * v_line.qty
             - (v_item.price_paise * v_line.qty * v_line.granted_bps) / 10000,
           updated_at = now()
     where id = v_line.id;

    -- The biggest line names the order at Razorpay and on legacy screens that
    -- still expect one sku. The bill itself always comes from cart_items.
    if v_item.price_paise * v_line.qty > v_best then
      v_best := v_item.price_paise * v_line.qty;
      v_primary := v_line.sku;
      v_primary_qty := v_line.qty;
    end if;
  end loop;

  if v_lines = 0 then raise exception 'CART_EMPTY'; end if;

  v_total := v_gross - v_discount;
  if v_total < 100 then raise exception 'BELOW_MIN_ORDER_AMOUNT'; end if;
  if p_amount_paise is not null and p_amount_paise <> v_total then
    raise exception 'AMOUNT_MISMATCH'; end if;

  -- Weighted mean across the basket. Every line is already <= the ceiling, so
  -- the mean is too, and ck_granted_le_ceiling on slots still holds.
  v_eff_bps := case when v_gross > 0 then (v_discount * 10000) / v_gross else 0 end;

  if v_camp.spent_paise + v_camp.reserved_paise
       - v_sess.reserved_paise + v_discount > v_camp.budget_paise then
    raise exception 'BUDGET_EXCEEDED'; end if;

  update campaigns
     set reserved_paise = reserved_paise - v_sess.reserved_paise + v_discount
   where id = v_camp.id;

  if v_slot.sharing = 'once' then
    update slots
       set status='locked', granted_bps=v_eff_bps, discount_paise=v_discount,
           reserved_paise=v_discount, locked_at=now()
     where id = v_slot.id;
  end if;

  update sessions
     set status='offer_locked', current_sku=v_primary, current_qty=v_primary_qty,
         offer_bps=v_eff_bps, offer_discount_paise=v_discount,
         offer_amount_paise=v_total, rzp_order_id=p_rzp_order_id,
         reserved_paise=v_discount, updated_at=now()
   where id = p_session_id;

  update carts set status='ordered', updated_at=now() where id = v_cart_id;

  insert into decisions (campaign_id, slot_id, session_id, kind, code,
                         granted_bps, human_reason, customer_reason, meta)
  values (v_camp.id, v_slot.id, p_session_id, 'order_created', 'P01_ORDER_CREATED',
          v_eff_bps,
          format('Cart order %s created: %s lines, %s gross, %s discount, %s payable (effective %s bps).',
                 p_rzp_order_id, v_lines, (v_gross/100.0)::numeric(12,2),
                 (v_discount/100.0)::numeric(12,2), (v_total/100.0)::numeric(12,2),
                 v_eff_bps),
          'Basket locked. Opening checkout.',
          jsonb_build_object('rzp_order_id', p_rzp_order_id,
                             'cart_id', v_cart_id, 'lines', v_lines,
                             'gross_paise', v_gross, 'discount_paise', v_discount,
                             'total_paise', v_total, 'sharing', v_slot.sharing));

  return jsonb_build_object('ok', true, 'cart_id', v_cart_id,
                            'slot_id', v_slot.id, 'campaign_id', v_camp.id,
                            'lines', v_lines, 'gross_paise', v_gross,
                            'discount_paise', v_discount, 'total_paise', v_total,
                            'effective_bps', v_eff_bps,
                            'reserved_paise', v_discount);
end $$;

-- ------------------------------------------------------ release_reservation --
-- Unchanged except for the last two lines: a dismissed checkout puts the
-- basket back to 'open'.
--
-- Without that the cart stays 'ordered' after someone closes the payment
-- sheet, and while every reader here accepts both statuses, "ordered" would
-- keep claiming an order exists for a reservation that was just handed back.
-- The shopper is returned to exactly where they were: same basket, same
-- prices, free to keep shopping.
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
  -- Keyed off the SESSION, not the slot: a shared slot is never 'locked'.
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
  update carts set status='open', updated_at=now()
    where session_id = v_sess.id and status = 'ordered';

  insert into decisions (campaign_id, slot_id, session_id, kind, code,
                         human_reason, customer_reason)
  values (v_sess.campaign_id, v_slot.id, v_sess.id, 'payment_failed',
          'P02_RESERVATION_RELEASED',
          format('Reservation on order %s released: %s.', p_rzp_order_id, p_reason),
          'Payment did not go through. Your basket is still here.');
  return jsonb_build_object('released',true);
end $$;

-- =============================== privileges ===============================
-- MUST be last: create-or-replace re-grants EXECUTE to PUBLIC, and every
-- function here is SECURITY DEFINER.
do $$
declare
  fn record;
  ours text[] := array['cart_for_session','get_cart','upsert_cart_item',
                       'remove_cart_item','clear_cart','reserve_cart',
                       'release_reservation'];
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
