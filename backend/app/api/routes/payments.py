"""Customer-facing payment routes: accept, confirm, poll.

No merchant key. The session id is the credential, handed out only in exchange
for a slot token.

`accept` is the route that re-runs the gate. The model approved this offer
several seconds ago; between then and now the budget may have moved and other
slots may have redeemed, so `payment_service.accept()` re-evaluates
`bounds.check()` against live campaign state before reserving anything.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DbDep
from app.core import bounds, rzp
from app.services import payment_service
from app.services.payment_service import (
    BAD_REQUEST_CODES,
    CONFLICT_CODES,
    NOT_FOUND_CODES,
    PaymentError,
)

log = logging.getLogger("kirana.payments")

router = APIRouter(prefix="/api/v1", tags=["payments"])


class AcceptBody(BaseModel):
    sku: str = Field(min_length=1, max_length=32)
    qty: int = Field(default=1, ge=1, le=bounds.MAX_QTY)
    discount_bps: int = Field(ge=0, le=bounds.MAX_BPS)


class ConfirmBody(BaseModel):
    """The checkout handler's callback payload.

    razorpay_signature is optional because a stub-mode order has none, and
    because the webhook and polling paths reach settlement without one.
    """

    order_id: str = Field(min_length=4, max_length=64)
    payment_id: str = Field(min_length=4, max_length=64)
    signature: str | None = None


#: Raised when Razorpay is unconfigured and stub mode is not permitted --
#: which is exactly the state of production until the keys are set. Without
#: this, tapping Pay returns a 500 and the customer sees a crash instead of a
#: sentence.
NOT_CONFIGURED = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    detail={
        "code": "PAYMENTS_NOT_CONFIGURED",
        "message": "Card payment is not switched on for this shop yet. "
                   "Your offer is still open -- please pay at the counter.",
    },
)


def _http(exc: PaymentError) -> HTTPException:
    code = (
        status.HTTP_404_NOT_FOUND if exc.code in NOT_FOUND_CODES
        else status.HTTP_409_CONFLICT if exc.code in CONFLICT_CODES
        else status.HTTP_400_BAD_REQUEST if exc.code in BAD_REQUEST_CODES
        else status.HTTP_400_BAD_REQUEST
    )
    return HTTPException(status_code=code,
                         detail={"code": exc.code, "message": exc.message})


@router.post("/sessions/{session_id}/accept")
async def accept(session_id: str, body: AcceptBody, db: DbDep) -> dict:
    """Create the Razorpay order and reserve the discount."""
    try:
        return await payment_service.accept(
            db, session_id, body.sku, body.qty, body.discount_bps
        )
    except rzp.PaymentConfigError as exc:
        log.warning("accept refused: razorpay not configured")
        raise NOT_CONFIGURED from exc
    except PaymentError as exc:
        raise _http(exc) from exc


@router.post("/payments/confirm")
async def confirm(body: ConfirmBody, db: DbDep) -> dict:
    """The checkout handler path.

    Signature is verified with the API KEY SECRET (the webhook secret is a
    different value and belongs to a different route). A bad signature is
    rejected before settlement -- this is the one place a client-supplied
    payment id could otherwise be used to settle an order that was never paid.
    """
    try:
        verified = rzp.verify_checkout_signature(
            body.order_id, body.payment_id, body.signature or ""
        )
    except rzp.PaymentConfigError as exc:
        raise NOT_CONFIGURED from exc

    if not verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_SIGNATURE",
                    "message": "That payment could not be verified."},
        )

    row = await db.rpc("get_payment_status", {"p_rzp_order_id": body.order_id})
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"code": "ORDER_NOT_FOUND", "message": "No such order."},
        )
    # Settle for exactly what was reserved. Trusting an amount from the client
    # would let a tampered callback settle a large order for one rupee; the
    # plpgsql AMOUNT_MISMATCH check is the backstop, this is the first line.
    session = await db.rpc("get_session_context", {"p_session_id": row["session_id"]})
    amount = int((session or {}).get("session", {}).get("offer_amount_paise") or 0)

    try:
        settled = await payment_service.settle(
            db,
            order_id=body.order_id,
            payment_id=body.payment_id,
            signature=body.signature,
            amount_paise=amount,
            source="checkout_handler",
        )
    except PaymentError as exc:
        raise _http(exc) from exc
    return settled


@router.get("/payments/{order_id}/status")
async def payment_status(order_id: str, db: DbDep) -> dict:
    """Polling backup. Safe to call repeatedly; settles at most once."""
    try:
        return await payment_service.status(db, order_id)
    except PaymentError as exc:
        raise _http(exc) from exc


@router.post("/payments/{order_id}/release")
async def release(order_id: str, db: DbDep) -> dict:
    """Called when the customer dismisses checkout or the payment fails.

    Returns the reservation to the budget so an abandoned checkout does not
    quietly hold discount for the rest of the demo.
    """
    try:
        return await payment_service.release(db, order_id, "customer dismissed checkout")
    except PaymentError as exc:
        raise _http(exc) from exc
