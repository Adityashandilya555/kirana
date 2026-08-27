"""The spreadsheet importer.

Every case here is a real shape a shopkeeper's price list takes. The money
parsing in particular is worth pinning: a one-paise rounding error at import
surfaces weeks later as an AMOUNT_MISMATCH at settlement, with nothing left to
point at.
"""

from __future__ import annotations

import pytest

from app.services import catalog_service as cat


def sheet(text: str) -> bytes:
    return text.encode("utf-8")


# ---------------------------------------------------------------- money -----
@pytest.mark.parametrize(
    ("raw", "paise"),
    [
        ("285", 28500),
        ("285.00", 28500),
        ("₹285.00", 28500),
        ("Rs 285", 28500),
        ("1,620.50", 162050),
        ("  145.5  ", 14550),
        (285, 28500),
        (285.0, 28500),
    ],
)
def test_money_parses_the_shapes_humans_write(raw: object, paise: int) -> None:
    assert cat.parse_money(raw) == paise


def test_money_keeps_the_last_paise() -> None:
    """int(float('285.15') * 100) is 28514. Decimal keeps it 28515."""
    assert cat.parse_money("285.15") == 28515
    assert cat.parse_money("0.07") == 7


@pytest.mark.parametrize("raw", [None, "", "   ", "-", "n/a", "TBD"])
def test_unreadable_money_is_none_not_zero(raw: object) -> None:
    # Zero would look like a free item and silently pass validation.
    assert cat.parse_money(raw) is None


# -------------------------------------------------------------- columns -----
def test_cost_price_is_not_stolen_by_the_price_matcher() -> None:
    """"Cost Price" contains "Price". Matching price first would consume the
    cost column and leave the margin unknowable."""
    parsed = cat.parse_sheet("p.csv", sheet(
        "Item Code,Particulars,Packing,MRP,Cost Price\n"
        "TEA250,Tata Tea Gold,pack,190,135\n"
    ))
    assert parsed.mapping["price_paise"] == "MRP"
    assert parsed.mapping["cost_paise"] == "Cost Price"
    assert parsed.rows[0].price_paise == 19000
    assert parsed.rows[0].cost_paise == 13500


@pytest.mark.parametrize(
    "header",
    [
        "sku,name,unit,price,cost",
        "Code,Product Name,Pack,Selling Price,Purchase Price",
        "item code,description,uom,rate,buy price",
    ],
)
def test_common_header_dialects_all_map(header: str) -> None:
    parsed = cat.parse_sheet("p.csv", sheet(f"{header}\nX1,Thing,pc,100,50\n"))
    assert parsed.missing == []
    assert parsed.rows[0].sku == "X1"


def test_missing_required_column_is_reported_not_guessed() -> None:
    parsed = cat.parse_sheet("p.csv", sheet("name,price\nThing,100\n"))
    assert "sku" in parsed.missing
    assert parsed.rows == []


# ----------------------------------------------------------- validation -----
def test_cost_at_or_above_price_is_rejected() -> None:
    parsed = cat.parse_sheet("p.csv", sheet(
        "sku,name,price,cost\nA,Loss leader,100.00,150.00\n"))
    assert not parsed.rows[0].ok
    assert any("loss" in e.lower() for e in parsed.rows[0].errors)


def test_missing_cost_is_an_error_not_a_zero_default() -> None:
    """Defaulting cost to zero makes everything look infinitely discountable."""
    parsed = cat.parse_sheet("p.csv", sheet("sku,name,price,cost\nA,Thing,99.00,\n"))
    assert not parsed.rows[0].ok
    assert any("cost" in e.lower() for e in parsed.rows[0].errors)


def test_duplicate_sku_is_rejected_after_the_first() -> None:
    parsed = cat.parse_sheet("p.csv", sheet(
        "sku,name,price,cost\nA,First,100,50\nA,Second,200,60\n"))
    assert parsed.rows[0].ok
    assert not parsed.rows[1].ok


def test_price_below_the_schema_floor_is_rejected() -> None:
    # The table itself enforces price_paise >= 100; catching it here gives the
    # shopkeeper a sentence instead of a constraint violation.
    parsed = cat.parse_sheet("p.csv", sheet("sku,name,price,cost\nA,Cheap,0.50,0.10\n"))
    assert not parsed.rows[0].ok


def test_sku_is_upper_cased_and_trimmed() -> None:
    parsed = cat.parse_sheet("p.csv", sheet("sku,name,price,cost\n  tea250 ,T,190,135\n"))
    assert parsed.rows[0].sku == "TEA250"


def test_margin_is_computed_on_the_sale_price() -> None:
    parsed = cat.parse_sheet("p.csv", sheet("sku,name,price,cost\nA,T,190.00,135.00\n"))
    # (19000-13500)/19000 = 28.94%
    assert parsed.rows[0].margin_bps == 2894


# ---------------------------------------------------------------- files -----
def test_utf8_bom_does_not_corrupt_the_first_header() -> None:
    """Excel's "CSV UTF-8" writes a BOM. Without utf-8-sig the sku column
    becomes '\\ufeffsku' and never maps."""
    blob = "﻿sku,name,price,cost\nA,Thing,100,50\n".encode("utf-8")
    parsed = cat.parse_sheet("p.csv", blob)
    assert parsed.missing == []
    assert parsed.rows[0].sku == "A"


def test_semicolon_delimited_csv_is_sniffed() -> None:
    parsed = cat.parse_sheet("p.csv", sheet("sku;name;price;cost\nA;Thing;100;50\n"))
    assert parsed.missing == []
    assert parsed.rows[0].price_paise == 10000


def test_legacy_xls_is_refused_with_an_actionable_message() -> None:
    with pytest.raises(cat.SheetError) as exc:
        cat.parse_sheet("old.xls", b"\xd0\xcf\x11\xe0")
    assert "xlsx" in str(exc.value)


def test_empty_file_is_refused() -> None:
    with pytest.raises(cat.SheetError):
        cat.parse_sheet("p.csv", sheet("\n\n"))


def test_template_round_trips_through_the_parser() -> None:
    """Whatever we hand out as an example must import cleanly."""
    parsed = cat.parse_sheet("t.csv", cat.template_csv().encode())
    assert parsed.missing == []
    assert len(parsed.valid) == 3
    assert parsed.rejected == []
