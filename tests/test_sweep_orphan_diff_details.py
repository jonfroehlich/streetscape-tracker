"""Tests for scripts/sweep_orphan_diff_details.py (issue #265): find published
diff detail files no catalog row points at, remove them only under --execute,
and never touch a file that is not diff-detail-shaped.

Every filename here comes from a generator, never by hand, because the sweep
keys on exactly the shapes the generators emit.
"""

import os
from datetime import date

import pytest

from scripts.sweep_orphan_diff_details import main, sweep
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.diff import generate_diff_filename
from streetscape_metadata_tracker.naming import (
    generate_run_filename,
    generate_streetwalk_diff_filename,
    generate_streetwalk_filename,
)

D0, D1 = date(2026, 4, 1), date(2026, 7, 1)


def _touch(data_dir, name):
    with open(os.path.join(data_dir, name), "w") as fh:
        fh.write("x")
    return name


def _register(conn, city_name):
    return db.register_city(
        conn,
        city_name=city_name,
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="us",
        center_lat=44.05,
        center_lon=-121.31,
        grid_width_m=1000,
        grid_height_m=1000,
        step_m=20,
    )


@pytest.fixture
def catalog(conn, data_dir):
    """A catalog with one referenced detail file per family, one orphan per
    family (non-default provider and network tokens, so the shapes the regex
    has to know are exercised), a row whose file is missing, and the
    non-diff neighbours the sweep must leave alone."""
    city_id = _register(conn, "Bend")
    run_ids = []
    for run_date in (D0, D1):
        csv = _touch(data_dir, generate_run_filename(city_id, 1000, 1000, 20, run_date) + ".csv.gz")
        run_json = _touch(data_dir, csv.replace(".csv.gz", ".json.gz"))
        run_ids.append(
            db.register_run(
                conn, city_id=city_id, run_date=run_date, csv_filename=csv, json_filename=run_json
            )
        )
    grid_ref = _touch(data_dir, generate_diff_filename(city_id, D0.isoformat(), D1.isoformat()))
    db.record_diff(
        conn,
        city_id=city_id,
        from_run_id=run_ids[0],
        to_run_id=run_ids[1],
        grid_aligned=True,
        panos_added=1,
        panos_removed=0,
        panos_persisted=0,
        capture_date_changed=0,
        points_gained_coverage=1,
        points_lost_coverage=0,
        coverage_delta_pct=1.0,
        detail_filename=grid_ref,
    )

    walk_ids = []
    for run_date in (D0, D1):
        walk_csv = generate_streetwalk_filename(city_id, 1000, 1000, 20, 15, run_date) + ".csv.gz"
        walk_ids.append(
            db.register_street_walk(
                conn,
                city_id=city_id,
                run_date=run_date,
                csv_filename=_touch(data_dir, walk_csv),
                provider="gsv",
                network_type="drive",
                spacing_m=15.0,
                match_dist_m=25.0,
            )
        )
    walk_ref = _touch(
        data_dir, generate_streetwalk_diff_filename(city_id, D0.isoformat(), D1.isoformat())
    )
    db.record_street_walk_diff(
        conn,
        city_id=city_id,
        from_walk_id=walk_ids[0],
        to_walk_id=walk_ids[1],
        edges_aligned=1,
        edges_added=0,
        edges_removed=0,
        edges_gained_coverage=1,
        edges_lost_coverage=0,
        coverage_fraction_changed=1,
        nearest_pano_date_changed=0,
        edges_fully_covered_delta=0,
        coverage_pct_by_length_delta=1.0,
        coverage_pct_by_length_any_delta=1.0,
        detail_filename=walk_ref,
    )

    # A second city whose row names a detail file that is not on disk.
    other = _register(conn, "Salem")
    other_runs = [
        db.register_run(
            conn,
            city_id=other,
            run_date=run_date,
            csv_filename=generate_run_filename(other, 1000, 1000, 20, run_date) + ".csv.gz",
        )
        for run_date in (D0, D1)
    ]
    missing = generate_diff_filename(other, D0.isoformat(), D1.isoformat())
    db.record_diff(
        conn,
        city_id=other,
        from_run_id=other_runs[0],
        to_run_id=other_runs[1],
        grid_aligned=True,
        panos_added=1,
        panos_removed=0,
        panos_persisted=0,
        capture_date_changed=0,
        points_gained_coverage=1,
        points_lost_coverage=0,
        coverage_delta_pct=1.0,
        detail_filename=missing,
    )

    orphans = [
        _touch(
            data_dir,
            generate_diff_filename(city_id, "2026-01-01", D0.isoformat(), provider="mapillary"),
        ),
        _touch(
            data_dir,
            generate_streetwalk_diff_filename(
                city_id,
                "2026-01-01",
                D0.isoformat(),
                provider="kartaview",
                network_type="all_public",
            ),
        ),
    ]
    survivors = [
        grid_ref,
        walk_ref,
        # A run CSV and its JSON for a city whose slug carries '_diff_' where
        # a diff's marker would sit — uncataloged, and still never a candidate.
        _touch(data_dir, generate_run_filename("my_diff_city", 1000, 1000, 20, D1) + ".csv.gz"),
        _touch(data_dir, generate_run_filename("my_diff_city", 1000, 1000, 20, D1) + ".json.gz"),
        _touch(data_dir, "cities.json.gz"),
    ]
    survivors += [
        name
        for name in os.listdir(data_dir)
        if name.endswith((".csv.gz", ".json.gz")) and name not in orphans
    ]
    return {"orphans": sorted(orphans), "survivors": sorted(set(survivors)), "missing": [missing]}


def _on_disk(data_dir, names):
    return [name for name in names if os.path.exists(os.path.join(data_dir, name))]


def test_dry_run_reports_the_orphans_and_deletes_nothing(conn, data_dir, catalog):
    report = sweep(conn, data_dir, execute=False)
    assert report.orphans == catalog["orphans"]
    assert report.removed == []
    assert _on_disk(data_dir, catalog["orphans"]) == catalog["orphans"]
    assert _on_disk(data_dir, catalog["survivors"]) == catalog["survivors"]


def test_execute_removes_exactly_the_orphans(conn, data_dir, catalog):
    report = sweep(conn, data_dir, execute=True)
    assert report.removed == catalog["orphans"]
    assert report.failed == []
    assert _on_disk(data_dir, catalog["orphans"]) == []
    assert _on_disk(data_dir, catalog["survivors"]) == catalog["survivors"]


def test_a_row_whose_file_is_missing_is_reported_never_fixed(conn, data_dir, catalog):
    before = conn.execute("SELECT * FROM run_diffs ORDER BY diff_id").fetchall()
    report = sweep(conn, data_dir, execute=True)
    assert report.missing == catalog["missing"]
    after = conn.execute("SELECT * FROM run_diffs ORDER BY diff_id").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_main_is_a_dry_run_by_default_and_executes_on_request(conn, data_dir, catalog):
    db_path = os.path.join(data_dir, "streetscape_tracker.db")
    assert main(["--data-dir", data_dir, "--db-path", db_path]) == 0
    assert _on_disk(data_dir, catalog["orphans"]) == catalog["orphans"]
    assert main(["--data-dir", data_dir, "--db-path", db_path, "--execute"]) == 0
    assert _on_disk(data_dir, catalog["orphans"]) == []
    assert _on_disk(data_dir, catalog["survivors"]) == catalog["survivors"]


def test_main_refuses_a_catalog_that_does_not_exist(tmp_path, data_dir, catalog):
    """A wrong --db-path would make every diff detail an orphan. db.connect
    would CREATE the file, so the refusal must come before it can."""
    absent = str(tmp_path / "nowhere.db")
    assert main(["--data-dir", data_dir, "--db-path", absent, "--execute"]) == 2
    assert not os.path.exists(absent)
    assert _on_disk(data_dir, catalog["orphans"]) == catalog["orphans"]


def test_main_refuses_a_catalog_with_no_runs(tmp_path, data_dir, catalog):
    empty = str(tmp_path / "empty.db")
    db.connect(empty).close()
    assert main(["--data-dir", data_dir, "--db-path", empty, "--execute"]) == 2
    assert _on_disk(data_dir, catalog["orphans"]) == catalog["orphans"]
