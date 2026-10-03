"""scripts/undated_imagery_share_analyze.py reads the GRID size (issue #289).

Its ``of_queried`` share once divided ``status_no_date`` by ``runs.total_points``,
a ROW count: for a census provider that is images plus empty points, not the
grid. The fixture separates the two numbers by 5x so the old denominator gives
a visibly different answer, and carries one census run with no
``total_grid_points`` (cataloged before v20) to pin that NULL is "not measured".
"""

from datetime import date

import pytest

from scripts.undated_imagery_share_analyze import measure_catalog, of_queried_kind
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.checkpointing import CENSUS_PROVIDERS


def _city(conn):
    return db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=1000,
        grid_height_m=1000,
        step_m=20,
    )


def _run(conn, cid, provider, day, ok, nd, rows, grid):
    db.register_run(
        conn,
        city_id=cid,
        run_date=date(2026, 5, day),
        csv_filename=f"{provider}-{day}.csv.gz",
        provider=provider,
        status_ok=ok,
        status_no_date=nd,
        total_points=rows,
        total_grid_points=grid,
    )


@pytest.fixture
def catalog(conn):
    cid = _city(conn)
    _run(conn, cid, "gsv", 1, ok=90, nd=10, rows=200, grid=200)
    # A census: 500 rows on a 100-point grid. The counts need not be
    # realizable together; what matters is that rows and grid differ by 5x.
    _run(conn, cid, "mapillary", 1, ok=300, nd=30, rows=500, grid=100)
    _run(conn, cid, "mapillary", 2, ok=10, nd=10, rows=40, grid=None)  # pre-v20
    return measure_catalog(conn)


def test_of_queried_divides_by_total_grid_points_not_total_points(catalog):
    m = catalog["mapillary"]
    # 30 / 100 grid points, NOT 30 / 500 rows (6.0), which is what the
    # row-count denominator produced.
    assert m["per_run_pct_of_queried"]["max"] == pytest.approx(30.0)
    assert m["points_queried"] == 100
    assert catalog["gsv"]["per_run_pct_of_queried"]["max"] == pytest.approx(5.0)


def test_a_run_without_total_grid_points_is_not_measured(catalog):
    m = catalog["mapillary"]
    assert m["runs_without_grid_points"] == 1
    assert m["per_run_pct_of_queried"]["n"] == 1
    # The pooled numerator is restricted to the backfilled runs: 30 / 100, not
    # (30 + 10) / 100.
    assert m["pooled_pct_of_queried"] == pytest.approx(30.0)
    assert catalog["gsv"]["runs_without_grid_points"] == 0


def test_of_present_reads_every_run_and_is_unchanged(catalog):
    # of_present never touched a grid-size column, so the NULL run counts here.
    m = catalog["mapillary"]
    assert m["per_run_pct_of_present"]["n"] == 2
    assert m["pooled_pct_of_present"] == pytest.approx(100.0 * 40 / 350, abs=1e-6)


def test_a_census_of_queried_is_labelled_an_upper_bound(catalog):
    assert catalog["gsv"]["of_queried_kind"] == "exact"
    assert catalog["mapillary"]["of_queried_kind"] == "upper_bound"
    assert {p for p in CENSUS_PROVIDERS if of_queried_kind(p) != "upper_bound"} == set()
