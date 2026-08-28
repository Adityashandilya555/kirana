"""Agent-to-agent commerce: discovery, machine catalog, signed quotes.

Public by design. An AI buyer that has never spoken to this shop must be able
to read the discovery document, fetch the catalog and get a verifiable price
without credentials -- that is what "transactable by an AI buyer" means.

What makes that safe is that none of it moves money or reserves anything. A
quote is advisory; reserving budget is still gated behind a session and a
second run of bounds.check(). The one thing a caller needs to hold is a slot
token, which is the same credential a human gets by scanning the sticker.

cost_paise appears nowhere here. The catalog is projected in SQL by
get_agent_catalog, which cannot leak it because it does not select it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import DbDep
from app.core import bounds
from app.core.config import settings
from app.core.db import RpcError
from app.services import agent_commerce
from app.services.agent_commerce import QuoteError
from app.services.decision_log import record
from app.core.codes import DecisionKind

log = logging.getLogger("kirana.agent")

router = APIRouter(tags=["agent"])

DEMO_MERCHANT_ID = "00000000-0000-0000-0000-00000000d001"


class QuoteBody(BaseModel):
    slot_token: str = Field(min_length=4, max_length=32)
    sku: str = Field(min_length=1, max_length=32)
    qty: int = Field(default=1, ge=1, le=bounds.MAX_QTY)


class VerifyBody(BaseModel):
    """A quote handed back to us for checking.

    Offered as a convenience, not as the mechanism: the whole point is that a
    buyer can verify independently. This endpoint exists so a demo can show the
    check passing without writing a client first, and it runs exactly the code
    a buyer would.
    """

    quote: dict
    signature: dict | None = None


def _base_url(request: Request) -> str:
    configured = settings.PUBLIC_API_BASE_URL.strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


@router.get("/.well-known/agent-commerce.json")
async def discovery(request: Request, db: DbDep) -> dict:
    """What an AI buyer reads first."""
    merchant = await db.rpc("get_merchant_by_name", {"p_name": "Sharma Kirana Store"})
    campaigns = await db.rpc(
        "list_merchant_campaigns", {"p_merchant_id": DEMO_MERCHANT_ID}
    )
    live = next((c for c in (campaigns or []) if c.get("status") == "live"), None)
    return agent_commerce.discovery(
        _base_url(request), merchant or {}, (live or {}).get("merkle_root")
    )


@router.get("/api/v1/agent/catalog")
async def agent_catalog(db: DbDep, merchant_id: str = DEMO_MERCHANT_ID) -> dict:
    """The machine catalog. List prices only -- what a sticker can discount
    them to is answered by /quote, because that depends on the sticker."""
    return await db.rpc("get_agent_catalog", {"p_merchant_id": merchant_id}) or {}


@router.post("/api/v1/agent/quote")
async def quote(body: QuoteBody, db: DbDep) -> dict:
    """Price one line item against one sticker, with proof.

    Runs the same bounds.check() the chat path runs, on the same live campaign
    state. There is deliberately no separate rule engine for agents: a
    divergence would mean a discount a machine could obtain that a human could
    not, which is precisely the failure this project exists to prevent.
    """
    try:
        ctx = await db.rpc(
            "get_slot_quote_context", {"p_slot_token": body.slot_token}
        )
    except RpcError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    if not ctx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SLOT_NOT_FOUND", "message": "No such code."},
        )

    try:
        result = agent_commerce.build_quote(ctx, body.sku, body.qty)
    except QuoteError as exc:
        # A refusal is a legitimate answer to a machine, same as to a person,
        # so it is logged and returned as a structured 409 rather than a 500.
        await record(
            db,
            campaign_id=str(ctx["campaign"]["id"]),
            kind=DecisionKind.REJECTED,
            code=exc.code,
            human_reason=f"Agent quote refused for {body.sku}: {exc.message}",
            customer_reason=exc.message,
            meta={"channel": "agent", "sku": body.sku, "qty": body.qty},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    q = result["quote"]
    await record(
        db,
        campaign_id=str(ctx["campaign"]["id"]),
        kind=DecisionKind.AGENT_QUOTE,
        code="A01_QUOTE_ISSUED",
        human_reason=(
            f"Machine buyer quoted {q['sku']} x{q['qty']} at "
            f"{q['granted_bps'] / 100:g}% off "
            f"({'signed' if result['signed'] else 'UNSIGNED'}), "
            f"held by {q['binding_constraint'] or 'nothing'}."
        ),
        granted_bps=q["granted_bps"],
        binding_constraint=q["binding_constraint"],
        meta={"channel": "agent", "expires_at": q["expires_at"]},
    )
    return result


@router.post("/api/v1/agent/verify")
async def verify_quote(body: VerifyBody) -> dict:
    """Re-run a buyer's three checks on a quote they hold.

    Purely a convenience: it needs no database and asserts nothing we could not
    have lied about. A buyer who wants an answer they can rely on runs the same
    checks themselves against the published public key.
    """
    return agent_commerce.self_check(
        {"quote": body.quote, "signature": body.signature}
    )
