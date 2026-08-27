"""Razorpay webhook.

Four constraints shape this file, and every one of them is a way the obvious
implementation fails:

1.  NO PYDANTIC BODY MODEL. Declaring one makes FastAPI consume the request
    stream to validate it, and `await request.body()` afterwards returns b"".
    The signature is computed over the exact bytes Razorpay sent, so once
    they are gone the signature can never be checked again.

2.  RETURN 200 IMMEDIATELY. Razorpay's webhook timeout is five seconds and it
    retries on any non-2xx. Settlement touches the database and can exceed
    that under load, so the work goes to BackgroundTasks and the response
    goes out first. A slow handler turns one payment into a retry storm.

3.  DEDUPE ON THE EVENT ID. Both `order.paid` and `payment.captured` fire for
    a single payment, and retries repeat them. `log_webhook_event` is the
    dedupe anchor via uq_webhook_event_id; `settle_payment` is idempotent on
    rzp_payment_id underneath it, so even a missed dedupe cannot double-settle.

4.  `notes` DESERIALISES AS [] WHEN EMPTY. Razorpay sends a JSON array, not an
    object, so indexing it as a dict raises TypeError inside a background task
    where nothing surfaces the error.

A signature failure is recorded and then dropped. It still returns 200: a
forged webhook must not be able to make Razorpay retry, and the row in
webhook_events is the evidence.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, Request

from app.api.deps import DbDep
from app.core import rzp
from app.core.db import DbBackend
from app.services import payment_service
from app.services.payment_service import PaymentError

log = logging.getLogger("kirana.webhook")

router = APIRouter(prefix="/api/v1", tags=["webhooks"])

#: The only events worth acting on. Both fire for one payment.
SETTLING_EVENTS = {"payment.captured", "order.paid"}
FAILURE_EVENTS = {"payment.failed"}


def _payment_entity(payload: dict[str, Any]) -> dict[str, Any]:
    return (
        payload.get("payload", {}).get("payment", {}).get("entity", {}) or {}
    )


async def _process(db: DbBackend, event_id: str, event_type: str, payload: dict) -> None:
    """Runs after the 200 has already gone out."""
    error: str | None = None
    try:
        entity = _payment_entity(payload)
        order_id = entity.get("order_id")
        payment_id = entity.get("id")

        if event_type in FAILURE_EVENTS and order_id:
            await payment_service.release(
                db, order_id, f"payment.failed: {entity.get('error_description') or ''}"
            )
        elif event_type in SETTLING_EVENTS and order_id and payment_id:
            await payment_service.settle(
                db,
                order_id=order_id,
                payment_id=payment_id,
                signature=None,  # webhook body signature, not a payment signature
                amount_paise=int(entity.get("amount") or 0),
                source="webhook",
            )
        else:
            log.info("webhook %s ignored (%s)", event_id, event_type)
    except PaymentError as exc:
        # ALREADY_REDEEMED here is the normal outcome of the checkout handler
        # having won the race, not a failure.
        error = f"{exc.code}: {exc.message}"
        log.info("webhook %s settled elsewhere or refused: %s", event_id, error)
    except Exception as exc:  # noqa: BLE001 - a background task must not vanish silently
        error = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("webhook %s failed", event_id)
    finally:
        await db.rpc("mark_webhook_processed", {
            "p_event_id": event_id, "p_error": error,
        })


@router.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    background: BackgroundTasks,
    db: DbDep,
    x_razorpay_signature: str = Header(default=""),
    x_razorpay_event_id: str = Header(default=""),
) -> dict:
    # Raw bytes, before anything parses them. See constraint 1.
    raw = await request.body()

    signature_ok = rzp.verify_webhook_signature(raw, x_razorpay_signature)

    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        payload = {}

    event_type = payload.get("event", "unknown")
    # Razorpay always sends the header; fall back so a malformed delivery
    # still gets a dedupe key rather than colliding on empty string.
    event_id = x_razorpay_event_id or f"noid_{hash(raw) & 0xffffffff:08x}"
    entity = _payment_entity(payload)

    record = await db.rpc("log_webhook_event", {
        "p_event_id": event_id,
        "p_event_type": event_type,
        "p_signature_ok": signature_ok,
        "p_payload": payload,
        "p_rzp_order_id": entity.get("order_id"),
        "p_rzp_payment_id": entity.get("id"),
    })

    if not signature_ok:
        log.warning("webhook %s failed signature check; recorded and dropped", event_id)
        return {"ok": True, "ignored": "bad_signature"}

    if (record or {}).get("duplicate"):
        # order.paid and payment.captured both arrive; so do retries.
        return {"ok": True, "duplicate": True}

    background.add_task(_process, db, event_id, event_type, payload)
    return {"ok": True}
