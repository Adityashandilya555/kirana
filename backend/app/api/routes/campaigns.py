"""Campaign creation, the commit ceremony, and the printable sheet."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.api.deps import DbDep, MerchantKey, require_merchant_key
from app.core.config import settings
from app.core.db import RpcError
from app.services import advisor, campaign_service, qr_service

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


class TierBody(BaseModel):
    """Who counts as a regular, and what everyone else gets instead.

    Draft-only, like the ceilings: once stickers are printed the promise is
    fixed, and moving the qualifying line afterwards would silently re-price
    codes already sitting on shelves.
    """

    min_txn_count: int = Field(default=0, ge=0, le=1_000)
    min_spend_paise: int = Field(default=0, ge=0, le=100_000_000)
    #: None is lifetime. Days rather than an enum: "three weeks" is just 21.
    window_days: int | None = Field(default=None, ge=1, le=3_650)
    #: What a shopper who does not qualify may reach, as a fraction of each
    #: product's own cap. 10000 = no reduction, which is the default so a
    #: campaign that never calls this behaves exactly as before.
    base_cap_fraction_bps: int = Field(default=10_000, ge=0, le=10_000)


class CommitBody(BaseModel):
    """What each sticker is scoped to, distributed round-robin across the sheet.

    Ignored when the campaign's binding is 'open'. For 'product' these are
    skus; for 'shelf' they are shelf ids.
    """

    targets: list[str] = Field(default_factory=list)

    #: 'tiered' keeps the old per-sticker ceilings from plan_ceilings and
    #: commits nothing per product -- byte-identical to before caps existed,
    #: which is why it is the default. 'margin' additionally freezes a ceiling
    #: for every product, derived from the margin floor, under its own root.
    ceiling_mode: str = Field(default="tiered", pattern="^(tiered|margin)$")

    #: 'once' is a coupon: the first person to buy with it kills it for
    #: everyone. 'shared' is a shelf fixture: many people scan the same sticker
    #: over its life, and what gets used up is one discount per CUSTOMER per
    #: campaign rather than the sticker itself.
    #:
    #: Default 'once' so nothing existing changes. Shared stickers are only
    #: meaningful once shoppers are identified, which is why this is opt-in
    #: rather than the new default.
    sticker_sharing: str = Field(default="once", pattern="^(once|shared)$")


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


@router.post("/campaigns/{campaign_id}/tier")
async def set_campaign_tier(
    campaign_id: str, body: TierBody, db: DbDep, _: MerchantKey,
) -> dict:
    """Set the qualifying rule. Draft only; 409 once committed.

    Recorded but not yet spent: the band is evaluated and written on every scan
    from here on, and nothing reads it when pricing. That is deliberate, so a
    shopkeeper can watch who their rule actually catches before it starts
    costing them margin.
    """
    try:
        return await db.rpc("set_campaign_tier", {
            "p_campaign_id": campaign_id,
            "p_min_txn_count": body.min_txn_count,
            "p_min_spend_paise": body.min_spend_paise,
            "p_window_days": body.window_days,
            "p_base_cap_fraction_bps": body.base_cap_fraction_bps,
        })
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
            db, campaign_id, binding=binding, targets=targets,
            ceiling_mode=(body.ceiling_mode if body else "tiered"),
            sticker_sharing=(body.sticker_sharing if body else "once"),
        )
    except RpcError as exc:
        raise _rpc_http(exc) from exc
    except ValueError as exc:
        # NO_PRODUCTS is its own answer: margin mode with an empty catalogue
        # has nothing to derive a ceiling from, and saying "campaign not
        # found" would send the shopkeeper looking in the wrong place.
        if str(exc) == "NO_PRODUCTS":
            raise HTTPException(
                status_code=422,
                detail={"code": "NO_PRODUCTS",
                        "message": "Add some products before committing a "
                                   "margin-based campaign — there is nothing "
                                   "to work out a ceiling from."},
            ) from exc
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


@router.post("/campaigns/advise")
async def advise(db: DbDep, _: MerchantKey,
                 merchant_id: str = DEMO_MERCHANT_ID) -> dict:
    """Propose campaign settings, checked against the simulator before returning.

    The model proposes; simulate.simulate() disposes. Same shape as the
    negotiation gate, which is what makes it explainable in one sentence.
    """
    try:
        return await advisor.advise(db, merchant_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": str(exc),
                    "message": "Add some products before planning a campaign."},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ADVISOR_UNAVAILABLE",
                    "message": "The assistant is not reachable just now. "
                               "Set the numbers yourself and the preview will "
                               "still check them."},
        ) from exc


@router.get("/campaigns/{campaign_id}/postmortem")
async def postmortem(campaign_id: str, db: DbDep, _: MerchantKey) -> dict:
    """What happened, in numbers the model did not invent."""
    try:
        return await advisor.postmortem(db, campaign_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": str(exc), "message": "No such campaign."},
        ) from exc


@router.get("/campaigns/{campaign_id}/sessions")
async def session_audit(
    campaign_id: str, db: DbDep, _: MerchantKey,
    limit: int = Query(default=500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Conversation-level context the decision rows do not carry.

    Chiefly `withheld_skus`: the products this slot's scope kept out of the
    model's world. Binding is enforced by omission rather than instruction, and
    until this endpoint nothing in the product could show that.

    Paginated, and the response carries `total` so the caller can tell a short
    page from the end of the data. A silent cap meant older threads rendered
    with no scope panel at all and nothing saying why.
    """
    return await db.rpc(
        "get_session_audit",
        {"p_campaign_id": campaign_id, "p_limit": limit, "p_offset": offset},
    ) or {"total": 0, "returned": 0, "offset": offset, "sessions": []}


@router.get("/campaigns/{campaign_id}/qr-sheet", response_class=HTMLResponse)
async def qr_sheet(
    campaign_id: str, request: Request, db: DbDep, k: str = Query(default=""),
) -> HTMLResponse:
    """Printable sheet. Auth by query param because this is opened in a browser
    tab and sent to a printer, where a custom header is not available.

    require_merchant_key is called directly rather than as a dependency, which
    is what makes the query-param key possible -- and is also why the request
    has to be threaded through by hand. It takes `request` first (the throttle
    is scoped per caller, and the caller is read off the headers), so passing
    only the key silently binds the key string to `request` and blows up inside
    _caller with "'str' object has no attribute 'headers'" on every single
    print. Both arguments, in order, or this route 500s for everyone.
    """
    await require_merchant_key(request, k)
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
