"""The shipped catalogue workbook must still import.

docs/kirana-catalog.xlsx is a deliverable a shopkeeper uploads, and the thing
that would break it is invisible: someone edits PRODUCTS in
scripts/gen_catalog_sheet.py, sets a cost above a price or repeats a code, and
nobody finds out until the import screen refuses rows in front of them.

These run the real parser over the real file, so they fail the moment the
sheet and the importer disagree about anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.bounds import max_discount_for_margin
from app.services import catalog_service

SHEET = Path(__file__).resolve().parents[2] / "docs" / "kirana-catalog.xlsx"

#: The floor the live campaigns use, and the one the workbook's guide sheet
#: computes its headroom column against.
DEMO_FLOOR_BPS = 1_200

#: Codes the seeded shelves and any committed campaign reference by name. A
#: Replace import deactivates whatever is absent, so dropping one of these from
#: the sheet silently empties a shelf and kills a live sticker's scope.
ORIGINAL_SKUS = {"ATTA5", "RICE5", "DAL1K", "OIL1L", "SUGAR1", "TEA250"}


@pytest.fixture(scope="module")
def parsed() -> catalog_service.ParsedSheet:
    if not SHEET.exists():
        pytest.skip(f"{SHEET} not generated; run scripts/gen_catalog_sheet.py")
    return catalog_service.parse_sheet(SHEET.name, SHEET.read_bytes())


def test_every_row_is_accepted(parsed) -> None:
    assert not parsed.missing
    assert parsed.rejected == [], [
        (r.line, r.sku, r.errors) for r in parsed.rejected
    ]
    assert len(parsed.valid) == len(parsed.rows) > 50


def test_the_columns_map_to_what_they_say(parsed) -> None:
    """`category` is an extra column the importer does not want. The alias
    matcher works by substring, so an unexpected header CAN steal a field --
    this asserts that it does not, rather than assuming."""
    assert parsed.mapping == {
        "sku": "sku", "name": "name", "unit": "unit",
        "cost_paise": "cost", "price_paise": "price",
    }


def test_money_survives_as_exact_paise(parsed) -> None:
    """Decimal, never float. int(285.00 * 100) is fine; int(285.15 * 100) is
    28514, and that one paise reappears as an AMOUNT_MISMATCH at settlement."""
    by_sku = {r.sku: r for r in parsed.valid}
    assert by_sku["ATTA5"].price_paise == 28_500
    assert by_sku["RICE5"].price_paise == 62_000
    assert all(isinstance(r.price_paise, int) for r in parsed.valid)


def test_the_original_demo_codes_survive_a_replace(parsed) -> None:
    assert ORIGINAL_SKUS <= {r.sku for r in parsed.valid}


def test_no_row_would_sell_at_a_loss(parsed) -> None:
    for row in parsed.valid:
        assert 0 < row.cost_paise < row.price_paise, row.sku


def test_no_name_starts_with_a_formula_character(parsed) -> None:
    """Only a warning in the importer, but this file is ours, so it should be
    clean: a leading = or + is code execution the moment anyone adds a CSV
    export."""
    assert [r.sku for r in parsed.rows if r.warnings] == []


def test_most_of_the_shop_can_actually_be_haggled(parsed) -> None:
    """The failure this catalogue exists to avoid. With a 12% floor the old
    seed left sunflower oil and sugar un-discountable, and the assistant
    answering "I cannot go below cost on this one" reads as a broken shop
    rather than a working gate."""
    usable = [
        r for r in parsed.valid
        if max_discount_for_margin(r.price_paise, r.cost_paise, DEMO_FLOOR_BPS) > 0
    ]
    assert len(usable) / len(parsed.valid) > 0.9


def test_a_few_items_are_still_refused_outright(parsed) -> None:
    """And the other half of it: a catalogue where everything is discountable
    never demonstrates the gate saying no, which is the product."""
    refused = {
        r.sku for r in parsed.valid
        if max_discount_for_margin(r.price_paise, r.cost_paise, DEMO_FLOOR_BPS) < 0
    }
    assert refused, "nothing in the catalogue shows a refusal"
    assert refused <= {"SUGAR1", "SALT1"}, f"unexpected refusals: {refused}"


def test_it_fits_inside_the_upload_limits(parsed) -> None:
    assert len(parsed.rows) <= catalog_service.MAX_ROWS
    assert SHEET.stat().st_size < 4 * 1024 * 1024
