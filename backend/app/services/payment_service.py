"""Order creation and settlement.

Two things in here carry the security argument.

FIRST: `accept()` re-runs `bounds.check()` server-side. The model approved an
offer several seconds ago through `propose_offer`, and that approval is not
trusted here -- not because the model is expected to lie, but because between
then and now the budget may have moved, another slot may have redeemed, and
the campaign may have been paused. The gate is cheap and pure; running it
again costs microseconds and removes an entire class of race.

SECOND: settlement is one plpgsql function. supabase-py speaks PostgREST over
HTTP where every .execute() is its own transaction, so "increment spent + mark
slot redeemed + insert payment + mint token" cannot be made atomic from
Python. All three paths -- checkout handler, webhook, polling -- call exactly
`settle_payment`, which serialises on a row lock and is idempotent on
rzp_payment_id. Whichever arrives second gets the winner's row back verbatim.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core import bounds, rzp
from app.core.bounds import BoundsInput
from app.core.codes import DecisionKind
from app.core.config import settings
from app.core.db import DbBackend, RpcError
from app.services import cart_service, customer_service, decision_log

log = logging.getLogger("kirana.payment")

#: plpgsql `raise exception 'X'` mapped to something an HTTP layer can act on.
CONFLICT_CODES = {
    "SLOT_ALREADY_REDEEMED", "SLOT_NOT_LOCKED", "SESSION_ALREADY_PAID",
    "SLOT_VOID", "ALREADY_COMMITTED",
}
BAD_REQUEST_CODES = {
    "CEILING_VIOLATION", "CAMPAIGN_MAX_VIOLATION", "BUDGET_EXCEEDED",
    "AMOUNT_MISMATCH", "BELOW_MIN_ORDER_AMOUNT", "CAMPAIGN_NOT_LIVE",
    "CART_EMPTY",
    # The database's own re-check of what bounds.check() already refused.
    # Reaching the client at all means Python and SQL disagreed about the same
    # order, which is worth a loud 400 rather than a quiet success -- these
    # should be unreachable, and the day one is not is the day to know.
    "PRODUCT_CAP_VIOLATION", "CUSTOMER_TIER_VIOLATION", "MARGIN_FLOOR_VIOLATION",
}
#: Not an error in the system -- the shopper already had their one discount
#: from this campaign. A conflict rather than a bad request, and it deserves a
#: sentence rather than a code.
CONFLICT_CODES = CONFLICT_CODES | {"CUSTOMER_ALREADY_REDEEMED"}
NOT_FOUND_CODES = {"SESSION_NOT_FOUND", "ORDER_NOT_FOUND"}


class PaymentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


async def accept(
    db: DbBackend, session_id: str, sku: str, qty: int, discount_bps: int
) -> dict[str, Any]:
    """Turn an offer into a Razorpay order and reserve the budget.

    The discount the caller asks for is a *request*, exactly like the model's
    was. It is re-gated here against live campaign state before a single paisa
    is reserved.
    """
    ctx = await db.rpc("get_session_context", {"p_session_id": session_id})
    if ctx is None:
        raise PaymentError("SESSION_NOT_FOUND", "No such session.")

    campaign, slot, session = ctx["campaign"], ctx["slot"], ctx["session"]
    item = next(
        (c for c in ctx.get("catalog") or [] if c["sku"] == (sku or "").upper()), None
    )
    if item is None:
        raise PaymentError("ITEM_NOT_FOUND", f"No such item {sku!r}.")

    # Same derivation as the negotiation path. Re-gating an accept against a
    # looser set of ceilings than the offer was made under would let a shopper
    # accept a number they were never actually offered.
    product_cap, customer_cap = customer_service.caps_for_item(
        item.get("cap_bps"), session.get("tier_cap_fraction_bps")
    )
    verdict = bounds.check(
        BoundsInput(
            proposed_bps=int(discount_bps),
            price_paise=int(item["price_paise"]),
            cost_paise=int(item["cost_paise"]),
            qty=int(qty),
            slot_ceiling_bps=int(slot["ceiling_bps"]),
            slot_status=slot["status"],
            campaign_status=campaign["status"],
            campaign_max_discount_bps=int(campaign["max_discount_bps"]),
            margin_floor_bps=int(campaign["margin_floor_bps"]),
            budget_paise=int(campaign["budget_paise"]),
            spent_paise=int(campaign["spent_paise"]),
            reserved_paise=int(campaign["reserved_paise"]),
            # Accepting an offer is not a negotiating turn: a customer who has
            # decided to pay must not be refused for having talked too long.
            turn_count=0,
            max_turns=int(campaign["max_turns"]),
            product_cap_bps=product_cap,
            customer_cap_bps=customer_cap,
        )
    )
    if not verdict.approved:
        await decision_log.record(
            db, campaign_id=campaign["id"], kind=DecisionKind.REJECTED,
            code=verdict.code.value, human_reason=f"Accept refused: {verdict.reason}",
            customer_reason=verdict.customer_reason,
            slot_id=slot["id"], session_id=session_id,
            proposed_bps=int(discount_bps),
        )
        raise PaymentError(verdict.code.value, verdict.customer_reason)

    order = rzp.create_order(
        amount_paise=verdict.final_amount_paise,
        receipt=rzp.new_receipt(),
        notes={
            "session_id": str(session_id),
            "slot_token": slot["slot_token"],
            "campaign_id": str(campaign["id"]),
            "sku": item["sku"],
        },
    )

    try:
        await db.rpc("reserve_slot", {
            "p_session_id": session_id,
            "p_sku": item["sku"],
            "p_qty": int(qty),
            "p_discount_bps": verdict.granted_bps,
            "p_discount_paise": verdict.discount_paise,
            "p_amount_paise": verdict.final_amount_paise,
            "p_rzp_order_id": order["id"],
        })
    except RpcError as exc:
        # The order exists at Razorpay but we could not reserve. Nothing has
        # been charged; the order simply expires unpaid.
        log.warning("reserve failed after order %s: %s", order["id"], exc)
        raise PaymentError(exc.code, exc.message) from exc

    return {
        "order_id": order["id"],
        "amount_paise": verdict.final_amount_paise,
        "currency": "INR",
        "key_id": settings.RAZORPAY_KEY_ID or None,
        "stub": bool(order.get("stub")),
        "sku": item["sku"],
        "qty": int(qty),
        "granted_bps": verdict.granted_bps,
        "discount_paise": verdict.discount_paise,
        "prefill": {"name": ctx["merchant"]["name"]},
        "session_id": session_id,
        "slot_token": slot["slot_token"],
        # Echoed so the UI can show what actually got approved, which may be
        # lower than what was asked for.
        "capped": verdict.granted_bps < int(discount_bps),
    }


async def checkout(db: DbBackend, session_id: str) -> dict[str, Any]:
    """Turn the whole basket into ONE Razorpay order.

    `accept()` above is the single-item path and stays for the machine-buyer
    flow and for the tests that pin its behaviour. This is what a person's
    phone calls: it re-gates every line the same way accept() re-gates one, and
    for the same reason -- the model approved those lines a conversation ago,
    and between then and now the budget may have moved, the campaign may have
    been paused, and another slot may have redeemed.

    A line that no longer passes the gate is NOT silently dropped and NOT
    silently re-priced. It is reported, with the shop's own sentence, and the
    checkout refuses. Charging someone for a basket that quietly changed under
    them while they were reaching for the Pay button is the one outcome worth
    failing loudly to avoid.

    The gate runs twice on purpose: here, in Python, so the customer gets a
    sentence rather than a plpgsql code -- and again inside `reserve_cart`, in
    SQL, in the same transaction that moves the budget, where it is the thing
    that actually holds.
    """
    ctx = await db.rpc("get_session_context", {"p_session_id": session_id})
    if ctx is None:
        raise PaymentError("SESSION_NOT_FOUND", "No such session.")

    cart = await cart_service.load(db, session_id)
    if not cart["items"]:
        raise PaymentError(
            "CART_EMPTY",
            "There is nothing in your basket yet. Ask me about something on "
            "the shelf and I will price it for you.",
        )

    campaign, slot, session = ctx["campaign"], ctx["slot"], ctx["session"]
    catalog = {c["sku"]: c for c in ctx.get("catalog") or []}

    lines: list[dict[str, Any]] = []
    gross = discount = 0
    for line in cart["items"]:
        sku = str(line["sku"])
        item = catalog.get(sku)
        if item is None:
            raise PaymentError(
                "ITEM_NOT_FOUND",
                f"{line.get('name') or sku} is no longer on this shelf. "
                "Remove it and I will re-price the rest.",
            )

        product_cap, customer_cap = customer_service.caps_for_item(
            item.get("cap_bps"), session.get("tier_cap_fraction_bps")
        )
        verdict = bounds.check(
            BoundsInput(
                proposed_bps=int(line["granted_bps"]),
                price_paise=int(item["price_paise"]),
                cost_paise=int(item["cost_paise"]),
                qty=int(line["qty"]),
                slot_ceiling_bps=int(slot["ceiling_bps"]),
                slot_status=slot["status"],
                campaign_status=campaign["status"],
                campaign_max_discount_bps=int(campaign["max_discount_bps"]),
                margin_floor_bps=int(campaign["margin_floor_bps"]),
                budget_paise=int(campaign["budget_paise"]),
                spent_paise=int(campaign["spent_paise"]),
                # This session's own hold is excluded: re-checking a basket
                # against a budget that already counts its own reservation
                # would refuse every second attempt at the same checkout.
                reserved_paise=max(
                    int(campaign["reserved_paise"])
                    - int(session.get("reserved_paise") or 0), 0),
                # Paying is not a negotiating turn. Someone who has decided to
                # buy must not be refused for having talked too long.
                turn_count=0,
                max_turns=int(campaign["max_turns"]),
                product_cap_bps=product_cap,
                customer_cap_bps=customer_cap,
            )
        )
        if not verdict.approved or verdict.granted_bps < int(line["granted_bps"]):
            await decision_log.record(
                db, campaign_id=campaign["id"], kind=DecisionKind.REJECTED,
                code=verdict.code.value,
                human_reason=f"Cart checkout refused on {sku}: {verdict.reason}",
                customer_reason=verdict.customer_reason,
                slot_id=slot["id"], session_id=session_id,
                proposed_bps=int(line["granted_bps"]),
            )
            raise PaymentError(
                verdict.code.value,
                f"{item['name']}: {verdict.customer_reason}",
            )

        gross += int(item["price_paise"]) * int(line["qty"])
        discount += verdict.discount_paise
        lines.append({
            "sku": sku,
            "name": item["name"],
            "qty": int(line["qty"]),
            "granted_bps": verdict.granted_bps,
            "discount_paise": verdict.discount_paise,
            "line_total_paise": verdict.final_amount_paise,
        })

    total = gross - discount
    order = rzp.create_order(
        amount_paise=total,
        receipt=rzp.new_receipt(),
        notes={
            "session_id": str(session_id),
            "slot_token": slot["slot_token"],
            "campaign_id": str(campaign["id"]),
            # Razorpay caps a note value at 512 characters, and a long basket
            # would silently truncate mid-sku. The count is what a support
            # ticket actually needs; the lines live in cart_items.
            "lines": str(len(lines)),
        },
    )

    try:
        reserved = await db.rpc("reserve_cart", {
            "p_session_id": session_id,
            "p_rzp_order_id": order["id"],
            "p_amount_paise": total,
        })
    except RpcError as exc:
        # The order exists at Razorpay but we could not reserve. Nothing has
        # been charged; the order simply expires unpaid.
        log.warning("reserve_cart failed after order %s: %s", order["id"], exc)
        raise PaymentError(exc.code, exc.message) from exc

    return {
        "order_id": order["id"],
        "amount_paise": total,
        "currency": "INR",
        "key_id": settings.RAZORPAY_KEY_ID or None,
        "stub": bool(order.get("stub")),
        "lines": lines,
        "line_count": len(lines),
        "gross_paise": gross,
        "discount_paise": discount,
        "effective_bps": int((reserved or {}).get("effective_bps") or 0),
        "prefill": {"name": ctx["merchant"]["name"]},
        "session_id": session_id,
        "slot_token": slot["slot_token"],
        # Kept so the checkout sheet has something to name the order with; the
        # bill itself always comes from cart_items.
        "sku": lines[0]["sku"],
        "qty": lines[0]["qty"],
    }


async def settle(
    db: DbBackend,
    *,
    order_id: str,
    payment_id: str,
    signature: str | None,
    amount_paise: int,
    source: str,
) -> dict[str, Any]:
    """The single settlement call. Idempotent on payment_id."""
    try:
        result = await db.rpc("settle_payment", {
            "p_rzp_order_id": order_id,
            "p_rzp_payment_id": payment_id,
            "p_signature": signature,
            "p_amount_paise": int(amount_paise),
            "p_source": source,
        })
    except RpcError as exc:
        raise PaymentError(exc.code, exc.message) from exc
    return result


async def release(db: DbBackend, order_id: str, reason: str) -> dict[str, Any]:
    try:
        return await db.rpc("release_reservation", {
            "p_rzp_order_id": order_id, "p_reason": reason[:200],
        })
    except RpcError as exc:
        raise PaymentError(exc.code, exc.message) from exc


async def status(db: DbBackend, order_id: str) -> dict[str, Any]:
    """What the phone polls while the webhook may or may not arrive.

    Checks our own database first. Only if we have not settled does it ask
    Razorpay, and if Razorpay says captured, it settles through the same
    function the webhook would have used -- so the polling path cannot produce
    a different outcome from the other two.
    """
    row = await db.rpc("get_payment_status", {"p_rzp_order_id": order_id})
    if row and row.get("settled"):
        return {**row, "source": "db"}

    try:
        payments = rzp.order_payments(order_id)
    except Exception as exc:  # noqa: BLE001 - polling must never 500
        # source="upstream_error", not "db". Saying "db" claimed our database
        # had answered when in fact Razorpay had failed, which made a revoked
        # API key look identical to "not paid yet": the phone polls forever and
        # the only trace is one warning line. Log with a traceback and say what
        # actually happened; the customer still gets a 200 so polling survives
        # a transient blip.
        log.exception("razorpay order.payments failed for %s", order_id)
        return {
            **(row or {}),
            "settled": False,
            "source": "upstream_error",
            "poll_error": "Could not reach the payment provider.",
        }

    captured = next((p for p in payments if p.get("status") == "captured"), None)
    if captured is None:
        return {**(row or {}), "settled": False, "source": "razorpay"}

    settled = await settle(
        db,
        order_id=order_id,
        payment_id=captured["id"],
        signature=None,
        amount_paise=int(captured["amount"]),
        source="poll",
    )
    return {**settled, "source": "poll"}
