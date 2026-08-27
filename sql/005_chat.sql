-- Session and audit plumbing for the chat turn.
--
-- Phase 2B needs three things the schema had no way to do: resolve a scanned
-- token into a session, append a turn, and write a decisions row. The data
-- layer is rpc()-only by design (see core/db.py), so each is a named function
-- rather than ad-hoc SQL in the application.
--
-- The interesting one is open_session_by_token. A phone reload must RESUME a
-- conversation, never fork it, so this is idempotent: it returns the existing
-- live session when there is one. The partial unique index
-- uq_one_live_session_per_slot already makes a second live row impossible, so
-- this function is the cooperative path to the same guarantee rather than the
-- thing enforcing it.

-- ------------------------------------------------ open_session_by_token ----
create or replace function public.open_session_by_token(
  p_slot_token text, p_transport text default 'web', p_transport_ref text default null
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_slot slots%rowtype; v_camp campaigns%rowtype; v_sess sessions%rowtype;
        v_resumed boolean := true;
begin
  select * into v_slot from slots where slot_token = upper(trim(p_slot_token));
  if not found then raise exception 'SLOT_NOT_FOUND'; end if;

  select * into v_camp from campaigns where id = v_slot.campaign_id;
  if v_camp.status <> 'live' then raise exception 'CAMPAIGN_NOT_LIVE'; end if;

  -- Resume. Any live session for this slot is THE session.
  select * into v_sess from sessions
   where slot_id = v_slot.id and status in ('open','offer_locked')
   order by created_at desc limit 1;

  if not found then
    if v_slot.status not in ('unused','offered') then
      raise exception 'SLOT_NOT_OPEN';
    end if;
    v_resumed := false;

    insert into sessions (slot_id, campaign_id, transport, transport_ref)
    values (v_slot.id, v_slot.campaign_id, p_transport, p_transport_ref)
    returning * into v_sess;

    update slots set status = 'offered'
     where id = v_slot.id and status = 'unused';

    insert into decisions (campaign_id, slot_id, session_id, kind, code,
                           human_reason, customer_reason)
    values (v_camp.id, v_slot.id, v_sess.id, 'session_opened', 'C02_SESSION_OPENED',
            format('Session opened on slot %s (ceiling %s bps, leaf %s).',
                   v_slot.slot_token, v_slot.ceiling_bps, v_slot.leaf_index),
            'Welcome! Ask me about anything on the shelf.');
  end if;

  return jsonb_build_object(
    'session_id', v_sess.id,
    'resumed', v_resumed,
    'context', get_session_context(v_sess.id)
  );
end $$;

-- ---------------------------------------------------- append_session_turn --
-- One transcript entry. p_bump_turn is false for assistant rows so a single
-- customer message costs exactly one turn no matter how many tool round-trips
-- it took to answer.
create or replace function public.append_session_turn(
  p_session_id uuid, p_role text, p_content text, p_bump_turn boolean default false
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_turns int;
begin
  if p_role not in ('user','assistant','system') then
    raise exception 'BAD_ROLE';
  end if;

  update sessions
     set transcript = transcript || jsonb_build_object(
           'role', p_role, 'content', p_content, 'at', now()),
         turn_count = turn_count + case when p_bump_turn then 1 else 0 end,
         updated_at = now()
   where id = p_session_id
   returning turn_count into v_turns;

  if not found then raise exception 'SESSION_NOT_FOUND'; end if;
  return jsonb_build_object('ok', true, 'turn_count', v_turns);
end $$;

-- ------------------------------------------------------------ log_decision --
-- The ONLY insert path into decisions from application code. Everything the
-- audit feed shows about a chat turn arrives through here.
--
-- llm_provider is nullable on purpose and must stay that way: an
-- injection_blocked row carries NULL there, and that null is the
-- machine-checkable proof that no model was invoked on a hostile message.
create or replace function public.log_decision(
  p_campaign_id uuid,
  p_kind text,
  p_code text,
  p_human_reason text,
  p_slot_id uuid default null,
  p_session_id uuid default null,
  p_turn_index int default null,
  p_proposed_bps int default null,
  p_granted_bps int default null,
  p_binding_constraint text default null,
  p_customer_reason text default null,
  p_llm_provider text default null,
  p_llm_model text default null,
  p_latency_ms int default null,
  p_raw_user_message text default null,
  p_raw_llm_output text default null,
  p_meta jsonb default '{}'::jsonb
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_id bigint;
begin
  insert into decisions (
    campaign_id, slot_id, session_id, turn_index, kind, code,
    proposed_bps, granted_bps, binding_constraint, human_reason,
    customer_reason, llm_provider, llm_model, latency_ms,
    raw_user_message, raw_llm_output, meta
  ) values (
    p_campaign_id, p_slot_id, p_session_id, p_turn_index, p_kind, p_code,
    p_proposed_bps, p_granted_bps, p_binding_constraint, p_human_reason,
    p_customer_reason, p_llm_provider, p_llm_model, p_latency_ms,
    p_raw_user_message, p_raw_llm_output, coalesce(p_meta, '{}'::jsonb)
  ) returning id into v_id;
  return jsonb_build_object('ok', true, 'id', v_id);
end $$;

-- =============================== privileges ===============================
-- MUST be last. Postgres grants EXECUTE to PUBLIC on every newly created
-- function, so the three `create or replace` statements above just re-opened
-- themselves to anon. Re-close them, and re-close everything else while we
-- are here so this file is safe to run on its own.
do $$
declare
  fn record;
  ours text[] := array[
    'ping','health_check','create_campaign','commit_campaign','get_campaign',
    'list_campaign_slots','list_merchant_campaigns','get_merchant_by_name',
    'get_session_context','get_audit_feed','reserve_slot','settle_payment',
    'release_reservation','verify_redemption','reset_demo','nuke_demo',
    'open_session_by_token','append_session_turn','log_decision'
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
