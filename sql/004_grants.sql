-- Standalone privilege migration for the live project.
--
-- 001-003 are already applied there, so all 16 of our SECURITY DEFINER
-- functions still carry the default EXECUTE-to-PUBLIC grant. Because a
-- SECURITY DEFINER function bypasses RLS, anyone holding the anon key could
-- call nuke_demo() over PostgREST and wipe the campaign.
--
-- The same block is appended to 002_functions.sql so that re-running that
-- file re-closes what its own `create or replace` statements re-open. The
-- duplication is deliberate: each file must be safe applied on its own.

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

-- Verify afterwards -- this should return zero rows:
--   select p.proname
--     from pg_proc p join pg_namespace n on n.oid = p.pronamespace
--    where n.nspname = 'public'
--      and p.proname in ('nuke_demo','reset_demo','settle_payment')
--      and has_function_privilege('anon', p.oid, 'EXECUTE');
