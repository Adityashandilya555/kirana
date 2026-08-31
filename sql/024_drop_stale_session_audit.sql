-- 024: remove the stale get_session_audit overload.
--
-- WHAT BROKE
--
-- The campaign postmortem returned 500 in production, and the browser
-- reported it as a CORS failure:
--
--   Access to fetch at '.../campaigns/<id>/postmortem' has been blocked by
--   CORS policy: No 'Access-Control-Allow-Origin' header is present
--
-- CORS was never the problem. An unhandled 500 is produced by Starlette's
-- outermost error middleware, which sits OUTSIDE CORSMiddleware and therefore
-- sends no Access-Control-Allow-Origin header; the browser has no way to
-- describe that except as a CORS violation. A 401 from the same route came
-- back WITH the header, which is what ruled CORS out.
--
-- The 500 underneath it was this:
--
--   ERROR 42725: function public.get_session_audit(uuid) is not unique
--
-- Two overloads existed at once:
--
--   get_session_audit(uuid)                       -- 010_audit.sql, capped 100
--   get_session_audit(uuid, int, int)             -- migration 015, paginated,
--                                                    p_limit/p_offset DEFAULT
--                                                    500/0
--
-- Because the second one defaults both page arguments, a call supplying only
-- p_campaign_id is a candidate for BOTH, and neither Postgres nor PostgREST
-- can choose. PostgREST answers 300 Multiple Choices. advisor.postmortem was
-- the one caller still passing a single argument; the audit route passes all
-- three and has always resolved cleanly, which is why only this one screen
-- failed.
--
-- This is the hazard named in the plan's risk 3, and it arrived exactly the
-- predicted way: migration 015 used `create or replace` with a changed
-- parameter list, which does not replace anything -- it adds an overload.
-- 009_shelves.sql makes the same point about slot leaves. The rule holds:
-- changing a function's signature means dropping the old one by name.
--
-- WHY THE FILE ALSO REDEFINES THE PAGINATED VERSION
--
-- sql/011_scope_snapshot.sql carries no SQL -- its statements were applied
-- directly as Supabase migration 015 -- so these files cannot rebuild the
-- schema that production is actually running, and a fresh database gets the
-- 100-row version with no scope_snapshot support. The body below is dumped
-- verbatim from production with pg_get_functiondef, so applying this file to
-- either a fresh or a live database converges them on the same definition.
--
-- Idempotent: `drop function if exists` by exact signature, then create or
-- replace. Safe to re-run, and safe to apply while the old backend is still
-- deployed -- that build's one-argument call then resolves to the paginated
-- function instead of failing to resolve at all.

-- ------------------------------------------------------------- the drop --
-- By exact signature. Dropping by bare name would be ambiguous here for the
-- very reason this migration exists.
drop function if exists public.get_session_audit(uuid);

-- ------------------------------------------- the one that should remain --
create or replace function public.get_session_audit(
  p_campaign_id uuid,
  p_limit       integer default 500,
  p_offset      integer default 0
)
returns jsonb
language sql
stable
security definer
set search_path to 'public'
as $function$
  with sess as (
    select se.*, sl.slot_token, sl.ceiling_bps, sl.status as slot_status,
           sl.bound_sku, sl.shelf_id, c.merchant_id
      from sessions se
      join slots sl on sl.id = se.slot_id
      join campaigns c on c.id = se.campaign_id
     where se.campaign_id = p_campaign_id
     order by se.created_at desc
     limit least(coalesce(p_limit, 500), 1000)
    offset greatest(coalesce(p_offset, 0), 0)
  )
  select jsonb_build_object(
    'total', (select count(*) from sessions where campaign_id = p_campaign_id),
    'returned', (select count(*) from sess),
    'offset', greatest(coalesce(p_offset, 0), 0),
    'sessions', coalesce((
      select jsonb_agg(jsonb_build_object(
        'session_id',   s.id,
        'started_at',   s.created_at,
        'status',       s.status,
        'turn_count',   s.turn_count,
        'slot_token',   s.slot_token,
        'ceiling_bps',  s.ceiling_bps,
        'slot_status',  s.slot_status,
        'bound_sku',    s.bound_sku,
        'shelf_name',   (select sh.name from shelves sh where sh.id = s.shelf_id),
        'sku',          s.current_sku,
        'qty',          s.current_qty,
        'offer_bps',    s.offer_bps,
        'amount_paise', s.offer_amount_paise,
        'scope_recorded', s.scope_snapshot is not null,
        'visible_skus', coalesce(
          s.scope_snapshot->'visible',
          (select coalesce(jsonb_agg(ci.sku order by ci.sku), '[]'::jsonb)
             from catalog_items ci
            where ci.merchant_id = s.merchant_id and ci.active
              and (s.bound_sku is not null and ci.sku = s.bound_sku
                   or s.bound_sku is null and s.shelf_id is not null
                      and exists (select 1 from shelf_items si
                                   where si.shelf_id = s.shelf_id and si.sku = ci.sku)
                   or s.bound_sku is null and s.shelf_id is null))),
        'withheld_skus', coalesce(
          s.scope_snapshot->'withheld',
          (select coalesce(jsonb_agg(ci.sku order by ci.sku), '[]'::jsonb)
             from catalog_items ci
            where ci.merchant_id = s.merchant_id and ci.active
              and not (s.bound_sku is not null and ci.sku = s.bound_sku
                   or s.bound_sku is null and s.shelf_id is not null
                      and exists (select 1 from shelf_items si
                                   where si.shelf_id = s.shelf_id and si.sku = ci.sku)
                   or s.bound_sku is null and s.shelf_id is null)))
      ) order by s.created_at desc)
      from sess s
    ), '[]'::jsonb)
  );
$function$;

-- =============================== privileges ===============================
-- MUST be last: create-or-replace re-grants EXECUTE to PUBLIC.
do $$
declare
  fn record;
begin
  for fn in
    select p.oid::regprocedure as sig
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.proname = 'get_session_audit'
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

-- ------------------------------------------------------------- the check --
-- Exactly one overload must remain. Without this the migration could appear
-- to succeed while leaving the ambiguity that caused the outage.
do $$
declare
  n int;
begin
  select count(*) into n
    from pg_proc p join pg_namespace ns on ns.oid = p.pronamespace
   where ns.nspname = 'public' and p.proname = 'get_session_audit';
  if n <> 1 then
    raise exception 'get_session_audit still has % overloads; expected 1', n;
  end if;
end $$;
