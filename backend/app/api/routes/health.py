"""Health endpoints.

Deliberately two of them:

  /health       static, no DB. This is what railway.json points at, so a
                Supabase blip can never fail a Railway deploy healthcheck.
  /health/deep  actually touches Postgres. This is what you hit from the
                phone to prove the whole chain is wired.
"""

from fastapi import APIRouter, Depends, Request

from app.api.deps import require_merchant_key
from app.core import db as dbmod
from app.core import llm
from app.core.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "kirana-agent", "env": settings.APP_ENV}


@router.get("/health/deep")
async def health_deep(request: Request) -> dict:
    client = getattr(request.app.state, "db", None)
    if client is None:
        return {"ok": False, "db": "unconfigured", "env": settings.APP_ENV}
    try:
        alive = await dbmod.ping(client)
        return {"ok": alive, "db": "up" if alive else "down", "env": settings.APP_ENV}
    except Exception as exc:  # noqa: BLE001 - a probe must never 500
        return {
            "ok": False,
            "db": "down",
            "env": settings.APP_ENV,
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }


@router.get("/health/llm", dependencies=[Depends(require_merchant_key)])
async def health_llm() -> dict:
    return await llm.health_report()
