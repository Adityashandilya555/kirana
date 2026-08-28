"""Shared FastAPI dependencies."""

import hmac
import logging
import time
from collections import OrderedDict, deque
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from app.core.config import settings
from app.core.db import DbBackend

log = logging.getLogger("kirana.auth")

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"code": "BAD_MERCHANT_KEY", "message": "Invalid X-Merchant-Key."},
)

# A shared static key with no throttle is brute-forceable, and this is the only
# credential in the system. Deliberately in-process and approximate: one
# container, one shop, and a real limiter is a dependency this does not need.
#
# PER CALLER, not global. A single process-wide counter meant any unauthenticated
# caller could send _FAIL_LIMIT bad requests and lock the shopkeeper out of their
# own console for the whole window -- turning a brute-force defence into a
# trivial denial of service, which is a strictly worse trade. Buckets are keyed
# by client so one attacker can only ever throttle themselves.
#
# The key itself is 288 bits of entropy, so brute force was never the real
# threat; this exists so that a shop which later sets a weak key is not
# defenceless. That is also why the limit is generous.
_FAIL_WINDOW_S = 60.0
_FAIL_LIMIT = 20
#: Cap the number of tracked callers. Without it, an attacker rotating spoofed
#: X-Forwarded-For values would grow this map without bound -- the same memory
#: exhaustion the throttle is meant to prevent, one level up.
_MAX_TRACKED = 2_048
_failures: OrderedDict[str, deque[float]] = OrderedDict()


def _caller(request: Request) -> str:
    """Best-effort client identity.

    X-Forwarded-For is set by Railway's proxy and is spoofable, which is
    tolerable here: spoofing only lets an attacker evade their OWN throttle,
    and evading a limit on yourself is not an attack. What matters is that one
    caller cannot consume another caller's budget.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _prune(bucket: deque[float], now: float) -> None:
    while bucket and now - bucket[0] > _FAIL_WINDOW_S:
        bucket.popleft()


def _record_failure(caller: str) -> None:
    now = time.monotonic()
    bucket = _failures.get(caller)
    if bucket is None:
        if len(_failures) >= _MAX_TRACKED:
            _failures.popitem(last=False)  # evict the least recently seen
        bucket = deque()
        _failures[caller] = bucket
    _failures.move_to_end(caller)
    bucket.append(now)
    _prune(bucket, now)


def _locked_out(caller: str) -> bool:
    bucket = _failures.get(caller)
    if bucket is None:
        return False
    _prune(bucket, time.monotonic())
    if not bucket:
        del _failures[caller]
        return False
    return len(bucket) >= _FAIL_LIMIT


async def require_merchant_key(
    request: Request, x_merchant_key: str = Header(default=""),
) -> str:
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

    caller = _caller(request)

    if _locked_out(caller):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "TOO_MANY_ATTEMPTS",
                "message": "Too many failed attempts. Try again in a minute.",
            },
        )

    if not hmac.compare_digest(x_merchant_key, settings.MERCHANT_API_KEY):
        _record_failure(caller)
        raise _UNAUTHORIZED

    # A success clears this caller's history: the shopkeeper who fat-fingers a
    # paste twice and then gets it right should not carry those failures.
    _failures.pop(caller, None)
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
