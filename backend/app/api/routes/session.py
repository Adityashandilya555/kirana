"""Customer-facing session routes.

There is no merchant key here. The slot token IS the credential: it is a
128-bit-derived Crockford-base32 string printed on one physical sticker, and
holding it is what a scan proves.

Everything returned here crosses to a browser, so the public projection is
explicit and allowlisted rather than a blocklist over the rpc payload.
`cost_paise` in particular is present in get_session_context -- bounds.check()
needs it -- and must never leave the server.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DbDep
from app.core.db import RpcError
from app.services import chat_service, customer_service

router = APIRouter(prefix="/api/v1", tags=["session"])


class OpenSession(BaseModel):
    slot_token: str = Field(min_length=4, max_length=32)
    transport: str = Field(default="web", pattern="^(web|telegram)$")
    transport_ref: str | None = None
    #: Optional, and optional on purpose. A shopper who will not give a number
    #: still gets to haggle -- they simply cannot be recognised as a returning
    #: customer, so they are treated as new. Refusing to serve them would be a
    #: worse shop than one that does not know who they are.
    phone: str | None = Field(default=None, max_length=24)


def public_context(ctx: dict[str, Any]) -> dict[str, Any]:
    """Allowlist. Adding a field here is a deliberate act."""
    campaign, slot, session = ctx["campaign"], ctx["slot"], ctx["session"]
    return {
        "session": {
            "id": session.get("id"),
            "status": session.get("status"),
            "turn_count": session.get("turn_count", 0),
            "current_sku": session.get("current_sku"),
            "current_qty": session.get("current_qty", 1),
            "offer_bps": session.get("offer_bps"),
            "offer_amount_paise": session.get("offer_amount_paise"),
        },
        "slot": {
            # NOT ceiling_bps: the shopper's own page must not reveal the
            # number the agent is being held to, or the negotiation is theatre.
            "slot_token": slot.get("slot_token"),
            "status": slot.get("status"),
        },
        "campaign": {
            "name": campaign.get("name"),
            "status": campaign.get("status"),
            "max_turns": campaign.get("max_turns"),
        },
        "merchant": ctx.get("merchant", {}),
        "catalog": [
            {
                "sku": c["sku"], "name": c["name"], "unit": c["unit"],
                "price_paise": c["price_paise"],
            }
            for c in ctx.get("catalog") or []
        ],
        "transcript": ctx.get("transcript") or [],
    }


def _rpc_http(exc: RpcError) -> HTTPException:
    not_found = {"SLOT_NOT_FOUND", "SESSION_NOT_FOUND"}
    conflict = {"SLOT_NOT_OPEN", "CAMPAIGN_NOT_LIVE"}
    code = (
        status.HTTP_404_NOT_FOUND if exc.code in not_found
        else status.HTTP_409_CONFLICT if exc.code in conflict
        else status.HTTP_400_BAD_REQUEST
    )
    messages = {
        "SLOT_NOT_FOUND": "That code is not one of ours. Check the sticker and retype it.",
        "SLOT_NOT_OPEN": "This code has already been used.",
        "CAMPAIGN_NOT_LIVE": "This offer has ended.",
    }
    return HTTPException(
        status_code=code,
        detail={"code": exc.code, "message": messages.get(exc.code, exc.message)},
    )


@router.post("/sessions", status_code=status.HTTP_200_OK)
async def open_session(body: OpenSession, db: DbDep) -> dict:
    """Open or RESUME the session for a scanned slot token.

    200 rather than 201 because this is idempotent: a phone reload returns the
    same session with its transcript, and the partial unique index on
    sessions(slot_id) makes a second live row impossible anyway.
    """
    # Normalised here rather than in SQL so the rule is one pure function with
    # its own tests. A number that will not fold is dropped, not rejected: the
    # shopper still gets a session, just an unidentified one.
    phone_e164 = customer_service.normalize_phone(body.phone)

    try:
        result = await chat_service.open_session(
            db, body.slot_token, transport=body.transport,
            transport_ref=body.transport_ref, phone_e164=phone_e164,
        )
    except RpcError as exc:
        raise _rpc_http(exc) from exc

    customer = result.get("customer") or {}
    return {
        "session_id": result["session_id"],
        "resumed": result.get("resumed", False),
        # Echoed back so the phone can say "welcome back" -- and deliberately
        # only the last four digits, never the number the browser just sent.
        "customer": {
            "identified": bool(customer.get("id")),
            "phone_last4": customer.get("phone_last4"),
            "returning": bool(customer.get("returning")),
        },
        # True when a number was supplied and could not be read. The UI uses
        # this to say so rather than silently treating them as a stranger.
        "phone_unreadable": bool(body.phone) and phone_e164 is None,
        **public_context(result["context"]),
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, db: DbDep) -> dict:
    ctx = await chat_service.load_context(db, session_id)
    if ctx is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SESSION_NOT_FOUND", "message": "No such session."},
        )
    return {"session_id": session_id, **public_context(ctx)}
