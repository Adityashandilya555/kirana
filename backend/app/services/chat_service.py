"""One chat turn, end to end.

    sanitize -> turn-limit precheck -> run_agent -> audit -> persist -> reply

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
from app.core.tools import CatalogItem, OfferContext, render_system_prompt
from app.services import decision_log

log = logging.getLogger("kirana.chat")

BLOCKED_REPLY = (
    "Let us stick to the shopping -- tell me what you are buying and "
    "I will see what I can do on the price."
)


async def open_session(
    db: DbBackend, slot_token: str, *, transport: str = "web",
    transport_ref: str | None = None,
) -> dict[str, Any]:
    """Resolve a scanned token to a session. Idempotent: a reload resumes."""
    return await db.rpc("open_session_by_token", {
        "p_slot_token": slot_token,
        "p_transport": transport,
        "p_transport_ref": transport_ref,
    })


async def load_context(db: DbBackend, session_id: str) -> dict[str, Any] | None:
    return await db.rpc("get_session_context", {"p_session_id": session_id})


def _offer_context(ctx: dict[str, Any]) -> OfferContext:
    campaign, slot = ctx["campaign"], ctx["slot"]
    session = ctx["session"]
    return OfferContext(
        catalog=[
            CatalogItem(
                sku=c["sku"], name=c["name"], unit=c["unit"],
                price_paise=int(c["price_paise"]), cost_paise=int(c["cost_paise"]),
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
    )


def _offer_payload(oc: OfferContext) -> dict[str, Any] | None:
    """The offer card. Built from the gate's Decision, never from the prose."""
    decision = oc.last_decision
    if decision is None or not decision.approved:
        return None
    return {
        "sku": oc.last_sku,
        "qty": oc.last_qty,
        "granted_bps": decision.granted_bps,
        "proposed_bps": decision.proposed_bps,
        "max_allowed_bps": decision.max_allowed_bps,
        "discount_paise": decision.discount_paise,
        "final_amount_paise": decision.final_amount_paise,
        "code": decision.code.value,
        # What the "capped by shelf limit" chip keys off.
        "capped": decision.code in CLAMPED_CODES,
        "binding_constraint": decision.binding_constraint,
        "customer_reason": decision.customer_reason,
    }


async def chat_turn(db: DbBackend, session_id: str, message: str) -> dict[str, Any]:
    ctx = await load_context(db, session_id)
    if ctx is None:
        raise ValueError("SESSION_NOT_FOUND")

    campaign, slot, session = ctx["campaign"], ctx["slot"], ctx["session"]
    campaign_id, slot_id = campaign["id"], slot["id"]
    turn_index = int(session.get("turn_count") or 0)

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
        }

    oc = _offer_context(ctx)

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
        }

    # -- 3. the model --------------------------------------------------------
    await db.rpc("append_session_turn", {
        "p_session_id": session_id, "p_role": "user",
        "p_content": screened.text, "p_bump_turn": True,
    })

    result = await agentmod.run_agent(
        oc,
        system_prompt=render_system_prompt(
            ctx["merchant"]["name"], ctx["merchant"]["store_line"], oc.catalog
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
    if result.used_fallback:
        await decision_log.record_llm_fallback(
            db, campaign_id=campaign_id, slot_id=slot_id, session_id=session_id,
            turn_index=turn_index, error=result.error,
        )

    # -- 5. persist and reply ------------------------------------------------
    turn = await db.rpc("append_session_turn", {
        "p_session_id": session_id, "p_role": "assistant",
        "p_content": result.reply, "p_bump_turn": False,
    })

    return {
        "session_id": session_id,
        "reply": result.reply,
        "offer": _offer_payload(oc),
        "blocked": False,
        "provider": result.provider,
        "model": result.model,
        "latency_ms": result.latency_ms,
        "steps": result.steps,
        "turn_count": (turn or {}).get("turn_count", turn_index + 1),
        "max_turns": oc.max_turns,
    }
