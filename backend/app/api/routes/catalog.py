"""Catalog, shelves, and the pre-commit simulator.

All merchant-key protected. Nothing here is reachable from a customer's phone,
and it deliberately handles `cost_paise`, which never crosses to a shopper.

The import is deliberately two calls rather than one. `preview` parses and
validates and writes nothing; `import` does the same work and then commits.
An upload that silently half-applied would leave a shop with a catalog it did
not choose, and the shopkeeper has no undo.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.api.deps import DbDep, MerchantKey
from app.core import bounds
from app.core.db import RpcError
from app.services import catalog_service, simulate
from app.services.catalog_service import SheetError

log = logging.getLogger("kirana.catalog")

router = APIRouter(prefix="/api/v1", tags=["catalog"])

DEMO_MERCHANT_ID = "00000000-0000-0000-0000-00000000d001"

#: A shopkeeper's price list is small. This is a guard against someone
#: uploading a video, not a real capacity limit.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024


class ShelfBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    skus: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=200)
    shelf_id: str | None = None
    merchant_id: str = DEMO_MERCHANT_ID


class SimulateBody(BaseModel):
    max_discount_bps: int = Field(ge=0, le=bounds.MAX_BPS)
    margin_floor_bps: int = Field(ge=0, le=9_000)
    budget_paise: int = Field(gt=0, le=100_000_000)
    slot_count: int = Field(ge=1, le=512)
    merchant_id: str = DEMO_MERCHANT_ID


class ItemsBody(BaseModel):
    """Manual add/edit, for the shop with eleven products and no spreadsheet.

    max_length matches the spreadsheet path's MAX_ROWS. Without it this route
    was the same denial-of-service as an oversized upload, through a different
    door -- an unbounded list straight into a single upsert.
    """

    items: list[dict[str, Any]] = Field(max_length=catalog_service.MAX_ROWS)
    replace: bool = False
    merchant_id: str = DEMO_MERCHANT_ID


_TOO_LARGE = HTTPException(
    status_code=413,
    detail={
        "code": "FILE_TOO_LARGE",
        "message": "That file is larger than 4 MB. A price list should be far smaller.",
    },
)


async def _read_upload(request: Request, file: UploadFile) -> bytes:
    """Read the upload without ever holding more than the cap in memory.

    The obvious form -- `blob = await file.read()` then check `len(blob)` --
    checks the size only after the entire body has been materialised, and
    Starlette applies no default body limit. A 2 GB POST was fully buffered
    before the 4 MB check ran, so one request could exhaust the container.

    Content-Length is a client-supplied claim, so it is a cheap first refusal
    and not the real bound; the chunked loop below is what actually holds.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise _TOO_LARGE

    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(64 * 1024):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise _TOO_LARGE
        chunks.append(chunk)

    blob = b"".join(chunks)
    if not blob:
        raise HTTPException(
            status_code=400,
            detail={"code": "EMPTY_FILE", "message": "That file is empty."},
        )
    return blob


def _parse_or_400(filename: str, blob: bytes) -> catalog_service.ParsedSheet:
    try:
        parsed = catalog_service.parse_sheet(filename, blob)
    except SheetError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "UNREADABLE_SHEET", "message": str(exc)}
        ) from exc

    if parsed.missing:
        pretty = {
            "sku": "an item code", "name": "a product name",
            "price_paise": "a price",
        }
        names = ", ".join(pretty.get(m, m) for m in parsed.missing)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MISSING_COLUMNS",
                "message": f"Could not find {names} in that sheet. "
                           f"Columns found: {', '.join(parsed.mapping.values()) or 'none'}.",
                "missing": parsed.missing,
            },
        )
    return parsed


# ------------------------------------------------------------------ catalog --
@router.get("/catalog")
async def list_catalog(db: DbDep, _: MerchantKey,
                       merchant_id: str = DEMO_MERCHANT_ID,
                       include_inactive: bool = False) -> dict:
    items = await db.rpc("list_catalog",
                         {"p_merchant_id": merchant_id, "p_all": include_inactive})
    return {"items": items or []}


@router.get("/catalog/template.csv", response_class=PlainTextResponse)
async def catalog_template(_: MerchantKey) -> PlainTextResponse:
    return PlainTextResponse(
        catalog_service.template_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="kirana-catalog-template.csv"'},
    )


@router.post("/catalog/preview")
async def preview_catalog(
    request: Request, _: MerchantKey, file: UploadFile = File(...),
) -> dict:
    """Parse and validate. Writes nothing."""
    blob = await _read_upload(request, file)
    return _parse_or_400(file.filename or "", blob).public()


@router.post("/catalog/import")
async def import_catalog(
    request: Request, db: DbDep, _: MerchantKey,
    file: UploadFile = File(...),
    replace: bool = Form(default=False),
    merchant_id: str = Form(default=DEMO_MERCHANT_ID),
) -> dict:
    """Parse, validate, then write the valid rows.

    Invalid rows are skipped rather than failing the whole import: a sheet with
    one bad line should not stop a shop loading the other two hundred. The
    response says exactly what was skipped and why.
    """
    blob = await _read_upload(request, file)
    parsed = _parse_or_400(file.filename or "", blob)

    if not parsed.valid:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "NO_VALID_ROWS",
                "message": "Every row in that sheet was rejected. Nothing was imported.",
                "rows": [r.public() for r in parsed.rows],
            },
        )

    try:
        result = await db.rpc("upsert_catalog", {
            "p_merchant_id": merchant_id,
            "p_items": [r.to_item() for r in parsed.valid],
            "p_replace": replace,
        })
    except RpcError as exc:
        raise HTTPException(status_code=400,
                            detail={"code": exc.code, "message": exc.message}) from exc

    return {**parsed.public(), "imported": result}


@router.post("/catalog/items")
async def upsert_items(body: ItemsBody, db: DbDep, _: MerchantKey) -> dict:
    """Manual add/edit, bypassing the spreadsheet."""
    try:
        return await db.rpc("upsert_catalog", {
            "p_merchant_id": body.merchant_id,
            "p_items": body.items,
            "p_replace": body.replace,
        })
    except RpcError as exc:
        raise HTTPException(status_code=400,
                            detail={"code": exc.code, "message": exc.message}) from exc


# ------------------------------------------------------------------- shelves --
@router.get("/shelves")
async def list_shelves(db: DbDep, _: MerchantKey,
                       merchant_id: str = DEMO_MERCHANT_ID) -> dict:
    return {"shelves": await db.rpc("list_shelves", {"p_merchant_id": merchant_id}) or []}


@router.post("/shelves")
async def upsert_shelf(body: ShelfBody, db: DbDep, _: MerchantKey) -> dict:
    try:
        return await db.rpc("upsert_shelf", {
            "p_merchant_id": body.merchant_id,
            "p_name": body.name,
            "p_skus": [s.strip().upper() for s in body.skus if s.strip()],
            "p_note": body.note,
            "p_shelf_id": body.shelf_id,
        })
    except RpcError as exc:
        raise HTTPException(status_code=400,
                            detail={"code": exc.code, "message": exc.message}) from exc


@router.delete("/shelves/{shelf_id}")
async def delete_shelf(shelf_id: str, db: DbDep, _: MerchantKey,
                       merchant_id: str = DEMO_MERCHANT_ID) -> dict:
    try:
        return await db.rpc("delete_shelf",
                            {"p_merchant_id": merchant_id, "p_shelf_id": shelf_id})
    except RpcError as exc:
        raise HTTPException(
            status_code=404 if exc.code == "SHELF_NOT_FOUND" else 400,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


# ----------------------------------------------------------------- simulator --
@router.post("/simulate")
async def simulate_campaign(body: SimulateBody, db: DbDep, _: MerchantKey) -> dict:
    """What these numbers will do, before commit makes them permanent.

    Computed with the same pure functions the live gate uses, so the preview
    cannot drift away from the enforcement it is predicting.
    """
    catalog = await db.rpc("list_catalog", {"p_merchant_id": body.merchant_id,
                                            "p_all": False})
    if not catalog:
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_CATALOG",
                    "message": "Add some products before planning a campaign."},
        )
    return simulate.simulate(
        catalog,
        max_discount_bps=body.max_discount_bps,
        margin_floor_bps=body.margin_floor_bps,
        budget_paise=body.budget_paise,
        slot_count=body.slot_count,
    )
