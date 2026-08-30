-- reserve_slot re-checks everything the gate checked.
--
-- The database has always re-checked the slot ceiling, the campaign maximum
-- and the budget. It has never re-checked the margin floor -- not from
-- oversight, but because it could not: the floor is enforced by a binary
-- search in Python over price and cost, and until per-product caps were
-- committed there was no stored number for SQL to compare against.
--
-- Now there is, so all three of the remaining bounds can be enforced at the
-- last moment before money is reserved:
--
--   PRODUCT_CAP_VIOLATION    the committed per-sku ceiling
--   CUSTOMER_TIER_VIOLATION  the band this shopper was placed in at scan time
--   MARGIN_FLOOR_VIOLATION   the floor itself, checkable in SQL for the first
--                            time
--
-- This is STRICTLY A TIGHTENING. Anything raised here was already refused in
-- Python by bounds.check(); if one of these ever fires in production it means
-- the two disagreed, which is exactly the thing worth finding out about. Being
-- a tightening is also what makes it safe to ship and safe to revert.

-- ------------------------------------------------------------------------
-- margin_bps_after -- the SQL twin of bounds.margin_bps_after.
--
-- Two rounding traps, both deliberate:
--
--   * The sale price uses plain integer division. Both operands are positive
--     there, so Postgres truncation and Python floor division agree.
--
--   * The margin itself uses floor() over numeric, NOT integer division. When
--     cost exceeds the sale price the numerator is negative, and Postgres `/`
--     truncates toward zero while Python `//` floors away from it -- so
--     -5000001/1000 is -5000 in SQL and -5001 in Python. The decision would
--     usually survive that (both are below any floor >= 0), but a twin that is
--     only usually right is not a twin. test_margin_parity.py walks a grid and
--     asserts the two agree exactly.
-- ------------------------------------------------------------------------
create or replace function public.margin_bps_after(
  p_price_paise bigint, p_cost_paise bigint, p_discount_bps int
) returns int
language plpgsql immutable set search_path = public as $$
declare v_sale bigint;
begin
  v_sale := (p_price_paise * (10000 - p_discount_bps)) / 10000;
  if v_sale <= 0 then
    return -10000;
  end if;
  return floor(((v_sale - p_cost_paise)::numeric * 10000) / v_sale)::int;
end $$;

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
  if v_slot.status = 'redeemed' then raise exception 'SLOT_ALREADY_REDEEMED'; end if;
  if v_slot.status = 'void'     then raise exception 'SLOT_VOID';             end if;
  if p_discount_bps > v_slot.ceiling_bps then raise exception 'CEILING_VIOLATION'; end if;
  if p_amount_paise < 100 then raise exception 'BELOW_MIN_ORDER_AMOUNT'; end if;

  select * into v_camp from campaigns where id = v_sess.campaign_id for update;
  if v_camp.status <> 'live' then raise exception 'CAMPAIGN_NOT_LIVE'; end if;
  if p_discount_bps > v_camp.max_discount_bps
    then raise exception 'CAMPAIGN_MAX_VIOLATION'; end if;

  -- The committed per-product ceiling. NULL on a campaign that predates caps,
  -- and NULL means "not applied" -- never "a cap of zero", which would make
  -- every legacy campaign refuse every order.
  select cap_bps into v_cap from campaign_product_caps
   where campaign_id = v_camp.id and sku = upper(trim(p_sku));
  if v_cap is not null and p_discount_bps > v_cap
    then raise exception 'PRODUCT_CAP_VIOLATION'; end if;

  -- The band this shopper was placed in when they scanned, resolved against
  -- the product cap. Read from the session snapshot rather than recomputed:
  -- a tier that moves between the offer and the checkout would let someone be
  -- refused for a number they were legitimately quoted.
  if v_cap is not null and v_sess.tier_cap_fraction_bps is not null then
    v_customer_cap := (v_cap * v_sess.tier_cap_fraction_bps) / 10000;
    if p_discount_bps > v_customer_cap
      then raise exception 'CUSTOMER_TIER_VIOLATION'; end if;
  end if;

  -- The margin floor, enforced in the database for the first time.
  select * into v_item from catalog_items
   where merchant_id = v_camp.merchant_id and sku = upper(trim(p_sku));
  if found and margin_bps_after(v_item.price_paise, v_item.cost_paise,
                                p_discount_bps) < v_camp.margin_floor_bps
    then raise exception 'MARGIN_FLOOR_VIOLATION'; end if;

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
          jsonb_build_object('rzp_order_id',p_rzp_order_id,
                             'product_cap_bps',v_cap,
                             'customer_cap_bps',v_customer_cap));

  return jsonb_build_object('ok',true,'slot_id',v_slot.id,'campaign_id',v_camp.id,
                            'reserved_paise',p_discount_paise);
end $$;

-- =============================== privileges ===============================
do $$
declare
  fn record;
  ours text[] := array['reserve_slot','margin_bps_after'];
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
