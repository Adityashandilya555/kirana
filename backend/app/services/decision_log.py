"""The single write path into `decisions`.

Nothing else in the application inserts an audit row. Keeping it to one
function is what makes "every outcome is recorded" a property you can check by
reading one file instead of grepping for inserts.

The plpgsql functions (commit_campaign, reserve_slot, settle_payment,
verify_redemption) write their own rows inside their own transactions, which
is correct -- an audit row that can survive its transaction rolling back is
worse than no audit row. This module covers the chat turn, where there is no
enclosing transaction to join.

`llm_provider` stays NULL on an injection_blocked row. That null is the
machine-checkable evidence that the model was never invoked, and the demo
asserts on it, so nothing here may helpfully fill it in.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.bounds import Decision
from app.core.codes import CLAMPED_CODES, DecisionKind
from app.core.db import DbBackend, RpcError
from app.core.sanitize import SanitizeResult
from app.core.tools import ToolCall

log = logging.getLogger("kirana.audit")

#: Tool results can be long; the audit feed wants a glance, not a dump.
_RESULT_PREVIEW = 240


async def record(
    db: DbBackend,
    *,
    campaign_id: str,
    kind: DecisionKind,
    code: str,
    human_reason: str,
    slot_id: str | None = None,
    session_id: str | None = None,
    turn_index: int | None = None,
    proposed_bps: int | None = None,
    granted_bps: int | None = None,
    binding_constraint: str | None = None,
    customer_reason: str | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    latency_ms: int | None = None,
    raw_user_message: str | None = None,
    raw_llm_output: str | None = None,
    meta: dict[str, Any] | None = None,
) -> int | None:
    """Write one row. Never raises: losing an audit row must not fail a turn
    the customer already experienced."""
    try:
        result = await db.rpc("log_decision", {
            "p_campaign_id": campaign_id,
            "p_kind": kind.value if isinstance(kind, DecisionKind) else str(kind),
            "p_code": code,
            "p_human_reason": human_reason,
            "p_slot_id": slot_id,
            "p_session_id": session_id,
            "p_turn_index": turn_index,
            "p_proposed_bps": proposed_bps,
            "p_granted_bps": granted_bps,
            "p_binding_constraint": binding_constraint,
            "p_customer_reason": customer_reason,
            "p_llm_provider": llm_provider,
            "p_llm_model": llm_model,
            "p_latency_ms": latency_ms,
            "p_raw_user_message": raw_user_message,
            "p_raw_llm_output": raw_llm_output,
            "p_meta": meta or {},
        })
        return (result or {}).get("id")
    except RpcError as exc:
        log.error("audit write failed (%s/%s): %s", kind, code, exc)
        return None


async def record_injection_blocked(
    db: DbBackend, *, campaign_id: str, slot_id: str, session_id: str,
    turn_index: int, raw_message: str, result: SanitizeResult,
) -> None:
    """Note the absent llm_provider -- see the module docstring."""
    await record(
        db,
        campaign_id=campaign_id,
        kind=DecisionKind.INJECTION_BLOCKED,
        code=result.code or "S00_BLOCKED",
        human_reason=(
            f"Blocked before the model was called: {', '.join(result.categories)}. "
            f"No provider was invoked."
        ),
        customer_reason="Let us stick to the shopping -- what are you buying today?",
        slot_id=slot_id,
        session_id=session_id,
        turn_index=turn_index,
        raw_user_message=raw_message[:500],
        llm_provider=None,  # explicit: this null is the proof
        meta={"categories": list(result.categories), "soft_flags": list(result.soft_flags)},
    )


async def record_tool_calls(
    db: DbBackend, *, campaign_id: str, slot_id: str, session_id: str,
    turn_index: int, calls: list[ToolCall], provider: str | None,
) -> None:
    for call in calls:
        # propose_offer gets its own richer row from record_gate_decision.
        if call.name == "propose_offer":
            continue
        await record(
            db,
            campaign_id=campaign_id,
            kind=DecisionKind.TOOL_CALL,
            code=f"T01_{call.name.upper()}",
            human_reason=f"Tool {call.name}({_args(call.args)}) -> {call.result[:_RESULT_PREVIEW]}",
            slot_id=slot_id,
            session_id=session_id,
            turn_index=turn_index,
            llm_provider=provider,
            meta={"tool": call.name, "args": call.args},
        )


async def record_gate_decision(
    db: DbBackend, *, campaign_id: str, slot_id: str, session_id: str,
    turn_index: int, decision: Decision, provider: str | None, model: str | None,
    latency_ms: int | None = None, raw_llm_output: str | None = None,
) -> None:
    """The row the demo points at. Kind reflects what the gate did, not what
    the model wanted: approved / clamped / rejected."""
    if not decision.approved:
        kind = DecisionKind.REJECTED
    elif decision.code in CLAMPED_CODES:
        kind = DecisionKind.CLAMPED
    else:
        kind = DecisionKind.APPROVED

    await record(
        db,
        campaign_id=campaign_id,
        kind=kind,
        code=decision.code.value,
        human_reason=decision.reason,
        customer_reason=decision.customer_reason,
        slot_id=slot_id,
        session_id=session_id,
        turn_index=turn_index,
        proposed_bps=decision.proposed_bps,
        granted_bps=decision.granted_bps if decision.approved else None,
        binding_constraint=decision.binding_constraint,
        llm_provider=provider,
        llm_model=model,
        latency_ms=latency_ms,
        raw_llm_output=(raw_llm_output or "")[:2000] or None,
        meta={
            "max_allowed_bps": decision.max_allowed_bps,
            "discount_paise": decision.discount_paise,
            "final_amount_paise": decision.final_amount_paise,
        },
    )


async def record_upsell(
    db: DbBackend, *, campaign_id: str, slot_id: str, session_id: str,
    turn_index: int, sku: str, decision: Decision, provider: str | None,
) -> None:
    """An add-on the agent was permitted to suggest.

    Recorded even when refused, because "the agent wanted to upsell and the
    gate said no" is exactly the kind of thing this trail exists to show -- the
    bound generalising past discounts rather than applying only to them.
    """
    await record(
        db,
        campaign_id=campaign_id,
        kind=DecisionKind.UPSELL,
        code=decision.code.value,
        human_reason=(
            f"Add-on {sku}: {decision.reason}"
            if decision.approved
            else f"Add-on {sku} withheld: {decision.reason}"
        ),
        customer_reason=decision.customer_reason if decision.approved else None,
        slot_id=slot_id,
        session_id=session_id,
        turn_index=turn_index,
        proposed_bps=decision.proposed_bps,
        granted_bps=decision.granted_bps if decision.approved else None,
        binding_constraint=decision.binding_constraint,
        llm_provider=provider,
        meta={"sku": sku, "approved": decision.approved},
    )


async def record_llm_fallback(
    db: DbBackend, *, campaign_id: str, slot_id: str, session_id: str,
    turn_index: int, error: str | None,
) -> None:
    await record(
        db,
        campaign_id=campaign_id,
        kind=DecisionKind.LLM_FALLBACK,
        code="L01_DETERMINISTIC_TIER",
        human_reason=(
            "Every configured provider failed; answered from the deterministic "
            f"tier. Bounds were still enforced. Errors: {error or 'none recorded'}"
        )[:500],
        customer_reason=None,
        slot_id=slot_id,
        session_id=session_id,
        turn_index=turn_index,
        llm_provider="deterministic",
        meta={"error": (error or "")[:300]},
    )


def _args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items())
