"""Razorpay client and signature verification.

Everything in this file exists because the SDK's shape is easy to get subtly
wrong in ways that look like a bug in your own code:

  * The verify calls RAISE. They never return False. `if not verify(...)` is
    always falsy-negative and silently accepts every forged signature, which
    is the worst possible failure mode. Both are wrapped to return a bool.

  * There are TWO different secrets. The checkout callback is signed with the
    API key secret; the webhook body is signed with the webhook secret. They
    are different values and mixing them up produces a signature failure that
    reads exactly like a tampering attempt.

  * verify_webhook_signature does bytes(body, 'utf-8') internally, so it needs
    a `str`. `await request.body()` gives you `bytes`. Passing those straight
    in is a TypeError, not a signature error, and the traceback points at the
    SDK rather than at the caller.

  * `receipt` is capped at 40 characters and must be unique per order. A fixed
    string works on the first rehearsal and fails on the second.

DEMO_MODE lets every route above this run without credentials: the client is
None, order creation returns a synthetic order id, and signature checks are
skipped. That is what makes Phase 4 testable before the keys arrive -- and it
refuses to engage when APP_ENV is production, so it cannot be left on by
accident in the one place it would matter.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core.config import settings

log = logging.getLogger("kirana.rzp")

#: Razorpay caps receipt at 40 chars.
RECEIPT_MAX = 40


class PaymentConfigError(RuntimeError):
    """Razorpay is not configured and we are not allowed to fake it."""


def configured() -> bool:
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def stub_mode() -> bool:
    """True when we may mint synthetic orders instead of calling Razorpay.

    Never in production, whatever DEMO_MODE says. A missing key in production
    must be a loud failure, not a silent switch to fake payments.
    """
    return not configured() and settings.DEMO_MODE and settings.APP_ENV != "production"


def client() -> Any:
    if not configured():
        raise PaymentConfigError(
            "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are not set."
        )
    import razorpay  # imported lazily so the app boots without the credentials

    c = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    c.set_app_details({"title": "kirana-agent", "version": "0.1.0"})
    return c


def new_receipt(prefix: str = "kir") -> str:
    """Unique, and inside Razorpay's 40-character limit.

    A fixed receipt survives exactly one rehearsal: the second create_order
    with the same value is rejected as a duplicate.
    """
    return f"{prefix}_{uuid.uuid4().hex}"[:RECEIPT_MAX]


def create_order(amount_paise: int, receipt: str, notes: dict[str, Any]) -> dict[str, Any]:
    """Create a Razorpay order, or a synthetic one in stub mode.

    `amount` must be an int in paise. Floats round in ways that produce
    off-by-one-paise mismatches at settlement, which then fail the amount
    check for reasons nobody can see.

    Deliberately does NOT set payment.capture_options: the minimum
    automatic_expiry_period is 12 minutes, which is longer than the entire
    demo, and a pending auto-capture window is not something to explain on
    stage.
    """
    if stub_mode():
        order_id = f"order_stub{uuid.uuid4().hex[:14]}"
        log.warning("razorpay not configured; minting stub order %s", order_id)
        return {
            "id": order_id,
            "amount": int(amount_paise),
            "currency": "INR",
            "receipt": receipt,
            "status": "created",
            "stub": True,
        }

    order = client().order.create({
        "amount": int(amount_paise),
        "currency": "INR",
        "receipt": receipt,
        "payment_capture": 1,
        "notes": notes,
    })
    return dict(order)


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Checkout handler callback. Signed with the API KEY SECRET."""
    if stub_mode():
        log.warning("razorpay not configured; accepting checkout signature unchecked")
        return True
    import razorpay

    try:
        client().utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })
        return True
    except razorpay.errors.SignatureVerificationError:
        return False


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Webhook body. Signed with the WEBHOOK SECRET, not the key secret.

    `raw_body` is the untouched bytes off the wire. It is decoded here because
    the SDK does bytes(body, 'utf-8') internally and therefore needs a str --
    and because re-serialising a parsed body would change the bytes the
    signature was computed over.
    """
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        if stub_mode():
            log.warning("no webhook secret; accepting webhook unchecked")
            return True
        return False
    import razorpay

    try:
        client().utility.verify_webhook_signature(
            raw_body.decode("utf-8"),
            signature,
            settings.RAZORPAY_WEBHOOK_SECRET,
        )
        return True
    except razorpay.errors.SignatureVerificationError:
        return False
    except Exception as exc:  # noqa: BLE001 - a malformed body must not 500
        log.warning("webhook signature check failed: %s", exc)
        return False


def order_payments(order_id: str) -> list[dict[str, Any]]:
    """Payments against an order. The method is `order.payments`, not
    `fetch_payments` -- the latter does not exist and the AttributeError
    surfaces only on the polling path, which is the one you reach for when
    the webhook has already failed."""
    if stub_mode():
        return []
    resp = client().order.payments(order_id)
    return list(resp.get("items") or [])


def notes_dict(raw: Any) -> dict[str, Any]:
    """Razorpay serialises empty `notes` as [] (a JSON array), not {}.

    Indexing that as a dict raises TypeError inside a webhook handler, where
    the exception is invisible because the route has already returned 200.
    """
    return raw if isinstance(raw, dict) else {}
