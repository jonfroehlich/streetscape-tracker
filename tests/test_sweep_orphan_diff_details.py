"""Tests for scripts/sweep_orphan_diff_details.py (issue #265): find published
diff detail files no catalog row points at, remove them only under --execute,
and never touch a file that is not diff-detail-shaped.

Every filename here comes from a generator, never by hand, because the sweep
keys on exactly the shapes the generators emit.
"""

import hashlib
import os
import sqlite3
import time
from datetime import date

import pytest

import scripts.sweep_orphan_diff_details as sweep_module
from scripts.sweep_orphan_diff_details import (
    USAGE_EXIT_CODE,
    main,
    open_catalog_readonly,
    sweep,
)
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.diff import generate_diff_filename
from streetscape_metadata_tracker.naming import (
    generate_run_filename,
    generate_streetwalk_diff_filename,
    generate_streetwalk_filename,
)

# The real writers, driven the way their own tests drive them, so the race
# tests below exercise production's write-then-record order and nothing else.
from tests.test_diff import _grid_diff, _two_run_series
from tests.test_walk_diff import NEW_FC, OLD_FC, _rediff, _register_city, _register_walk

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
    # Everything here predates the age window: these are files stranded before
    # #265, which is the population the sweep exists for.
    for name in os.listdir(data_dir):
        if name.endswith((".csv.gz", ".json.gz")):
            _age(os.path.join(data_dir, name))
    return {
        "orphans": sorted(orphans),
        "survivors": sorted(set(survivors)),
        "missing": [missing],
        "referenced": sorted([grid_ref, walk_ref]),
        "city_id": city_id,
    }


def _age(path, hours=48):
    """Backdate a file's mtime (never sleep): the age window reads mtime."""
    t = time.time() - hours * 3600
    os.utime(path, (t, t), follow_symlinks=False)


def _on_disk(data_dir, names):
    return [name for name in names if os.path.lexists(os.path.join(data_dir, name))]


def _db_path(data_dir):
    return os.path.join(data_dir, "streetscape_tracker.db")


def _sidecars(path):
    return [s for s in ("-wal", "-shm", "-journal") if os.path.exists(path + s)]


def _digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


@pytest.fixture(autouse=True)
def _no_batch_in_flight(monkeypatch):
    """The in-flight detector reads `ps`; a real run-due on the machine running
    the suite must not decide these tests."""
    monkeypatch.setattr(sweep_module, "_run_due_in_flight", lambda: None)


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
    assert main(["--data-dir", data_dir, "--db-path", _db_path(data_dir)]) == 0
    assert _on_disk(data_dir, catalog["orphans"]) == catalog["orphans"]
    assert main(["--data-dir", data_dir, "--db-path", _db_path(data_dir), "--execute"]) == 0
    assert _on_disk(data_dir, catalog["orphans"]) == []
    assert _on_disk(data_dir, catalog["survivors"]) == catalog["survivors"]


def test_main_refuses_a_catalog_that_does_not_exist(tmp_path, data_dir, catalog, caplog):
    """A wrong --db-path would make every diff detail an orphan, and must not
    be created by the attempt to open it."""
    absent = str(tmp_path / "nowhere.db")
    assert main(["--data-dir", data_dir, "--db-path", absent, "--execute"]) == USAGE_EXIT_CODE
    assert not os.path.exists(absent)
    assert "no catalog at" in caplog.text
    assert _on_disk(data_dir, catalog["orphans"]) == catalog["orphans"]


def test_main_refuses_a_catalog_with_no_runs(tmp_path, data_dir, catalog, caplog):
    """Refused up front, even on a dry run, and for that reason by name — the
    stale-catalog check would also stop --execute, but only later and saying
    something less useful."""
    empty = str(tmp_path / "empty.db")
    db.connect(empty).close()
    assert main(["--data-dir", data_dir, "--db-path", empty]) == USAGE_EXIT_CODE
    assert main(["--data-dir", data_dir, "--db-path", empty, "--execute"]) == USAGE_EXIT_CODE
    assert "catalogs no runs or walks" in caplog.text
    assert _on_disk(data_dir, catalog["orphans"]) == catalog["orphans"]


# ── A diff being written while the sweep runs (#402 review, BLOCKING) ──────
#
# Both collectors write the detail file BEFORE committing its row. These run a
# real sweep, on its own read-only connection, inside that window.


def _sweep_inside(monkeypatch, attr, data_dir, reports):
    """Wrap db.<attr> (the row commit that follows the file write) so a full
    --execute sweep runs between the write and the commit."""
    real = getattr(db, attr)

    def sweep_then_record(*args, **kwargs):
        sweeper = open_catalog_readonly(_db_path(data_dir))
        try:
            reports.append(sweep(sweeper, data_dir, execute=True))
        finally:
            sweeper.close()
        return real(*args, **kwargs)

    monkeypatch.setattr(db, attr, sweep_then_record)


def test_a_grid_diff_written_during_a_sweep_survives_it(conn, data_dir, monkeypatch):
    series = _two_run_series(conn, data_dir, "gsv", [("p1", "2020-05-01")], [("p2", "2024-05-01")])
    reports = []
    _sweep_inside(monkeypatch, "record_diff", data_dir, reports)
    change = _grid_diff(conn, data_dir, series, "gsv")
    assert len(reports) == 1
    assert reports[0].removed == []
    assert reports[0].too_recent == [change["diff_file"]]
    assert os.path.exists(os.path.join(data_dir, change["diff_file"]))


def test_a_walk_diff_written_during_a_sweep_survives_it(conn, data_dir, monkeypatch):
    city_id = _register_city(conn)
    _register_walk(conn, data_dir, city_id, D0, OLD_FC)
    walk_id, _ = _register_walk(conn, data_dir, city_id, D1, NEW_FC)
    reports = []
    _sweep_inside(monkeypatch, "record_street_walk_diff", data_dir, reports)
    change = _rediff(conn, data_dir, city_id, walk_id, D1, NEW_FC)
    assert len(reports) == 1
    assert reports[0].removed == []
    assert reports[0].too_recent == [change["diff_file"]]
    assert os.path.exists(os.path.join(data_dir, change["diff_file"]))


def test_the_disk_is_read_before_the_catalog(conn, data_dir, catalog, monkeypatch):
    """A diff written AND recorded between the two reads must count as
    referenced. With the age guard off, only the read order can save it."""
    late = generate_diff_filename(catalog["city_id"], D0.isoformat(), D1.isoformat(), "mapillary")
    real_scan = sweep_module.scan_diff_details

    def write_and_record_then_scan(data_dir_):
        _touch(data_dir, late)
        _record_grid_diff(conn, catalog["city_id"], late)
        return real_scan(data_dir_)

    monkeypatch.setattr(sweep_module, "scan_diff_details", write_and_record_then_scan)
    report = sweep(conn, data_dir, execute=False, min_age_hours=0)
    assert late not in report.orphans + report.too_recent


def test_each_orphan_is_rechecked_against_the_catalog_before_its_unlink(
    conn, data_dir, catalog, monkeypatch
):
    """A row that appears after the catalog was read saves its file: simulated
    by a stale read that sees no rows at all, while the rows are real."""
    monkeypatch.setattr(sweep_module, "referenced_detail_files", lambda conn_: set())
    report = sweep(conn, data_dir, execute=True)
    assert report.rechecked == catalog["referenced"]
    assert report.removed == catalog["orphans"]
    assert _on_disk(data_dir, catalog["referenced"]) == catalog["referenced"]


def test_a_recent_orphan_is_skipped_and_reported_and_an_old_one_removed(conn, data_dir, catalog):
    fresh = _touch(
        data_dir,
        generate_diff_filename(catalog["city_id"], "2026-02-01", D0.isoformat(), "kartaview"),
    )
    report = sweep(conn, data_dir, execute=True)
    assert report.too_recent == [fresh]
    assert report.removed == catalog["orphans"]
    assert _on_disk(data_dir, [fresh]) == [fresh]


def test_min_age_zero_is_loud_and_negative_is_a_usage_error(data_dir, catalog, caplog):
    caplog.set_level("INFO", logger="sweep_orphan_diff_details")
    fresh = _touch(
        data_dir,
        generate_diff_filename(catalog["city_id"], "2026-02-01", D0.isoformat(), "kartaview"),
    )
    argv = ["--data-dir", data_dir, "--db-path", _db_path(data_dir)]
    assert main([*argv, "--min-age-hours", "0"]) == 0
    assert "AGE GUARD DISABLED" in caplog.text
    assert f"orphan: {fresh}" in caplog.text
    with pytest.raises(SystemExit) as exc:
        main([*argv, "--min-age-hours", "-1"])
    assert exc.value.code == 2


def test_execute_is_refused_while_a_run_due_is_in_flight(data_dir, catalog, monkeypatch, caplog):
    monkeypatch.setattr(sweep_module, "_run_due_in_flight", lambda: "pid 1: scheduler run-due")
    argv = ["--data-dir", data_dir, "--db-path", _db_path(data_dir)]
    assert main([*argv, "--execute"]) == USAGE_EXIT_CODE
    assert _on_disk(data_dir, catalog["orphans"]) == catalog["orphans"]
    assert main(argv) == 0  # a dry run proceeds, and says why it is only that
    assert "in flight" in caplog.text


# ── The catalog is read, never written (#402 review) ───────────────────────


def test_dry_run_and_execute_leave_the_catalog_byte_identical(conn, data_dir, catalog):
    conn.close()  # the fixture's writer: its own sidecars would mask ours
    path = _db_path(data_dir)
    before = _digest(path)
    assert _sidecars(path) == []
    assert main(["--data-dir", data_dir, "--db-path", path]) == 0
    assert main(["--data-dir", data_dir, "--db-path", path, "--execute"]) == 0
    assert _digest(path) == before
    assert _sidecars(path) == []


def test_the_catalog_connection_refuses_writes(data_dir, catalog):
    sweeper = open_catalog_readonly(_db_path(data_dir))
    try:
        with pytest.raises(sqlite3.OperationalError):
            sweeper.execute("CREATE TABLE nope (x)")
    finally:
        sweeper.close()


def test_an_unrelated_sqlite_file_is_refused_and_left_untouched(tmp_path, data_dir, catalog):
    scratch = str(tmp_path / "some_other.db")
    other = sqlite3.connect(scratch)
    other.execute("CREATE TABLE t (x)")
    other.commit()
    other.close()
    before = _digest(scratch)
    assert main(["--data-dir", data_dir, "--db-path", scratch]) == USAGE_EXIT_CODE
    assert main(["--data-dir", data_dir, "--db-path", scratch, "--execute"]) == USAGE_EXIT_CODE
    assert _digest(scratch) == before
    assert _sidecars(scratch) == []
    assert _on_disk(data_dir, catalog["orphans"]) == catalog["orphans"]


def test_an_older_schema_catalog_is_refused_not_migrated(conn, data_dir, catalog):
    conn.close()
    path = _db_path(data_dir)
    old = sqlite3.connect(path)
    old.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
    old.commit()
    old.close()
    before = _digest(path)
    assert main(["--data-dir", data_dir, "--db-path", path]) == USAGE_EXIT_CODE
    assert _digest(path) == before
    assert _sidecars(path) == []


def test_a_catalog_older_than_the_disk_refuses_execute(conn, data_dir, catalog, caplog):
    """An unreferenced diff dated after the newest run or walk the catalog
    knows means a restored backup or a dev copy — against which every later
    diff looks orphaned. A dry run reports it; --execute deletes NOTHING."""
    caplog.set_level("INFO", logger="sweep_orphan_diff_details")
    later = _touch(
        data_dir, generate_diff_filename(catalog["city_id"], D1.isoformat(), "2026-10-01")
    )
    _age(os.path.join(data_dir, later))
    assert sweep(conn, data_dir, execute=False).newer_than_catalog == [later]
    argv = ["--data-dir", data_dir, "--db-path", _db_path(data_dir)]
    assert main([*argv, "--execute"]) == USAGE_EXIT_CODE
    assert f"dated after the catalog: {later}" in caplog.text
    assert _on_disk(data_dir, [*catalog["orphans"], later]) == [*catalog["orphans"], later]


# ── Only top-level regular files are candidates (#402 review) ──────────────


def test_symlinks_and_subdirectories_are_never_candidates(tmp_path, conn, data_dir, catalog):
    city_id = catalog["city_id"]
    name = generate_diff_filename(city_id, "2026-02-01", D0.isoformat(), "panoramax")
    target = tmp_path / "outside.csv.gz"
    target.write_text("x")
    link = os.path.join(data_dir, name)
    os.symlink(target, link)
    _age(link)
    subdir = os.path.join(data_dir, generate_diff_filename(city_id, "2026-03-01", D0.isoformat()))
    os.mkdir(subdir)
    nested_dir = os.path.join(data_dir, "nested")
    os.mkdir(nested_dir)
    nested = os.path.join(nested_dir, generate_diff_filename(city_id, "2026-02-15", D0.isoformat()))
    _touch(nested_dir, os.path.basename(nested))
    _age(nested)

    report = sweep(conn, data_dir, execute=True)
    assert report.removed == catalog["orphans"]
    assert os.path.lexists(link)
    assert target.exists()
    assert os.path.isdir(subdir)
    assert os.path.exists(nested)


def _record_grid_diff(conn, city_id, detail_filename):
    """A run_diffs row naming ``detail_filename``. The pair is the fixture's
    two runs REVERSED, because UNIQUE (from_run_id, to_run_id) already holds
    the forward pair; the sweep reads only the name."""
    run_ids = [
        r["run_id"]
        for r in conn.execute(
            "SELECT run_id FROM runs WHERE city_id = ? ORDER BY run_date", (city_id,)
        ).fetchall()
    ]
    conn.execute(
        "INSERT INTO run_diffs (city_id, from_run_id, to_run_id, grid_aligned, "
        "detail_filename, computed_at) VALUES (?, ?, ?, 1, ?, 't')",
        (city_id, run_ids[1], run_ids[0], detail_filename),
    )
    conn.commit()
