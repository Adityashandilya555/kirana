"""One chat turn, end to end.

    sanitize -> turn-limit precheck -> load cart -> run_agent -> audit
             -> flush cart -> persist -> reply

The ordering is the security argument in miniature. Screening happens before
any provider is constructed, so a blocked message provably costs zero tokens
and writes a row with llm_provider NULL. The turn-limit check happens before
the model too, so an exhausted session cannot be talked into one more round.

What comes back to the phone is assembled from `bounds.Decision`, never parsed
out of the model's prose. The model writes the sentence; the gate decides the
number. If those two ever disagree, the number the customer can act on is the
gate's, because the offer object is built from it directly.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core import agent as agentmod
from app.core import bounds, sanitize
from app.core.codes import CLAMPED_CODES, BoundsCode, DecisionKind
from app.core.db import DbBackend
from app.core.tools import (
    CatalogItem,
    OfferContext,
    render_system_prompt,
    scope_note,
)
from app.services import cart_service, decision_log

log = logging.getLogger("kirana.chat")

BLOCKED_REPLY = (
    "Let us stick to the shopping -- tell me what you are buying and "
    "I will see what I can do on the price."
)


async def open_session(
    db: DbBackend, slot_token: str, *, transport: str = "web",
    transport_ref: str | None = None, phone_e164: str | None = None,
) -> dict[str, Any]:
    """Resolve a scanned token to a session. Idempotent: a reload resumes.

    With a phone, "resume" means this customer's negotiation on this sticker.
    Without one it means the sticker's unidentified negotiation, which is
    exactly the old behaviour. The two live under separate partial unique
    indexes so a shared shelf sticker can carry several conversations at once
    without any of them seeing another's transcript.
    """
    return await db.rpc("open_session_by_token", {
        "p_slot_token": slot_token,
        "p_transport": transport,
        "p_transport_ref": transport_ref,
        "p_phone_e164": phone_e164,
    })


async def load_context(db: DbBackend, session_id: str) -> dict[str, Any] | None:
    return await db.rpc("get_session_context", {"p_session_id": session_id})


def _offer_context(
    ctx: dict[str, Any], cart: dict[str, Any] | None = None
) -> OfferContext:
    campaign, slot = ctx["campaign"], ctx["slot"]
    session = ctx["session"]
    return OfferContext(
        catalog=[
            CatalogItem(
                sku=c["sku"], name=c["name"], unit=c["unit"],
                price_paise=int(c["price_paise"]), cost_paise=int(c["cost_paise"]),
                # None on a campaign that predates per-product caps. Null means
                # "not applied", never "a cap of zero" -- the latter would make
                # every legacy campaign refuse every discount.
                cap_bps=(int(c["cap_bps"]) if c.get("cap_bps") is not None else None),
            )
            for c in ctx.get("catalog") or []
        ],
        slot_ceiling_bps=int(slot["ceiling_bps"]),
        slot_status=slot["status"],
        campaign_status=campaign["status"],
        campaign_max_discount_bps=int(campaign["max_discount_bps"]),
        margin_floor_bps=int(campaign["margin_floor_bps"]),
        budget_paise=int(campaign["budget_paise"]),
        spent_paise=int(campaign["spent_paise"]),
        reserved_paise=int(campaign["reserved_paise"]),
        turn_count=int(session.get("turn_count") or 0),
        max_turns=int(campaign["max_turns"]),
        # Read off the snapshot written when the session opened, never
        # recomputed here. Sessions from before tiers existed have no snapshot,
        # and the honest default for "no record" is the untiered one: band
        # 'new' at the full fraction, which is exactly today's behaviour.
        tier_key=session.get("tier_key") or "new",
        tier_cap_fraction_bps=int(session.get("tier_cap_fraction_bps") or 10_000),
        tier_stats=session.get("tier_stats") or {},
        cart=cart or dict(cart_service.EMPTY),
    )


def _offer_payload(oc: OfferContext) -> dict[str, Any] | None:
    """The offer card. Built from the gate's Decision, never from the prose.

    Deliberately WITHOUT max_allowed_bps. That field is the smallest of the
    four ceilings, and because plan_ceilings tiers most stickers below the
    campaign maximum, for a typical slot it is exactly slot.ceiling_bps. Sending
    it meant a shopper could ask for 2%, be approved as proposed, and read their
    committed ceiling straight out of the response body -- without ever
    triggering a clamp. From then on the haggling is theatre: they simply ask
    for the number they were shown.

    It stays server-side and still reaches the model through propose_offer,
    which is its entire purpose -- the refuse-and-explain loop where the agent
    re-proposes inside the bound. The merchant console keeps it too.

    What remains here is enough to render the offer honestly: what was asked,
    what was granted, whether the gate intervened, and which rule bound it. On a
    clamped turn customer_reason already says "the best this code allows is
    12%", which is a deliberate disclosure at the point the negotiation has
    ended -- not a number handed over on turn one.
    """
    decision = oc.last_decision
    if decision is None or not decision.approved:
        return None
    return {
        "sku": oc.last_sku,
        "qty": oc.last_qty,
        "granted_bps": decision.granted_bps,
        "proposed_bps": decision.proposed_bps,
        "discount_paise": decision.discount_paise,
        "final_amount_paise": decision.final_amount_paise,
        "code": decision.code.value,
        # What the "capped by shelf limit" chip keys off.
        "capped": decision.code in CLAMPED_CODES,
        "binding_constraint": decision.binding_constraint,
        "customer_reason": decision.customer_reason,
        # The card is a receipt for one negotiation now, not the thing you pay
        # from. Payment happens once, from the basket.
        "added_to_cart": oc.last_sku in oc.cart_ops
                         and not oc.cart_ops[oc.last_sku].remove,
    }


PAID_REPLY = (
    "That basket is paid for, ji -- show the code at the counter. "
    "Scan the sticker again if you would like to buy something else."
)


async def chat_turn(db: DbBackend, session_id: str, message: str) -> dict[str, Any]:
    ctx = await load_context(db, session_id)
    if ctx is None:
        raise ValueError("SESSION_NOT_FOUND")

    campaign, slot, session = ctx["campaign"], ctx["slot"], ctx["session"]
    campaign_id, slot_id = campaign["id"], slot["id"]
    turn_index = int(session.get("turn_count") or 0)

    # -- 0. this conversation is over ---------------------------------------
    # A paid session is closed, and saying so is not pedantry. On a ONE-SHOT
    # sticker the gate already refuses: slot_status becomes 'redeemed' and
    # bounds.check returns SLOT_NOT_OPEN. On a SHARED sticker the slot stays
    # 'offered' forever by design, so nothing downstream objects -- the model
    # negotiates a fresh discount, the shopper is told they have it, and the
    # basket write is the only thing that fails, quietly, in a log line. They
    # would be holding a price nobody can sell them.
    if session.get("status") == "paid":
        await db.rpc("append_session_turn", {
            "p_session_id": session_id, "p_role": "assistant",
            "p_content": PAID_REPLY, "p_bump_turn": False,
        })
        return {
            "session_id": session_id, "reply": PAID_REPLY, "offer": None,
            "blocked": False, "session_closed": True,
            "provider": None, "latency_ms": 0, "turn_count": turn_index,
            "cart": await cart_service.load(db, session_id),
        }

    # -- 1. screen, before any provider exists ------------------------------
    screened = sanitize.sanitize(message)
    if screened.blocked:
        await db.rpc("append_session_turn", {
            "p_session_id": session_id, "p_role": "user",
            "p_content": screened.text, "p_bump_turn": True,
        })
        await decision_log.record_injection_blocked(
            db, campaign_id=campaign_id, slot_id=slot_id, session_id=session_id,
            turn_index=turn_index, raw_message=message, result=screened,
        )
        await db.rpc("append_session_turn", {
            "p_session_id": session_id, "p_role": "assistant",
            "p_content": BLOCKED_REPLY, "p_bump_turn": False,
        })
        return {
            "session_id": session_id, "reply": BLOCKED_REPLY, "offer": None,
            "blocked": True, "block_categories": list(screened.categories),
            "provider": None, "latency_ms": 0, "turn_count": turn_index + 1,
            "cart": await cart_service.load(db, session_id),
        }

    # The basket, before the model sees anything. It is seeded into the prompt
    # and into the tools, so the assistant knows what the shopper already has
    # without spending a round trip asking.
    cart = await cart_service.load(db, session_id)
    oc = _offer_context(ctx, cart)

    # -- 2. turn limit, also before the model -------------------------------
    if oc.turn_count >= oc.max_turns:
        verdict = bounds.check(
            bounds.BoundsInput(
                proposed_bps=0, price_paise=1, cost_paise=0,
                turn_count=oc.turn_count, max_turns=oc.max_turns,
            )
        )
        await decision_log.record(
            db, campaign_id=campaign_id, kind=DecisionKind.REJECTED,
            code=BoundsCode.TURN_LIMIT.value, human_reason=verdict.reason,
            customer_reason=verdict.customer_reason, slot_id=slot_id,
            session_id=session_id, turn_index=turn_index,
            raw_user_message=screened.text[:500],
        )
        await db.rpc("append_session_turn", {
            "p_session_id": session_id, "p_role": "assistant",
            "p_content": verdict.customer_reason, "p_bump_turn": False,
        })
        return {
            "session_id": session_id, "reply": verdict.customer_reason, "offer": None,
            "blocked": False, "turn_limit_reached": True,
            "provider": None, "latency_ms": 0, "turn_count": oc.turn_count,
            # Out of turns is not out of basket. Whatever was negotiated
            # before the limit is still theirs to pay for.
            "cart": cart,
        }

    # -- 3. the model --------------------------------------------------------
    await db.rpc("append_session_turn", {
        "p_session_id": session_id, "p_role": "user",
        "p_content": screened.text, "p_bump_turn": True,
    })

    result = await agentmod.run_agent(
        oc,
        system_prompt=render_system_prompt(
            ctx["merchant"]["name"], ctx["merchant"]["store_line"], oc.catalog,
            scope_note(slot, oc.catalog), cart,
        ),
        transcript=ctx.get("transcript") or [],
        user_message=screened.text,
    )

    # -- 4. audit ------------------------------------------------------------
    await decision_log.record_tool_calls(
        db, campaign_id=campaign_id, slot_id=slot_id, session_id=session_id,
        turn_index=turn_index, calls=result.tool_calls, provider=result.provider,
    )
    if oc.last_decision is not None:
        await decision_log.record_gate_decision(
            db, campaign_id=campaign_id, slot_id=slot_id, session_id=session_id,
            turn_index=turn_index, decision=oc.last_decision,
            provider=result.provider, model=result.model,
            latency_ms=result.latency_ms, raw_llm_output=result.raw_output,
        )
    if oc.last_addon is not None:
        addon_sku, addon_decision = oc.last_addon
        await decision_log.record_upsell(
            db, campaign_id=campaign_id, slot_id=slot_id, session_id=session_id,
            turn_index=turn_index, sku=addon_sku, decision=addon_decision,
            provider=result.provider,
        )
    if result.used_fallback:
        await decision_log.record_llm_fallback(
            db, campaign_id=campaign_id, slot_id=slot_id, session_id=session_id,
            turn_index=turn_index, error=result.error,
        )

    # -- 5. the basket -------------------------------------------------------
    # After the audit and before the reply. An approval that was written to the
    # decision log but not to the basket would be a discount the shopper was
    # told about, that the merchant can prove was granted, and that they cannot
    # actually buy -- which is the worst of the three orderings.
    updated = await cart_service.flush(db, session_id, oc.cart_ops)
    if updated is not None:
        cart = updated

    # -- 6. persist and reply ------------------------------------------------
    turn = await db.rpc("append_session_turn", {
        "p_session_id": session_id, "p_role": "assistant",
        "p_content": result.reply, "p_bump_turn": False,
    })

    return {
        "session_id": session_id,
        "reply": result.reply,
        "offer": _offer_payload(oc),
        "cart": cart,
        "blocked": False,
        "provider": result.provider,
        "model": result.model,
        "latency_ms": result.latency_ms,
        "steps": result.steps,
        "turn_count": (turn or {}).get("turn_count", turn_index + 1),
        "max_turns": oc.max_turns,
    }
