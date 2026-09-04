"""The basket, and the one rule about who is allowed to fill it.

A line enters a cart only after `bounds.check()` approved it. The tools cannot
write here -- they are synchronous and the database is not, which is the same
reason `propose_offer` never touched Postgres -- so a turn's approvals are
collected on `OfferContext.cart_ops` and flushed here afterwards, by
`chat_service`, which is async and already owns the audit writes.

That ordering matters and is not incidental. If a tool could write the cart
directly, a turn that later fails or times out would leave items in a basket
the shopper was never told about. Collect, then commit once the turn is known
to have produced a reply.

Nothing in this module decides a price. The amounts it writes come from the
`Decision` the gate returned, and `reserve_cart` re-derives every one of them
from the live catalogue at checkout, so being wrong here costs a rejected
checkout rather than a wrong charge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.core.db import DbBackend, RpcError

log = logging.getLogger("kirana.cart")

EMPTY: dict[str, Any] = {
    "cart_id": None, "status": "open", "items": [], "count": 0,
    "gross_paise": 0, "discount_paise": 0, "total_paise": 0,
}


@dataclass
class CartOp:
    """One pending change to the basket, produced by a tool inside a turn."""

    sku: str
    remove: bool = False
    qty: int = 1
    granted_bps: int = 0
    discount_paise: int = 0
    line_total_paise: int = 0
    unit_price_paise: int = 0
    name: str = ""
    unit: str = "pc"
    decision_code: str | None = None
    binding_constraint: str | None = None


def normalise(raw: Any) -> dict[str, Any]:
    """A cart, whatever the database handed back.

    `get_cart` returns SQL NULL when the session has never had one, which is
    the common case on turn one -- and an empty basket and a missing basket
    are the same thing to everyone upstream.
    """
    if not isinstance(raw, dict):
        return dict(EMPTY)
    items = raw.get("items")
    return {
        "cart_id": raw.get("cart_id"),
        "status": raw.get("status") or "open",
        "items": list(items) if isinstance(items, list) else [],
        "count": int(raw.get("count") or 0),
        "gross_paise": int(raw.get("gross_paise") or 0),
        "discount_paise": int(raw.get("discount_paise") or 0),
        "total_paise": int(raw.get("total_paise") or 0),
    }


def preview(cart: dict[str, Any], ops: dict[str, CartOp]) -> dict[str, Any]:
    """What the basket will look like once this turn's ops are flushed.

    Pure, so the `view_cart` tool can answer inside the same turn that added
    something -- a model that adds oil and is then asked "what do I have?"
    must not be told the oil is missing because the write has not happened
    yet. Totals are recomputed rather than adjusted, because an adjustment is
    a second arithmetic that can disagree with the first.
    """
    lines = {str(i.get("sku")): dict(i) for i in cart.get("items") or []}
    for sku, op in ops.items():
        if op.remove:
            lines.pop(sku, None)
            continue
        existing = lines.get(sku)
        # Same rule the SQL upsert applies: a line never goes backwards. A
        # shopper keeps the best rate they were actually told they had.
        granted = max(op.granted_bps, int(existing.get("granted_bps") or 0)) if existing else op.granted_bps
        gross = op.unit_price_paise * op.qty
        discount = gross * granted // 10_000
        lines[sku] = {
            "sku": sku,
            "name": op.name or (existing or {}).get("name") or sku,
            "unit": op.unit or (existing or {}).get("unit") or "pc",
            "qty": op.qty,
            "unit_price_paise": op.unit_price_paise,
            "gross_paise": gross,
            "granted_bps": granted,
            "discount_paise": discount,
            "line_total_paise": gross - discount,
            "binding_constraint": (
                op.binding_constraint if granted == op.granted_bps
                else (existing or {}).get("binding_constraint")
            ),
            "decision_code": op.decision_code,
        }

    items = list(lines.values())
    gross = sum(int(i.get("gross_paise") or 0) for i in items)
    discount = sum(int(i.get("discount_paise") or 0) for i in items)
    return {
        "cart_id": cart.get("cart_id"),
        "status": cart.get("status") or "open",
        "items": items,
        "count": len(items),
        "gross_paise": gross,
        "discount_paise": discount,
        "total_paise": gross - discount,
    }


async def load(db: DbBackend, session_id: str) -> dict[str, Any]:
    """The basket, or an empty one if the basket cannot be read.

    NEVER raises. This is called at the top of every chat turn, and on the day
    the backend shipped ahead of its migrations that made it the first thing a
    shopper hit: PostgREST answered `Could not find the function
    public.get_cart` on every message, the RpcError propagated out of
    chat_turn, and the whole negotiation returned "The shop's system did not
    respond." A shop that cannot remember a basket should still be able to
    quote a price -- degrading to no basket is a bad afternoon, taking the
    conversation down with it is a dead demo.

    Logged at ERROR because the degraded state is otherwise invisible: the
    chat looks fine and the Pay button simply never appears.
    """
    try:
        return normalise(await db.rpc("get_cart", {"p_session_id": session_id}))
    except RpcError as exc:
        log.error(
            "cart unreadable for session %s (%s: %s) -- serving an empty "
            "basket; the negotiation still works but nothing can be bought. "
            "If this says the function is missing, sql/025_cart.sql has not "
            "been applied to this database.",
            session_id, exc.code, exc.detail[:200],
        )
        return dict(EMPTY)


async def flush(
    db: DbBackend, session_id: str, ops: dict[str, CartOp]
) -> dict[str, Any] | None:
    """Apply a turn's basket changes. Returns the cart, or None if nothing moved.

    A single failing line is logged and skipped rather than aborting the turn.
    The shopper has already been told what they were granted; losing the whole
    reply because one write raced a catalogue edit would be the worse outcome,
    and the missing line resurfaces the moment they mention it again.
    """
    if not ops:
        return None
    cart: dict[str, Any] | None = None
    for sku, op in ops.items():
        try:
            if op.remove:
                raw = await db.rpc("remove_cart_item", {
                    "p_session_id": session_id, "p_sku": sku,
                })
            else:
                raw = await db.rpc("upsert_cart_item", {
                    "p_session_id": session_id,
                    "p_sku": sku,
                    "p_qty": op.qty,
                    "p_granted_bps": op.granted_bps,
                    "p_discount_paise": op.discount_paise,
                    "p_line_total_paise": op.line_total_paise,
                    "p_decision_code": op.decision_code,
                    "p_binding_constraint": op.binding_constraint,
                })
            cart = normalise(raw)
        except RpcError as exc:
            log.warning("cart write failed for %s on session %s: %s",
                        sku, session_id, exc.code)
    return cart
