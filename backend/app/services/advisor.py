"""Two places the model writes prose over numbers it did not invent.

Both follow the same rule, and it is the same rule as the negotiation gate:
the model proposes, something deterministic disposes. A model that invents a
rupee figure in a financial summary is worse than no summary, so every number
in both outputs is computed here and handed to the model as fact.

  advise()     -- the model proposes campaign settings; simulate.simulate()
                  checks them and hands back warnings; the model revises. Up to
                  two rounds, then whatever it produced is returned with the
                  simulation attached so a shopkeeper sees both.

  postmortem() -- aggregates are computed in SQL and Python; the model only
                  turns them into sentences.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core import llm as llmmod
from app.core.config import settings
from app.core.db import DbBackend
from app.services import simulate

log = logging.getLogger("kirana.advisor")

MAX_ROUNDS = 2


async def _ask(prompt: str, system: str) -> str | None:
    """One shot at whichever provider answers. Returns None if none do.

    No tools and no loop: this is prose generation, and the numbers are already
    settled. A failure here degrades the feature to its computed half rather
    than failing the request -- the simulation and the aggregates are the
    useful part, the prose is the wrapper.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    for spec in llmmod.provider_chain():
        try:
            # A longer read timeout than a negotiation turn gets. Both callers
            # here hand the model the whole catalogue and ask for structured
            # JSON, which takes far more than the 12 seconds a haggle allows --
            # and the failure looked identical to the provider being down, so
            # the console reported "not reachable" while /health/llm showed the
            # same provider answering in under two seconds.
            model = llmmod.build_chat_model(
                spec, read_timeout_s=settings.LLM_ADVISOR_TIMEOUT_S
            )
            reply = await model.ainvoke(
                [SystemMessage(content=system), HumanMessage(content=prompt)]
            )
            llmmod.record_success(spec.name)
            return reply.content if isinstance(reply.content, str) else str(reply.content)
        except Exception as exc:  # noqa: BLE001 - try the next tier
            llmmod.record_failure(spec.name)
            log.warning("advisor provider %s failed: %s", spec.name, exc)
    return None


# --------------------------------------------------------------- advise --
ADVISE_SYSTEM = """You help a small Indian grocer plan a discount campaign.

You will be given the shop's real products with their margins, and the results
of any past campaigns. Propose four numbers:

  budget_paise       total to give away, in paise
  max_discount_bps   the highest any sticker may go, in basis points
  margin_floor_bps   the profit the shop refuses to go below, in basis points
  slot_count         how many stickers to print

Reply with ONLY a JSON object with those four integer keys and a fifth key
"reasoning" holding one short sentence per number, as an object keyed by the
same names. No prose outside the JSON, no markdown fences.

The margin floor is the number that surprises people: set it above an item's
margin and that item can never be discounted at all. Look at the margins you
are given before choosing it."""


def _parse_proposal(raw: str) -> dict[str, Any] | None:
    """Models add fences and prose no matter what the prompt says."""
    text = raw.strip()
    if "```" in text:
        chunks = [c for c in text.split("```") if "{" in c]
        text = chunks[0] if chunks else text
        text = text.removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _sane(p: dict[str, Any]) -> dict[str, int] | None:
    """Clamp to the API's own bounds. The model is a proposer here exactly as
    it is in a negotiation, and the same principle applies: never store a
    number from it without checking the range first."""
    try:
        return {
            "budget_paise": max(100, min(int(p["budget_paise"]), 100_000_000)),
            "max_discount_bps": max(0, min(int(p["max_discount_bps"]), 10_000)),
            "margin_floor_bps": max(0, min(int(p["margin_floor_bps"]), 9_000)),
            "slot_count": max(1, min(int(p["slot_count"]), 512)),
        }
    except (KeyError, TypeError, ValueError):
        return None


async def advise(db: DbBackend, merchant_id: str) -> dict[str, Any]:
    """Propose campaign settings, then check them against the simulator."""
    catalog = await db.rpc("list_catalog", {"p_merchant_id": merchant_id, "p_all": False})
    if not catalog:
        raise ValueError("EMPTY_CATALOG")

    past = await db.rpc("list_merchant_campaigns", {"p_merchant_id": merchant_id}) or []

    margins = "\n".join(
        f"  {c['sku']}: {c['name']}, sells at Rs {c['price_paise'] / 100:.2f}, "
        f"margin {c['margin_bps'] / 100:.2f}%"
        for c in catalog
    )
    history = "\n".join(
        f"  {c['name']}: budget Rs {c['budget_paise'] / 100:,.0f}, "
        f"spent Rs {c['spent_paise'] / 100:,.0f}, "
        f"{c['slots_redeemed']} of {c['slots_total']} stickers redeemed"
        for c in past[:5]
    ) or "  (no past campaigns)"

    prompt = f"The shop's products:\n{margins}\n\nPast campaigns:\n{history}"
    proposal: dict[str, int] | None = None
    sim: dict[str, Any] | None = None
    rounds: list[dict[str, Any]] = []
    reasoning: dict[str, Any] = {}

    for attempt in range(MAX_ROUNDS):
        raw = await _ask(prompt, ADVISE_SYSTEM)
        if raw is None:
            break
        parsed = _parse_proposal(raw)
        candidate = _sane(parsed) if parsed else None
        if candidate is None:
            break

        sim = simulate.simulate(
            catalog,
            max_discount_bps=candidate["max_discount_bps"],
            margin_floor_bps=candidate["margin_floor_bps"],
            budget_paise=candidate["budget_paise"],
            slot_count=candidate["slot_count"],
        )
        proposal = candidate
        reasoning = (parsed or {}).get("reasoning", {}) if parsed else {}
        rounds.append({"proposal": candidate, "warnings": sim["warnings"]})

        blocking = [w for w in sim["warnings"] if w["level"] in {"warn", "stop"}]
        if not blocking or attempt == MAX_ROUNDS - 1:
            break

        # The simulator disposes: hand the model its own consequences.
        issues = "\n".join(f"  - {w['message']}" for w in blocking)
        prompt = (
            f"{prompt}\n\nYour previous proposal was:\n"
            f"{json.dumps(candidate)}\n\nIt produced these problems:\n{issues}\n\n"
            "Revise the four numbers to avoid them. Same JSON format."
        )

    if proposal is None:
        raise RuntimeError("ADVISOR_UNAVAILABLE")

    return {
        "proposal": proposal,
        "reasoning": reasoning,
        "simulation": sim,
        "rounds": len(rounds),
        # Said out loud: the numbers were checked, not merely generated.
        "note": (
            "Proposed by the assistant and checked against your real margins "
            "with the same rules the live gate uses. Nothing is committed until "
            "you press commit."
        ),
    }


# ------------------------------------------------------------ postmortem --
POSTMORTEM_SYSTEM = """You are writing a short debrief for a small Indian
grocer about a discount campaign that has run.

You will be given computed figures. Use ONLY those figures -- never invent a
number, a product, or a percentage. Write three short paragraphs:

  1. What happened: money given away, stickers used.
  2. What the rules did: which limit bound most often, and what that cost.
  3. What to change next time: one concrete suggestion.

Plain English, no markdown, no bullet points. Around 120 words."""


def _aggregate(campaign: dict[str, Any], rows: list[dict[str, Any]],
               sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Every number the debrief may mention, computed here."""
    kinds: dict[str, int] = {}
    binds: dict[str, int] = {}
    asked_total = granted_total = clamped_n = 0

    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        if r.get("binding_constraint"):
            binds[r["binding_constraint"]] = binds.get(r["binding_constraint"], 0) + 1
        if r.get("proposed_bps") is not None and r.get("granted_bps") is not None:
            asked_total += r["proposed_bps"]
            granted_total += r["granted_bps"]
            if r["granted_bps"] < r["proposed_bps"]:
                clamped_n += 1

    # A shopper on a bound sticker who asked about something outside its scope
    # is a merchandising signal, and it falls out of Phase C's withheld_skus.
    bound_sessions = [s for s in sessions if s.get("withheld_skus")]

    return {
        "budget_paise": campaign["budget_paise"],
        "spent_paise": campaign["spent_paise"],
        "unspent_paise": campaign["budget_paise"] - campaign["spent_paise"],
        "spent_pct": round(campaign["spent_paise"] * 100 / max(campaign["budget_paise"], 1), 1),
        "slots_total": campaign["slots_total"],
        "slots_redeemed": campaign["slots_redeemed"],
        "slots_verified": campaign["slots_verified"],
        "conversations": len(sessions),
        "decisions": len(rows),
        "kinds": kinds,
        "clamped_count": clamped_n,
        "binding_counts": binds,
        "most_common_bind": max(binds, key=binds.get) if binds else None,
        "avg_asked_bps": round(asked_total / max(clamped_n or len(rows), 1)),
        "avg_granted_bps": round(granted_total / max(clamped_n or len(rows), 1)),
        "bound_sticker_sessions": len(bound_sessions),
    }


def _sessions_of(audit: Any) -> list[dict[str, Any]]:
    """The session list, whichever shape the database handed back.

    The paginated function returns an envelope, {total, returned, offset,
    sessions}; the one it replaced returned a bare array. Iterating the
    envelope as if it were the array yields its KEYS -- four strings -- and the
    first .get() on one raises AttributeError, which is the same failure the
    qr-sheet route hit for the same reason: a shape changed underneath a caller
    that was never updated with it.
    """
    if isinstance(audit, dict):
        return audit.get("sessions") or []
    return audit or []


async def postmortem(db: DbBackend, campaign_id: str) -> dict[str, Any]:
    campaign = await db.rpc("get_campaign", {"p_campaign_id": campaign_id})
    if campaign is None:
        raise ValueError("CAMPAIGN_NOT_FOUND")

    feed = await db.rpc(
        "get_audit_feed",
        {"p_campaign_id": campaign_id, "p_after_id": 0, "p_limit": 200},
    )
    # All three arguments, named. Production carries TWO get_session_audit
    # overloads: the original (uuid) from 010_audit.sql, and the paginated
    # (uuid, int, int) whose limit/offset default to 500/0. A call naming only
    # p_campaign_id matches both, so PostgREST answers 300 Multiple Choices and
    # this endpoint 500s -- which reaches the browser as a CORS error, because
    # an unhandled 500 is produced outside the CORS middleware and carries no
    # Access-Control-Allow-Origin header. Naming p_limit and p_offset picks the
    # paginated one unambiguously, and keeps working after the stale overload
    # is dropped in 024.
    audit = await db.rpc(
        "get_session_audit",
        {"p_campaign_id": campaign_id, "p_limit": 500, "p_offset": 0},
    )
    sessions = _sessions_of(audit)
    figures = _aggregate(campaign, (feed or {}).get("items") or [], sessions)

    prose = await _ask(
        f"Campaign “{campaign['name']}”. Figures:\n{json.dumps(figures, indent=2)}",
        POSTMORTEM_SYSTEM,
    )

    return {
        "campaign": {"id": campaign["id"], "name": campaign["name"],
                     "status": campaign["status"]},
        "figures": figures,
        # Degrades honestly: without a provider the shopkeeper still gets every
        # number, just without the sentences around them.
        "summary": prose,
        "summary_available": prose is not None,
    }
