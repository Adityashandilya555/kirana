"""Python and Postgres must compute the same margin, byte for byte.

reserve_slot now enforces the margin floor in SQL, which it never could before
per-product caps gave it a stored number to work from. That makes
`bounds.margin_bps_after` and `public.margin_bps_after` twins, and twins drift.

The specific trap, and the reason this file exists rather than a comment:

    Python `//` FLOORS. Postgres integer `/` TRUNCATES TOWARD ZERO.

They agree on positives, so the sale-price line is safe either way. They do NOT
agree when cost exceeds the sale price and the division is inexact:

    Python:    -5000001 // 1000  ==  -5001
    Postgres:  -5000001 /  1000  ==  -5000

A below-cost margin is below any floor >= 0 whichever way it rounds, so the
DECISION would usually survive -- which is exactly what makes this the kind of
bug that ships. The SQL uses floor() over numeric specifically to match, and
the digest below pins that it does.

The digest was produced by running the identical grid through Postgres:

    select md5(string_agg(price||':'||cost||':'||bps||'='||
                          margin_bps_after(price,cost,bps),
                          '|' order by price, cost, bps))
      from (... the same cross join ...) g;

Regenerate it the same way if the grid changes. A CI run needs no database:
the digest carries Postgres's answer across.
"""

from __future__ import annotations

import hashlib

from app.core.bounds import margin_bps_after

# Real shop values plus the edges that break naive arithmetic: a 1-paise price
# where the sale rounds to zero, costs far above price, and the two discount
# extremes.
PRICES = [100, 4800, 19000, 28500, 62000, 999, 1]
COSTS = [0, 50, 4600, 12000, 24500, 61999, 120000]
DISCOUNTS = [0, 1, 231, 500, 1200, 1925, 5000, 9999, 10000]

#: Computed by Postgres. See the module docstring for the query.
POSTGRES_ROWS = 441
POSTGRES_SUM = -6_162_617_390
POSTGRES_DIGEST = "258cd7329f2bc0ee0d2b572745bd7afb"


def _grid() -> list[tuple[int, int, int, int]]:
    """Ordered exactly as the SQL `order by price, cost, bps`."""
    return [
        (price, cost, bps, margin_bps_after(price, cost, bps))
        for price in sorted(PRICES)
        for cost in sorted(COSTS)
        for bps in sorted(DISCOUNTS)
    ]


def test_the_grid_is_the_same_size_postgres_walked() -> None:
    assert len(_grid()) == POSTGRES_ROWS


def test_python_and_postgres_agree_on_every_margin() -> None:
    """One digest over the whole grid.

    A single mismatched row changes the hash, and the sum below narrows down
    whether the difference is one big row or many small ones.
    """
    joined = "|".join(f"{p}:{c}:{b}={m}" for p, c, b, m in _grid())
    digest = hashlib.md5(joined.encode()).hexdigest()  # noqa: S324 - not a credential
    assert digest == POSTGRES_DIGEST, (
        "Python and Postgres margin arithmetic have drifted. "
        "Rerun the query in this module's docstring and compare row by row."
    )


def test_the_totals_agree_too() -> None:
    # Cheap triangulation: if the digest fails, this says whether the two are
    # off by one row or by many.
    assert sum(m for *_, m in _grid()) == POSTGRES_SUM


# ------------------------------------------------------- the rounding trap --
def test_a_below_cost_margin_floors_rather_than_truncating() -> None:
    """The case the two languages disagree on unless SQL is written carefully.

    Selling at 100 with a cost of 4600 is catastrophically below cost, and the
    exact figure only matters because a twin that is only usually right is not
    a twin.
    """
    assert margin_bps_after(100, 4600, 0) == -450_000


def test_a_sale_price_rounding_to_zero_is_refused_not_divided_by() -> None:
    # 1 paise at 1 bp off floors to a sale of 0. Dividing by it would be a
    # crash in Python and a NULL in SQL; both return the sentinel instead.
    assert margin_bps_after(1, 0, 1) == -10_000
    assert margin_bps_after(100, 0, 10_000) == -10_000


def test_full_price_on_a_free_item_is_full_margin() -> None:
    assert margin_bps_after(100, 0, 0) == 10_000
