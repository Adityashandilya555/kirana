"""Supabase access.

supabase-py speaks PostgREST over HTTP, so every .execute() is its own
implicit transaction. Anything that must be atomic -- above all the
settlement -- lives in a plpgsql function and is called through rpc().
Do not try to sequence multi-table writes from Python.

The client is expensive to construct and is created once in the app
lifespan, not per request.
"""

from __future__ import annotations

import re
from typing import Any

from supabase import AsyncClient, acreate_client

from app.core.config import settings

# plpgsql `raise exception 'SLOT_ALREADY_REDEEMED'` surfaces as a PostgREST
# error whose message embeds the token. Pull it back out so callers can
# branch on a code instead of substring-matching prose.
_PG_CODE = re.compile(r"\b([A-Z][A-Z0-9_]{4,})\b")


class RpcError(RuntimeError):
    def __init__(self, fn: str, code: str, message: str) -> None:
        super().__init__(f"{fn}: {code}: {message}")
        self.fn = fn
        self.code = code
        self.message = message


async def create_db_client() -> AsyncClient:
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set. "
            "Copy backend/.env.example to backend/.env and fill them in."
        )
    return await acreate_client(
        settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY
    )


async def rpc(client: AsyncClient, fn: str, params: dict[str, Any] | None = None) -> Any:
    """Call a Postgres function, raising RpcError with the plpgsql code."""
    try:
        resp = await client.rpc(fn, params or {}).execute()
    except Exception as exc:  # noqa: BLE001 - normalise every backend error
        raw = getattr(exc, "message", None) or str(exc)
        match = _PG_CODE.search(raw)
        raise RpcError(fn, match.group(1) if match else "RPC_FAILED", raw) from exc
    return resp.data


async def ping(client: AsyncClient) -> bool:
    """Cheapest possible round trip to Postgres. Backed by sql/002."""
    data = await rpc(client, "ping")
    return data == "pong"
