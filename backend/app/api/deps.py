"""Shared FastAPI dependencies."""

from fastapi import Header, HTTPException, status

from app.core.config import settings


async def require_merchant_key(x_merchant_key: str = Header(default="")) -> str:
    """The only auth in the system. Multi-tenant auth is explicitly out of
    scope -- one hardcoded merchant, one shared key."""
    if x_merchant_key != settings.MERCHANT_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "BAD_MERCHANT_KEY", "message": "Invalid X-Merchant-Key."},
        )
    return x_merchant_key
