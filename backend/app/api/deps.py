"""Shared FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from app.core.config import settings
from app.core.db import DbBackend


async def require_merchant_key(x_merchant_key: str = Header(default="")) -> str:
    """The only auth in the system. Multi-tenant auth is explicitly out of
    scope -- one hardcoded merchant, one shared key."""
    if x_merchant_key != settings.MERCHANT_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "BAD_MERCHANT_KEY", "message": "Invalid X-Merchant-Key."},
        )
    return x_merchant_key


async def get_db(request: Request) -> DbBackend:
    """The database backend built once in the app lifespan."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "DB_UNAVAILABLE", "message": "Database is not configured."},
        )
    return db


DbDep = Annotated[DbBackend, Depends(get_db)]
MerchantKey = Annotated[str, Depends(require_merchant_key)]
