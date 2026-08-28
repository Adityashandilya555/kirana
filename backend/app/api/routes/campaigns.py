"""Campaign creation, the commit ceremony, and the printable sheet."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.api.deps import DbDep, MerchantKey, require_merchant_key
from app.core.config import settings
from app.core.db import RpcError
from app.services import campaign_service, qr_service

router = APIRouter(prefix="/api/v1", tags=["campaigns"])

DEMO_MERCHANT_ID = "00000000-0000-0000-0000-00000000d001"


class CreateCampaign(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    budget_paise: int = Field(gt=0, le=100_000_000)
    max_discount_bps: int = Field(ge=0, le=10_000)
    margin_floor_bps: int = Field(ge=0, le=9_000)
    max_turns: int = Field(default=6, ge=1, le=20)
    slot_count: int = Field(default=24, ge=1, le=512)
    merchant_id: str = DEMO_MERCHANT_ID
    #: open  = any product · product = one sku per sticker · shelf = a curated set
    slot_binding: str = Field(default="open", pattern="^(open|product|shelf)$")


class CommitBody(BaseModel):
    """What each sticker is scoped to, distributed round-robin across the sheet.

    Ignored when the campaign's binding is 'open'. For 'product' these are
    skus; for 'shelf' they are shelf ids.
    """

    targets: list[str] = Field(default_factory=list)


def _rpc_http(exc: RpcError) -> HTTPException:
    conflicts = {
        "ALREADY_COMMITTED", "CEILING_ABOVE_CAMPAIGN_MAX", "SLOT_COUNT_MISMATCH",
    }
    not_found = {"CAMPAIGN_NOT_FOUND", "MERCHANT_NOT_FOUND"}
    code = (
        status.HTTP_404_NOT_FOUND if exc.code in not_found
        else status.HTTP_409_CONFLICT if exc.code in conflicts
        else status.HTTP_400_BAD_REQUEST
    )
    return HTTPException(status_code=code,
                         detail={"code": exc.code, "message": exc.message})


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
async def create_campaign(body: CreateCampaign, db: DbDep, _: MerchantKey) -> dict:
    try:
        return await campaign_service.create_campaign(
            db,
            merchant_id=body.merchant_id,
            name=body.name,
            budget_paise=body.budget_paise,
            max_discount_bps=body.max_discount_bps,
            margin_floor_bps=body.margin_floor_bps,
            max_turns=body.max_turns,
            slot_count=body.slot_count,
            slot_binding=body.slot_binding,
        )
    except RpcError as exc:
        raise _rpc_http(exc) from exc


@router.post("/campaigns/{campaign_id}/commit")
async def commit_campaign(
    campaign_id: str, db: DbDep, _: MerchantKey, body: CommitBody | None = None,
) -> dict:
    """Irreversible. After this the ceilings cannot move without the root moving."""
    campaign = await db.rpc("get_campaign", {"p_campaign_id": campaign_id})
    if campaign is None:
        raise HTTPException(status_code=404,
                            detail={"code": "CAMPAIGN_NOT_FOUND", "message": "No such campaign."})
    binding = campaign.get("slot_binding", "open")
    targets = list((body.targets if body else []) or [])

    if binding != "open" and not targets:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "NO_TARGETS",
                "message": (
                    "This campaign binds each sticker to a "
                    + ("product" if binding == "product" else "shelf")
                    + ", so it needs at least one to bind to."
                ),
            },
        )

    try:
        result = await campaign_service.commit_campaign(
            db, campaign_id, binding=binding, targets=targets
        )
    except RpcError as exc:
        raise _rpc_http(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404,
                            detail={"code": str(exc), "message": "Campaign not found."}) from exc
    campaign = result["campaign"]
    return {
        **campaign,
        "qr_sheet_url": f"/api/v1/campaigns/{campaign_id}/qr-sheet",
        "slots_created": result["commit"]["slots"],
    }


@router.get("/campaigns")
async def list_campaigns(
    db: DbDep, _: MerchantKey, merchant_id: str = DEMO_MERCHANT_ID,
) -> list[dict]:
    """Newest first. The console's home screen."""
    return await db.rpc("list_merchant_campaigns", {"p_merchant_id": merchant_id}) or []


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, db: DbDep, _: MerchantKey) -> dict:
    campaign = await db.rpc("get_campaign", {"p_campaign_id": campaign_id})
    if campaign is None:
        raise HTTPException(status_code=404,
                            detail={"code": "CAMPAIGN_NOT_FOUND", "message": "No such campaign."})
    return campaign


@router.get("/campaigns/{campaign_id}/slots")
async def list_slots(campaign_id: str, db: DbDep, _: MerchantKey) -> list[dict]:
    return await db.rpc("list_campaign_slots", {"p_campaign_id": campaign_id})


@router.get("/campaigns/{campaign_id}/audit")
async def audit_feed(
    campaign_id: str,
    db: DbDep,
    _: MerchantKey,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    return await db.rpc(
        "get_audit_feed",
        {"p_campaign_id": campaign_id, "p_after_id": after_id, "p_limit": limit},
    )


@router.get("/campaigns/{campaign_id}/sessions")
async def session_audit(campaign_id: str, db: DbDep, _: MerchantKey) -> list[dict]:
    """Conversation-level context the decision rows do not carry.

    Chiefly `withheld_skus`: the products this slot's scope kept out of the
    model's world. Binding is enforced by omission rather than instruction, and
    until this endpoint nothing in the product could show that.
    """
    return await db.rpc("get_session_audit", {"p_campaign_id": campaign_id}) or []


@router.get("/campaigns/{campaign_id}/qr-sheet", response_class=HTMLResponse)
async def qr_sheet(campaign_id: str, db: DbDep, k: str = Query(default="")) -> HTMLResponse:
    """Printable sheet. Auth by query param because this is opened in a browser
    tab and sent to a printer, where a custom header is not available."""
    await require_merchant_key(k)
    campaign = await db.rpc("get_campaign", {"p_campaign_id": campaign_id})
    if campaign is None:
        raise HTTPException(status_code=404,
                            detail={"code": "CAMPAIGN_NOT_FOUND", "message": "No such campaign."})
    if not campaign.get("merkle_root"):
        raise HTTPException(
            status_code=409,
            detail={"code": "NOT_COMMITTED",
                    "message": "Commit the campaign before printing. Printing an "
                               "uncommitted sheet would put codes on paper that "
                               "nothing has promised anything about."},
        )
    slots = await db.rpc("list_campaign_slots", {"p_campaign_id": campaign_id})
    return HTMLResponse(
        qr_service.render_sheet(campaign, slots, settings.PUBLIC_APP_BASE_URL)
    )
