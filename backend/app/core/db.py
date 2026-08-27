"""Database access, behind one tiny surface.

Every write that must be atomic lives in a plpgsql function, because
supabase-py speaks PostgREST over HTTP where each .execute() is its own
transaction. That constraint turns out to be a gift: the entire data layer
is `rpc(name, params)`, which is small enough to have two implementations.

  PostgresBackend  asyncpg straight at Postgres. Local dev and integration
                   tests -- no network, no credentials, no PostgREST.
  SupabaseBackend  PostgREST. What runs on Railway.

Application code never learns which one it has. Picking a backend is the
last decision made at startup and the first one forgotten.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from app.core.config import settings

# plpgsql `raise exception 'SLOT_ALREADY_REDEEMED'` reaches us as prose with
# the token embedded. Pull it back out so callers branch on a code instead of
# substring-matching an error message.
_PG_CODE = re.compile(r"\b([A-Z][A-Z0-9_]{4,})\b")


class RpcError(RuntimeError):
    def __init__(self, fn: str, code: str, message: str) -> None:
        super().__init__(f"{fn}: {code}: {message}")
        self.fn = fn
        self.code = code
        self.message = message

    @classmethod
    def from_exception(cls, fn: str, exc: Exception) -> "RpcError":
        raw = getattr(exc, "message", None) or str(exc)
        match = _PG_CODE.search(raw)
        return cls(fn, match.group(1) if match else "RPC_FAILED", raw)


class DbBackend(ABC):
    name: str

    @abstractmethod
    async def rpc(self, fn: str, params: dict[str, Any] | None = None) -> Any: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def health(self) -> dict[str, Any]:
        """Prove we can actually READ, not merely connect.

        The old ping() returned a literal from an unprivileged function, so a
        backend misconfigured with the anon key reported a full green while
        every table read silently returned [] under RLS. health_check() reads
        a real table and is revoked from anon, so a wrong key raises here
        instead of lying.
        """
        result = await self.rpc("health_check")
        if not isinstance(result, dict) or result.get("catalog_items", 0) < 1:
            raise RpcError(
                "health_check",
                "EMPTY_DATABASE",
                f"connected, but the catalog is empty: {result!r}. "
                "Either the seed never ran, or this key cannot read the tables.",
            )
        return result


class PostgresBackend(DbBackend):
    """Direct asyncpg. Used when DATABASE_URL is set."""

    name = "postgres"

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "PostgresBackend":
        import asyncpg

        async def init(conn: Any) -> None:
            # asyncpg hands back jsonb as raw text unless told otherwise, and
            # every one of our functions returns jsonb.
            for typename in ("json", "jsonb"):
                await conn.set_type_codec(
                    typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
                )

        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8, init=init)
        return cls(pool)

    async def rpc(self, fn: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        keys = list(params)
        # Named notation, matching how PostgREST invokes these functions --
        # so argument order can never drift between the two backends.
        # Structured values are serialised and explicitly cast: Postgres will
        # not implicitly promote text to jsonb when resolving a named argument,
        # so without the cast the call fails to match the function signature.
        fragments, values = [], []
        for i, k in enumerate(keys, start=1):
            value = params[k]
            if isinstance(value, (dict, list)):
                # The connection carries a jsonb codec, so asyncpg serialises
                # this itself. Pre-dumping here would encode it twice and the
                # function would receive a JSON string instead of an array.
                fragments.append(f"{k} => ${i}::jsonb")
                values.append(value)
            else:
                fragments.append(f"{k} => ${i}")
                values.append(value)
        sql = f"select public.{fn}({', '.join(fragments)})"
        try:
            async with self._pool.acquire() as conn:
                return await conn.fetchval(sql, *values)
        except Exception as exc:  # noqa: BLE001 - normalise every backend error
            raise RpcError.from_exception(fn, exc) from exc

    async def close(self) -> None:
        await self._pool.close()


class SupabaseBackend(DbBackend):
    """PostgREST via supabase-py. What runs in production."""

    name = "supabase"

    def __init__(self, client: Any) -> None:
        self.client = client

    @classmethod
    async def connect(cls, url: str, key: str) -> "SupabaseBackend":
        from supabase import acreate_client

        return cls(await acreate_client(url, key))

    async def rpc(self, fn: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self.client.rpc(fn, params or {}).execute()
        except Exception as exc:  # noqa: BLE001
            raise RpcError.from_exception(fn, exc) from exc
        return resp.data

    async def close(self) -> None:
        return None


async def create_db_backend() -> DbBackend:
    if settings.DATABASE_URL:
        return await PostgresBackend.connect(settings.DATABASE_URL)
    if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY:
        return await SupabaseBackend.connect(
            settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY
        )
    raise RuntimeError(
        "No database configured. Set DATABASE_URL for local Postgres, or "
        "SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY for the hosted project."
    )
