"""The tool-calling loop, ported from ~/agent1's run_agent().

The notebook's shape survives intact: bind_tools -> ainvoke -> if the reply
carries tool_calls, execute them, append a ToolMessage each, and go round
again; otherwise that reply is the answer. Capped by max_steps. No framework
agent executor -- roughly forty lines of control flow that can be read in one
sitting and debugged on a stage.

Three deliberate departures from the notebook:

  * It handles PARALLEL tool calls. The notebook clamped to one because NVIDIA
    NIM misbehaved; Ollama Cloud returns several per turn and dropping the
    extras strands their tool_call_ids, which the next request rejects.

  * It fails over. Each provider from llm.provider_chain() is tried in order,
    and the circuit breaker is fed on the way past.

  * Tier 3 is a deterministic responder, not an error. When no provider
    answers, a keyword parser still produces a real propose_offer call, so the
    demo degrades to a worse sentence rather than a spinner that never stops.

Never with_structured_output(): Ollama Cloud ignores JSON-Schema structured
outputs silently (ollama/ollama#12362), which returns plausible prose instead
of raising. The tool argument schema gives us the same guarantee by a route
that actually holds.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.core import llm as llmmod
from app.core import tools as toolsmod
from app.core.config import settings
from app.core.tools import OfferContext

log = logging.getLogger("kirana.agent")

#: Tier 3's opening ask when the shopper named no number. High enough that the
#: gate has something to clamp, which is the beat the demo wants to show.
FALLBACK_OPENING_BPS = 1_000


@dataclass
class AgentResult:
    reply: str
    provider: str | None
    model: str | None
    latency_ms: int
    steps: int
    tool_calls: list[toolsmod.ToolCall] = field(default_factory=list)
    raw_output: str = ""
    error: str | None = None

    @property
    def used_fallback(self) -> bool:
        return self.provider == "deterministic"


def _history_messages(transcript: list[dict[str, Any]], limit: int = 8) -> list[BaseMessage]:
    """Recent turns only. The full transcript grows without bound and every
    token of it is latency on a stage."""
    out: list[BaseMessage] = []
    for entry in transcript[-limit:]:
        role, content = entry.get("role"), (entry.get("content") or "")
        if not content:
            continue
        if role == "user":
            out.append(HumanMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
    return out


def gate_sentence(ctx: OfferContext) -> str:
    """What to say when the model ended on a tool call with nothing to say.

    This is a real failure mode, not a theoretical one: a model that spends its
    last step calling propose_offer produces an AIMessage with tool_calls and
    an empty content, and the loop exits with no prose. The old fallback shipped
    the gate's own audit sentence verbatim -- which is "Done -- 5% off." A
    shopper negotiating three items got that same six-word bubble three times
    and could not tell which item any of them was about.

    The gate still decides the number. This only spends the item name and the
    rupee figure it already returned, which is the difference between a receipt
    and a shrug.
    """
    decision = ctx.last_decision
    if decision is None:
        return "Tell me which item you are looking at and I will check the price."

    item = ctx.item(ctx.last_sku or "")
    name = item.name if item else (ctx.last_sku or "that")
    qty = f" x{ctx.last_qty}" if ctx.last_qty > 1 else ""

    if not decision.approved:
        # Do not stop at the refusal. Three turns of the identical sentence --
        # "Aashirvaad Select Atta 10kg -- I cannot go below cost on this one" --
        # is what a shopper actually got while asking about rice, and a bubble
        # that repeats verbatim reads as a crashed app rather than a shop
        # holding its price. Say the no, then hand the conversation back.
        instead = ctx.discountable_alternatives(ctx.last_sku or "", limit=2)
        line = f"{name}{qty} -- {decision.customer_reason}"
        if instead:
            names = " or ".join(c.name for c in instead)
            return f"{line} I can do something on {names} though, ji -- shall I?"
        return f"{line} Tell me what else you need and I will check it, ji."
    return (
        f"{name}{qty} at {decision.granted_bps / 100:g}% off, "
        f"Rs {decision.final_amount_paise / 100:,.2f}. "
        f"It is in your basket -- anything else, ji?"
    )


def _run_tool(tool_map: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    tool = tool_map.get(name)
    if tool is None:
        return f'{{"error":"unknown tool {name}"}}'
    try:
        return str(tool.invoke(args))
    except Exception as exc:  # noqa: BLE001 - a bad call must not kill the turn
        log.warning("tool %s failed: %s", name, exc)
        return f'{{"error":"{type(exc).__name__}: {exc}"}}'


async def _attempt(
    spec: llmmod.ProviderSpec,
    ctx: OfferContext,
    system_prompt: str,
    history: list[BaseMessage],
    user_message: str,
) -> AgentResult:
    """One provider, the whole loop. Raises so the caller can fail over."""
    tools, tool_map = toolsmod.build_tools(ctx)
    model = llmmod.build_chat_model(spec).bind_tools(tools)

    messages: list[BaseMessage] = [
        SystemMessage(content=system_prompt),
        *history,
        HumanMessage(content=user_message),
    ]

    started = time.monotonic()
    deadline = started + settings.LLM_GLOBAL_DEADLINE_S
    steps = 0
    last_text = ""

    for _ in range(max(1, settings.AGENT_MAX_STEPS)):
        steps += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("agent deadline exceeded")

        ai: AIMessage = await asyncio.wait_for(model.ainvoke(messages), timeout=remaining)
        messages.append(ai)
        last_text = ai.content if isinstance(ai.content, str) else str(ai.content)

        calls = getattr(ai, "tool_calls", None) or []
        if not calls:
            break

        # Every call gets a ToolMessage, including ones we do not recognise --
        # an unanswered tool_call_id makes the next request invalid.
        for call in calls:
            result = _run_tool(tool_map, call.get("name", ""), call.get("args") or {})
            messages.append(
                ToolMessage(content=result, tool_call_id=call.get("id") or call.get("name", ""))
            )

    latency = int((time.monotonic() - started) * 1000)
    reply = (last_text or "").strip()
    if not reply:
        reply = gate_sentence(ctx)
    return AgentResult(
        reply=reply,
        provider=spec.name,
        model=spec.model,
        latency_ms=latency,
        steps=steps,
        tool_calls=list(ctx.calls),
        raw_output=last_text or "",
    )


# ---------------------------------------------------------------------------
# Tier 3: no model at all
# ---------------------------------------------------------------------------
_PCT = re.compile(r"(\d{1,3})\s*(?:%|percent|pct)")
_QTY = re.compile(r"\b(\d{1,2})\s*(?:x|nos?|units?|packs?|bags?|bottles?)?\b")


def deterministic_reply(ctx: OfferContext, user_message: str) -> AgentResult:
    """A real offer without a language model.

    Not an error path and not a canned string: it resolves an item, asks the
    gate, and reports the gate's own sentence. The demo gets worse prose and
    keeps every guarantee.
    """
    started = time.monotonic()
    _, tool_map = toolsmod.build_tools(ctx)

    best, best_score = None, 0
    for item in ctx.catalog:
        score = toolsmod._score(item, user_message)
        if score > best_score:
            best, best_score = item, score
    if best is None:
        best = max(ctx.catalog, key=lambda c: c.price_paise, default=None)

    if best is None:
        return AgentResult(
            reply="The shelf list is unavailable right now. Please ask the shopkeeper.",
            provider="deterministic", model=None,
            latency_ms=int((time.monotonic() - started) * 1000), steps=0,
        )

    pct = _PCT.search(user_message.lower())
    asked_bps = min(int(pct.group(1)) * 100, 10_000) if pct else FALLBACK_OPENING_BPS

    qty = 1
    for raw in _QTY.findall(user_message):
        value = int(raw)
        if 1 <= value <= toolsmod.MAX_QTY and not (pct and value == int(pct.group(1))):
            qty = value
            break

    _run_tool(tool_map, "propose_offer", {
        "sku": best.sku, "qty": qty, "discount_bps": asked_bps,
        "message": "", "rationale": "deterministic tier-3 responder",
    })

    decision = ctx.last_decision
    if decision is None:
        reply = f"{best.name} is {best.price_paise / 100:,.2f} rupees."
    else:
        # Same sentence the tool-calling path falls back to, so a shopper
        # cannot tell from the wording which tier answered them.
        reply = gate_sentence(ctx)

    return AgentResult(
        reply=reply,
        provider="deterministic",
        model=None,
        latency_ms=int((time.monotonic() - started) * 1000),
        steps=1,
        tool_calls=list(ctx.calls),
    )


async def run_agent(
    ctx: OfferContext,
    *,
    system_prompt: str,
    transcript: list[dict[str, Any]],
    user_message: str,
) -> AgentResult:
    """Try each configured provider in order, then the deterministic tier."""
    history = _history_messages(transcript)
    errors: list[str] = []

    for spec in llmmod.provider_chain():
        # A fresh context per attempt: a half-finished tool trail from a
        # provider that timed out must not be attributed to the next one.
        # The tier fields were missing from this copy, and their absence was
        # silent: every attempt ran at the default cap_fraction of 10000, so
        # get_customer_standing always said "new shopper" and the customer cap
        # was never applied during the NEGOTIATION -- while payment_service.
        # accept() applied it faithfully. A shopper could be quoted a rate the
        # checkout would then refuse. Copy the whole context.
        attempt_ctx = OfferContext(
            catalog=ctx.catalog,
            slot_ceiling_bps=ctx.slot_ceiling_bps,
            slot_status=ctx.slot_status,
            campaign_status=ctx.campaign_status,
            campaign_max_discount_bps=ctx.campaign_max_discount_bps,
            margin_floor_bps=ctx.margin_floor_bps,
            budget_paise=ctx.budget_paise,
            spent_paise=ctx.spent_paise,
            reserved_paise=ctx.reserved_paise,
            turn_count=ctx.turn_count,
            max_turns=ctx.max_turns,
            tier_key=ctx.tier_key,
            tier_cap_fraction_bps=ctx.tier_cap_fraction_bps,
            tier_stats=ctx.tier_stats,
            cart=ctx.cart,
        )
        try:
            result = await _attempt(spec, attempt_ctx, system_prompt, history, user_message)
        except Exception as exc:  # noqa: BLE001 - any failure means "next tier"
            llmmod.record_failure(spec.name)
            errors.append(f"{spec.name}: {type(exc).__name__}: {exc}"[:200])
            log.warning("provider %s failed: %s", spec.name, exc)
            continue

        llmmod.record_success(spec.name)
        ctx.calls = attempt_ctx.calls
        ctx.last_decision = attempt_ctx.last_decision
        ctx.last_sku = attempt_ctx.last_sku
        ctx.last_qty = attempt_ctx.last_qty
        # Both of these were dropped on the floor. last_addon meant an upsell
        # that went through the gate was never recorded in the audit log;
        # cart_ops would mean an item the shopper was told they had never
        # reached their basket.
        ctx.last_addon = attempt_ctx.last_addon
        ctx.cart_ops = attempt_ctx.cart_ops
        return result

    result = deterministic_reply(ctx, user_message)
    result.error = "; ".join(errors) or "no provider configured"
    return result
