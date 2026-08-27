-- Payment status and webhook bookkeeping.
--
-- Settlement itself already lives in settle_payment(). These are the two
-- supporting reads/writes the payment routes need and the schema had no way
-- to do: what the phone polls while it waits, and the dedupe record that
-- makes a retried webhook harmless.

-- --------------------------------------------------------- payment status --
-- Answers "has this order settled?" from OUR database, so the polling path
-- does not hit Razorpay on every tick. Returns settled=false rather than
-- nothing for an unknown order: the phone polls a few times before the
-- reservation row is visible, and a 404 there would look like failure.
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
    'redemption_token', sl.redemption_token,
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

-- ------------------------------------------------------- webhook bookkeeping --
-- Razorpay fires BOTH order.paid and payment.captured for one payment, and
-- retries on any non-2xx. Dedupe is on the event id it gives us, enforced by
-- uq_webhook_event_id -- so this returns duplicate=true rather than raising,
-- and the caller simply stops.
--
-- The row is written even when signature_ok is false. A forged or misdirected
-- webhook is exactly the thing you want a record of.
create or replace function public.log_webhook_event(
  p_event_id text,
  p_event_type text,
  p_signature_ok boolean,
  p_payload jsonb,
  p_rzp_order_id text default null,
  p_rzp_payment_id text default null
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_id uuid;
begin
  insert into webhook_events (event_id, event_type, rzp_order_id, rzp_payment_id,
                              signature_ok, payload)
  values (p_event_id, p_event_type, p_rzp_order_id, p_rzp_payment_id,
          p_signature_ok, coalesce(p_payload, '{}'::jsonb))
  on conflict (event_id) do nothing
  returning id into v_id;

  if v_id is null then
    return jsonb_build_object('ok', true, 'duplicate', true);
  end if;
  return jsonb_build_object('ok', true, 'duplicate', false, 'id', v_id);
end $$;

create or replace function public.mark_webhook_processed(
  p_event_id text, p_error text default null
) returns jsonb
language plpgsql security definer set search_path = public as $$
begin
  update webhook_events
     set processed = p_error is null, process_error = p_error
   where event_id = p_event_id;
  return jsonb_build_object('ok', true);
end $$;

-- =============================== privileges ===============================
-- MUST be last: the create-or-replace statements above re-granted EXECUTE to
-- PUBLIC on everything they touched.
do $$
declare
  fn record;
  ours text[] := array[
    'ping','health_check','create_campaign','commit_campaign','get_campaign',
    'list_campaign_slots','list_merchant_campaigns','get_merchant_by_name',
    'get_session_context','get_audit_feed','reserve_slot','settle_payment',
    'release_reservation','verify_redemption','reset_demo','nuke_demo',
    'open_session_by_token','append_session_turn','log_decision','get_redemption',
    'get_payment_status','log_webhook_event','mark_webhook_processed'
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
