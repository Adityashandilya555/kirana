-- The column migration 011 describes but never contained.
--
-- 011_scope_snapshot.sql is prose. Its last line -- "(full statements applied
-- as migration 015_scope_snapshot; see supabase)" -- says the SQL went
-- straight into Supabase and was never written down, and 016's header repeats
-- it. The consequence was invisible for as long as nobody replayed the
-- directory: sessions.scope_snapshot exists in production and in no local
-- database, so `make db-reset` fails at 024, which is the first file to
-- reference the column from a SQL-language function body -- the plpgsql ones
-- in 016 and 017 are not parsed until they run, so they "succeed" against a
-- schema that cannot execute them.
--
-- This is the column, written down, idempotent. Applying it to Supabase where
-- migration 015_scope_snapshot already ran is a no-op. It closes the drift for
-- the one thing that actually blocked a local replay; the rest of what that
-- migration did to get_session_audit was superseded wholesale by 024, which
-- dumps its own body.
--
-- What it holds: the visible/withheld sku lists computed at the moment the
-- session opened, by the same predicate get_session_context uses to build the
-- catalogue it hands the model. The point of snapshotting it is that the
-- console presents it as evidence, and evidence derived from mutable shelves
-- is not evidence.

alter table sessions
  add column if not exists scope_snapshot jsonb;
