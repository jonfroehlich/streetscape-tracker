"""Backfill of street_walks.coverage_by_highway from artifacts (issue #101)."""

import gzip
import json
import os
from datetime import date

from scripts.backfill_streetwalk_coverage import backfill
from streetscape_metadata_tracker import db

BREAKDOWN = {"residential": {"edges": 80, "coverage_pct_by_length": 84.0}}


def _register_city(conn, name="Bend"):
    return db.register_city(
        conn,
        city_name=name,
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.31,
        grid_width_m=5000,
        grid_height_m=5000,
        step_m=20,
    )


def _walk(
    conn, data_dir, city_id, run_date, *, breakdown=BREAKDOWN, write_artifact=True, corrupt=False
):
    """Catalog a pre-v11 walk (NULL breakdown column) with its artifact on disk."""
    stem = f"{city_id}_width_5000_height_5000_step_20_streetwalk_sp15_{run_date.isoformat()}"
    coverage_name = stem + "_coverage.json.gz"
    if corrupt:
        with open(os.path.join(data_dir, coverage_name), "wb") as fh:
            fh.write(b"not gzip at all")
    elif write_artifact:
        metadata = {"totals": {"coverage_pct_by_length": 80.0}}
        if breakdown is not None:
            metadata["coverage_by_highway"] = breakdown
        with gzip.open(os.path.join(data_dir, coverage_name), "wt", encoding="utf-8") as fh:
            json.dump(
                {"type": "FeatureCollection", "features": [], "properties": {"metadata": metadata}},
                fh,
            )
    return db.register_street_walk(
        conn,
        city_id=city_id,
        run_date=run_date,
        csv_filename=stem + ".csv.gz",
        coverage_filename=coverage_name,
    )


def _column(conn, walk_id):
    return conn.execute(
        "SELECT coverage_by_highway FROM street_walks WHERE walk_id = ?", (walk_id,)
    ).fetchone()[0]


def test_dry_run_reports_but_writes_nothing(conn, data_dir):
    city_id = _register_city(conn)
    walk_id = _walk(conn, data_dir, city_id, date(2026, 5, 1))

    counts = backfill(conn, data_dir, execute=False)
    assert counts["updated"] == 1
    assert _column(conn, walk_id) is None  # dry run touched nothing


def test_execute_populates_null_rows_from_artifacts(conn, data_dir):
    city_id = _register_city(conn)
    walk_id = _walk(conn, data_dir, city_id, date(2026, 5, 1))

    counts = backfill(conn, data_dir, execute=True)
    assert counts["updated"] == 1
    assert json.loads(_column(conn, walk_id)) == BREAKDOWN

    # Idempotent: a second run finds no candidates.
    assert backfill(conn, data_dir, execute=True)["updated"] == 0


def test_missing_artifact_or_key_is_counted_and_skipped(conn, data_dir):
    city_id = _register_city(conn)
    gone = _walk(conn, data_dir, city_id, date(2026, 3, 1), write_artifact=False)
    keyless = _walk(conn, data_dir, city_id, date(2026, 5, 1), breakdown=None)

    counts = backfill(conn, data_dir, execute=True)
    assert counts["updated"] == 0
    assert counts["missing_artifact"] == 1
    assert counts["missing_key"] == 1
    assert counts["unreadable"] == 0
    assert _column(conn, gone) is None
    assert _column(conn, keyless) is None


def test_unreadable_artifact_is_counted_separately(conn, data_dir):
    """A present-but-corrupt artifact (truncated gzip, bad JSON) is its own
    count — not conflated with a readable artifact that lacks the key."""
    city_id = _register_city(conn)
    walk_id = _walk(conn, data_dir, city_id, date(2026, 5, 1), corrupt=True)

    counts = backfill(conn, data_dir, execute=True)
    assert counts["updated"] == 0
    assert counts["unreadable"] == 1
    assert counts["missing_key"] == 0
    assert _column(conn, walk_id) is None


def test_populated_rows_are_untouched(conn, data_dir):
    """A row that already carries a breakdown is not a candidate — the
    artifact is never re-read over it."""
    city_id = _register_city(conn)
    stem = f"{city_id}_width_5000_height_5000_step_20_streetwalk_sp15_2026-05-01"
    existing = json.dumps({"trunk": {"edges": 3}})
    db.register_street_walk(
        conn,
        city_id=city_id,
        run_date=date(2026, 5, 1),
        csv_filename=stem + ".csv.gz",
        coverage_filename=stem + "_coverage.json.gz",
        coverage_by_highway=existing,
    )

    counts = backfill(conn, data_dir, execute=True)
    assert counts == {
        "updated": 0,
        "missing_artifact": 0,
        "unreadable": 0,
        "missing_key": 0,
        "skipped_no_filename": 0,
    }
    row = db.get_latest_street_walk(conn, city_id)
    assert row["coverage_by_highway"] == existing
