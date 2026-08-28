"""Shared FastAPI dependencies."""

import hmac
import logging
import time
from collections import deque
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from app.core.config import settings
from app.core.db import DbBackend

log = logging.getLogger("kirana.auth")

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"code": "BAD_MERCHANT_KEY", "message": "Invalid X-Merchant-Key."},
)

# A shared key with no throttle is brute-forceable at host speed, and this is
# the only credential in the system. Deliberately in-process and approximate:
# one container, one shop, and a real limiter is a dependency this does not
# need. Counts failures only, so ordinary console use is never throttled.
_FAIL_WINDOW_S = 60.0
_FAIL_LIMIT = 20
_failures: deque[float] = deque()


def _record_failure() -> None:
    now = time.monotonic()
    _failures.append(now)
    while _failures and now - _failures[0] > _FAIL_WINDOW_S:
        _failures.popleft()


def _locked_out() -> bool:
    now = time.monotonic()
    while _failures and now - _failures[0] > _FAIL_WINDOW_S:
        _failures.popleft()
    return len(_failures) >= _FAIL_LIMIT


async def require_merchant_key(x_merchant_key: str = Header(default="")) -> str:
    """The only auth in the system. Multi-tenant auth is explicitly out of
    scope -- one hardcoded merchant, one shared key.

    Three things this does beyond comparing strings:

      * Refuses outright when no key is configured. The setting now defaults
        to empty, so an unconfigured deploy denies everything rather than
        accepting a default published in this repository.
      * Compares with hmac.compare_digest. The timing channel is largely
        theoretical over a network, but the cost of not doing it is zero.
      * Throttles after repeated failures, because a shared static key with no
        rate limit is brute-forceable.
    """
    if not settings.MERCHANT_API_KEY:
        log.error("MERCHANT_API_KEY is not configured; refusing all merchant requests")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "MERCHANT_KEY_UNSET",
                "message": "This shop's console is not configured.",
            },
        )

    if _locked_out():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "TOO_MANY_ATTEMPTS",
                "message": "Too many failed attempts. Try again in a minute.",
            },
        )

    if not hmac.compare_digest(x_merchant_key, settings.MERCHANT_API_KEY):
        _record_failure()
        raise _UNAUTHORIZED
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
