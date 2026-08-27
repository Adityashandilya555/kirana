"""Merchant-side redemption verification.

Behind the merchant key: this is the counter-side action that burns a code,
and the first scan is irreversible. A customer holding a redemption token must
not be able to spend it themselves by hitting this endpoint.

The customer's own phone proves the same commitment a different way -- it
replays the inclusion proof in the browser with the TS twin of merkle.py,
which reads but never burns.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import DbDep, MerchantKey
from app.core import ids
from app.core.db import RpcError
from app.services import qr_service, verify_service

router = APIRouter(prefix="/api/v1", tags=["verify"])


class VerifyBody(BaseModel):
    # Accepts a raw scan payload ("KIRANA1:<token>") or a hand-typed code;
    # verify_service folds both to canonical form.
    token: str = Field(min_length=1, max_length=64)


@router.post("/verify")
async def verify(body: VerifyBody, db: DbDep, _: MerchantKey) -> dict:
    """Burn-once. The first call is green; every later call is red."""
    try:
        return await verify_service.verify(db, body.token)
    except RpcError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@router.get("/redemption/{token}")
async def redemption_view(token: str, db: DbDep) -> dict:
    """Read-only view for the CUSTOMER's own screen.

    No merchant key, and it must never burn the code -- so it deliberately
    does NOT call verify_redemption. It returns the commitment fields the
    browser needs to replay the proof itself, plus what the customer already
    knows (their own discount and what they paid).
    """
    canonical = verify_service.canonical_redemption_token(token) or token
    row = await db.rpc("get_redemption", {"p_redemption_token": canonical})
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"code": "UNKNOWN_TOKEN", "message": "No such redemption code."},
        )
    return row


@router.get("/redemption/{token}/qr.svg")
async def redemption_qr(token: str) -> Response:
    """The QR the merchant scans off the customer's screen.

    Rendered server-side with segno rather than shipping a QR library to the
    phone: the payload is tiny, the customer's page is the one thing that must
    load instantly on a bad connection, and segno is already a dependency for
    the printed sheet.
    """
    canonical = verify_service.canonical_redemption_token(token)
    if not canonical:
        raise HTTPException(
            status_code=400,
            detail={"code": "BAD_TOKEN", "message": "Not a redemption code."},
        )
    svg = qr_service.qr_svg(ids.redemption_payload(canonical), scale=6)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        # Immutable: a redemption token never changes payload.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
