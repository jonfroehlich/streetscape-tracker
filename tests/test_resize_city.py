"""Tests for scripts/resize_city.py: the manual escape hatch that overwrites a
city's otherwise-frozen grid geometry.

The behavior under test is the diff-continuity guard. Geometry is immutable so
run-to-run diffs align on an identical rectangle; resizing a city that already
has real (non-baseline) runs silently orphans those runs onto a differently
shaped grid. The script must refuse that without --force, and must never touch
the catalog unless --execute is given.
"""

import os
from datetime import date

import pytest

from scripts.resize_city import main
from streetscape_metadata_tracker import db
from tests.conftest import make_city_df, write_city_csv_gz

ORIGINAL = dict(center_lat=44.05, center_lon=-121.31, grid_width_m=1000, grid_height_m=1000)


@pytest.fixture
def db_path(data_dir):
    return os.path.join(data_dir, "streetscape_tracker.db")


@pytest.fixture
def city_id(conn):
    """A registered city with a small frozen grid and no runs yet."""
    return db.register_city(
        conn,
        city_name="Browning",
        state_name="Montana",
        state_code="MT",
        country_name="United States",
        country_code="US",
        step_m=20,
        **ORIGINAL,
    )


def add_run(conn, data_dir, city_id, run_date, *, is_baseline=False, provider="gsv"):
    """Register a run (with its snapshot on disk) for the guard to see."""
    token = "" if provider == "gsv" else f"_{provider}"
    csv_name = f"{city_id}_width_1000_height_1000_step_20{token}_{run_date}.csv.gz"
    write_city_csv_gz(make_city_df([("p1", "2020-05-01")]), os.path.join(data_dir, csv_name))
    return db.register_run(
        conn,
        city_id=city_id,
        run_date=run_date,
        provider=provider,
        csv_filename=csv_name,
        is_baseline=is_baseline,
        total_points=2,
    )


def run_cli(monkeypatch, db_path, *args):
    """Invoke the script's main() with argv, returning its exit code."""
    monkeypatch.setattr(
        "sys.argv", ["resize_city.py", *[str(a) for a in args], "--db-path", db_path]
    )
    return main()


def geometry(conn, city_id):
    row = db.resolve_city(conn, city_id)
    return (row.center_lat, row.center_lon, row.grid_width_m, row.grid_height_m)


def test_refuses_city_with_real_runs_and_leaves_geometry_untouched(
    monkeypatch, conn, data_dir, db_path, city_id
):
    add_run(conn, data_dir, city_id, date(2026, 7, 1))
    before = geometry(conn, city_id)

    code = run_cli(monkeypatch, db_path, city_id, "--width", 2500, "--height", 2500, "--execute")

    assert code == 2
    assert geometry(conn, city_id) == before


def test_force_overrides_the_guard(monkeypatch, conn, data_dir, db_path, city_id):
    add_run(conn, data_dir, city_id, date(2026, 7, 1))

    code = run_cli(
        monkeypatch,
        db_path,
        city_id,
        "--width",
        2500,
        "--height",
        2500,
        "--force",
        "--execute",
    )

    assert code == 0
    assert geometry(conn, city_id) == (44.05, -121.31, 2500, 2500)


def test_baseline_only_city_resizes_without_force(monkeypatch, conn, data_dir, db_path, city_id):
    """Legacy undated runs have no diffs to lose, so resizing them is free."""
    add_run(conn, data_dir, city_id, date(2026, 1, 1), is_baseline=True)

    code = run_cli(monkeypatch, db_path, city_id, "--width", 2500, "--height", 2500, "--execute")

    assert code == 0
    assert geometry(conn, city_id)[2:] == (2500, 2500)


def test_guard_sees_runs_from_any_provider(monkeypatch, conn, data_dir, db_path, city_id):
    """Geometry is shared by all providers, so a Mapillary run blocks too."""
    add_run(conn, data_dir, city_id, date(2026, 7, 1), provider="mapillary")

    code = run_cli(monkeypatch, db_path, city_id, "--width", 2500, "--height", 2500, "--execute")

    assert code == 2
    assert geometry(conn, city_id)[2:] == (1000, 1000)


def test_dry_run_is_the_default_and_writes_nothing(monkeypatch, conn, db_path, city_id, capsys):
    code = run_cli(monkeypatch, db_path, city_id, "--width", 2500, "--height", 2500)

    assert code == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert geometry(conn, city_id)[2:] == (1000, 1000)


def test_explicit_recenter_moves_the_center(monkeypatch, conn, db_path, city_id):
    code = run_cli(
        monkeypatch,
        db_path,
        city_id,
        "--width",
        2500,
        "--height",
        2500,
        "--center-lat",
        48.56,
        "--center-lon",
        -113.014,
        "--execute",
    )

    assert code == 0
    assert geometry(conn, city_id) == (48.56, -113.014, 2500, 2500)


def test_unknown_city_exits_nonzero(monkeypatch, db_path, city_id):
    code = run_cli(
        monkeypatch, db_path, "Nowhere, ZZ", "--width", 2500, "--height", 2500, "--execute"
    )

    assert code == 1


def test_resize_leaves_an_audit_note(monkeypatch, conn, db_path, city_id):
    run_cli(monkeypatch, db_path, city_id, "--width", 2500, "--height", 2500, "--execute")

    notes = conn.execute("SELECT notes FROM cities WHERE city_id = ?", (city_id,)).fetchone()[
        "notes"
    ]
    assert "resize_city.py" in notes
