"""Customer-facing payment routes: cart, checkout, confirm, poll.

No merchant key. The session id is the credential, handed out only in exchange
for a slot token.

`checkout` is the route a phone calls, and it is the one that re-runs the gate
-- for every line in the basket. The model approved those lines a conversation
ago; between then and now the budget may have moved and other slots may have
redeemed, so `payment_service.checkout()` re-evaluates `bounds.check()` against
live campaign state before reserving anything, and `reserve_cart` does it a
second time in SQL inside the transaction that actually moves the money.

`accept` is the older single-item form. It is what the machine-buyer flow uses
and what the payment tests pin, and it re-gates exactly the same way.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DbDep
from app.core import bounds, rzp
from app.core.db import RpcError
from app.services import cart_service, payment_service
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


class RemoveBody(BaseModel):
    sku: str = Field(min_length=1, max_length=32)


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


def _captured_upstream(order_id: str, payment_id: str) -> bool:
    """Does Razorpay itself say this payment was captured against this order?

    Deliberately narrow: the payment id must match one Razorpay lists for
    THIS order, and its status must be `captured`. An order id alone proves
    nothing -- it is visible in the checkout sheet -- and neither does a
    payment id on its own. The pair, confirmed by the provider, does.

    Returns False rather than raising on any upstream problem, so a Razorpay
    outage degrades to the ordinary BAD_SIGNATURE refusal and the polling path
    instead of a 500 in front of a customer who has just paid.
    """
    if rzp.stub_mode():
        return False
    try:
        payments = rzp.order_payments(order_id)
    except Exception:  # noqa: BLE001 - a provider blip must not 500 here
        log.exception("confirm: could not reach Razorpay for order %s", order_id)
        return False
    return any(
        p.get("id") == payment_id and p.get("status") == "captured"
        for p in payments
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


@router.get("/sessions/{session_id}/cart")
async def read_cart(session_id: str, db: DbDep) -> dict:
    """The basket. Safe to poll; it is a read.

    No merchant key and no ownership check beyond the session id, which is the
    credential everywhere else on this router: a uuid handed out only in
    exchange for a slot token. What it discloses is what the holder of that
    uuid negotiated themselves.
    """
    return await cart_service.load(db, session_id)


@router.post("/sessions/{session_id}/cart/remove")
async def remove_from_cart(session_id: str, body: RemoveBody, db: DbDep) -> dict:
    """Take a line out of the basket from the UI rather than by asking.

    Removing is safe to expose and adding is not: an add carries a discount,
    and the only thing allowed to decide a discount is the gate. A shopper who
    wants something added asks the assistant, which proposes, and the gate
    answers. There is no route that writes a rate the client chose.
    """
    try:
        return cart_service.normalise(await db.rpc("remove_cart_item", {
            "p_session_id": session_id, "p_sku": body.sku,
        }))
    except RpcError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@router.post("/sessions/{session_id}/checkout")
async def checkout(session_id: str, db: DbDep) -> dict:
    """One Razorpay order for the whole basket.

    No body: the basket is server-side state and the client does not get to
    describe it. Sending line items from the phone would mean trusting a client
    for both the products and their rates, which is the entire thing the gate
    exists to prevent.
    """
    try:
        return await payment_service.checkout(db, session_id)
    except rzp.PaymentConfigError as exc:
        log.warning("checkout refused: razorpay not configured")
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

    WHEN THE HMAC DOES NOT VERIFY, we ask Razorpay directly before refusing.
    Not as a softening: asking the payment provider whether it captured this
    payment against this order is a STRICTER check than recomputing an HMAC
    over ids the client just handed us. It exists because the failure it
    replaces is invisible and total -- the customer's money is gone, the
    handler's callback is rejected with BAD_SIGNATURE, the phone falls back to
    polling, and if the webhook is also misconfigured nothing ever settles.
    The screen sits on "Opening checkout..." for ninety seconds and then says
    the receipt is taking a moment. It never arrives, and there is no QR for
    the merchant to scan.

    A missing signature is the common cause: some checkout flows return the
    handler payload without `razorpay_signature`, and `verify` on an empty
    string is a guaranteed failure. Stub mode is excluded from the fallback --
    there is no upstream to ask.
    """
    try:
        verified = rzp.verify_checkout_signature(
            body.order_id, body.payment_id, body.signature or ""
        )
    except rzp.PaymentConfigError as exc:
        raise NOT_CONFIGURED from exc

    if not verified:
        verified = _captured_upstream(body.order_id, body.payment_id)
        if verified:
            log.warning(
                "confirm: signature check failed for %s but Razorpay reports "
                "payment %s captured against it; settling on the upstream record",
                body.order_id, body.payment_id,
            )

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


async def _order_belongs_to_session(db: DbDep, order_id: str, session_id: str) -> bool:
    """Both routes below act on an order, and the order id alone is not a
    credential -- it appears in the checkout sheet, in Razorpay's dashboard and
    in any shared screen. The session id is: it is a uuid handed out only in
    exchange for a slot token, and it already gates /accept."""
    row = await db.rpc("get_payment_status", {"p_rzp_order_id": order_id})
    return bool(row) and str(row.get("session_id") or "") == session_id


@router.get("/payments/{order_id}/status")
async def payment_status(order_id: str, session_id: str, db: DbDep) -> dict:
    """Polling backup. Safe to call repeatedly; settles at most once.

    session_id is required. Without it, anyone holding an order id could read
    back the redemption_token, which ids.py is explicit about being a bearer
    credential -- whoever shows it claims the discount.
    """
    if not await _order_belongs_to_session(db, order_id, session_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ORDER_NOT_FOUND", "message": "No such order."},
        )
    try:
        return await payment_service.status(db, order_id)
    except PaymentError as exc:
        raise _http(exc) from exc


@router.post("/payments/{order_id}/release")
async def release(order_id: str, session_id: str, db: DbDep) -> dict:
    """Called when the customer dismisses checkout or the payment fails.

    Returns the reservation to the budget so an abandoned checkout does not
    quietly hold discount for the rest of the demo.

    session_id is required, and this is the sharper of the two. release() nulls
    sessions.rzp_order_id, and settle_payment finds its session BY that column
    -- so releasing someone else's in-flight order means their payment captures
    at Razorpay and then settles nowhere: money taken, no discount applied, no
    redemption token minted. The webhook logs that as ORDER_NOT_FOUND at INFO,
    indistinguishable from the benign race it was written for.
    """
    if not await _order_belongs_to_session(db, order_id, session_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ORDER_NOT_FOUND", "message": "No such order."},
        )
    try:
        return await payment_service.release(db, order_id, "customer dismissed checkout")
    except PaymentError as exc:
        raise _http(exc) from exc
