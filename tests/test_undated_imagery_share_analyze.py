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
    # gsv is exact here only because its one run has as many rows as points.
    assert catalog["gsv"]["of_queried_kind"] == "exact"
    assert catalog["mapillary"]["of_queried_kind"] == "upper_bound"
    assert {p for p in CENSUS_PROVIDERS if of_queried_kind(p) != "upper_bound"} == set()


def test_a_gsv_run_with_more_rows_than_points_makes_gsv_an_upper_bound(conn):
    """PR #422 review: "one gsv row is one grid point" is a property of the CSV,
    not of the provider. A legacy resumed gsv run can hold several rows for one
    point, where a duplicated NO_DATE row counts twice, so a gsv block with any
    such run is labelled ``upper_bound``. Killed by deciding the kind from the
    provider alone."""
    cid = _city(conn)
    _run(conn, cid, "gsv", 1, ok=90, nd=10, rows=200, grid=200)
    _run(conn, cid, "gsv", 2, ok=90, nd=10, rows=290, grid=200)  # resumed: 90 repeated rows
    m = measure_catalog(conn)["gsv"]
    assert m["runs_with_more_rows_than_grid_points"] == 1
    assert m["of_queried_kind"] == "upper_bound"


def test_the_run_behind_each_maximum_is_named(conn):
    """PR #422 review: a writeup that names the worst run must trace the name
    to the metrics file. The maximum of each per-run distribution carries its
    run, and the of_present and of_queried maxima can be different runs."""
    cid = _city(conn)
    _run(
        conn, cid, "mapillary", 1, ok=10, nd=10, rows=40, grid=400
    )  # 50% of present, 2.5% of queried
    _run(
        conn, cid, "mapillary", 2, ok=270, nd=30, rows=500, grid=100
    )  # 10% of present, 30% of queried
    m = measure_catalog(conn)["mapillary"]
    assert m["max_run_pct_of_present"] == {
        "city_id": cid,
        "run_date": "2026-05-01",
        "pct": 50.0,
    }
    assert m["max_run_pct_of_queried"] == {
        "city_id": cid,
        "run_date": "2026-05-02",
        "pct": 30.0,
    }
    assert m["per_run_pct_of_present"]["max"] == m["max_run_pct_of_present"]["pct"]
    assert m["per_run_pct_of_queried"]["max"] == m["max_run_pct_of_queried"]["pct"]
