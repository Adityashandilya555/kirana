"""The anon role must not be able to execute our functions.

This is a regression guard, not a unit test. Every function in sql/002 is
SECURITY DEFINER, which means it bypasses RLS -- so an EXECUTE grant to anon
is a direct path to nuke_demo() over PostgREST. Postgres re-grants EXECUTE to
PUBLIC on every `create or replace`, so this can silently regress any time
someone edits a function body.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

OUR_FUNCTIONS = [
    "ping", "health_check", "create_campaign", "commit_campaign", "get_campaign",
    "list_campaign_slots", "list_merchant_campaigns", "get_merchant_by_name",
    "get_session_context", "get_audit_feed", "reserve_slot", "settle_payment",
    "release_reservation", "verify_redemption", "reset_demo", "nuke_demo",
]

DESTRUCTIVE = {"nuke_demo", "reset_demo", "settle_payment", "commit_campaign"}


@pytest.fixture
async def conn():
    # settings reads backend/.env, which os.environ does not see.
    from app.core.config import settings

    dsn = os.environ.get("DATABASE_URL") or settings.DATABASE_URL
    if not dsn:
        pytest.skip("no DATABASE_URL (set it, or run `make db-reset`)")
    import asyncpg

    c = await asyncpg.connect(dsn)
    try:
        if not await c.fetchval("select 1 from pg_roles where rolname='anon'"):
            pytest.skip("no anon role (bare Postgres, not Supabase-shaped)")
        yield c
    finally:
        await c.close()


async def test_anon_cannot_execute_any_of_our_functions(conn):
    leaked = await conn.fetch(
        """
        select p.proname
          from pg_proc p join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public'
           and p.proname = any($1::text[])
           and has_function_privilege('anon', p.oid, 'EXECUTE')
        """,
        OUR_FUNCTIONS,
    )
    names = sorted(r["proname"] for r in leaked)
    assert not names, (
        f"anon can execute {names}. Re-run the privileges block at the end of "
        f"sql/002_functions.sql -- a `create or replace` re-granted EXECUTE to PUBLIC."
    )


async def test_destructive_functions_are_the_ones_that_matter(conn):
    """Named separately so a failure says *why* it is urgent."""
    leaked = await conn.fetch(
        """
        select p.proname
          from pg_proc p join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public' and p.proname = any($1::text[])
           and has_function_privilege('anon', p.oid, 'EXECUTE')
        """,
        sorted(DESTRUCTIVE),
    )
    assert not leaked, "anon can wipe or settle the campaign directly over PostgREST"


async def test_pgcrypto_is_left_alone(conn):
    """The revoke is scoped by name for a reason: gen_random_uuid() is a column
    default on six tables, and revoking it from PUBLIC breaks every insert."""
    assert await conn.fetchval(
        "select has_function_privilege('anon','public.gen_random_uuid()','EXECUTE')"
    ), "gen_random_uuid() was revoked -- inserts will fail for non-superusers"


async def test_health_check_reads_a_real_table(conn):
    row = await conn.fetchval("select health_check()")
    import json

    data = json.loads(row) if isinstance(row, str) else row
    assert data["catalog_items"] >= 1, (
        "health_check must fail when it cannot read; a probe that returns a "
        "literal cannot distinguish a wrong key from a right one"
    )
