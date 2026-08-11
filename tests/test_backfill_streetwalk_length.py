"""Backfill of the street_walks absolute-length columns from artifacts (v12)."""

import gzip
import json
import os
from datetime import date

from scripts.backfill_streetwalk_length import backfill
from streetscape_metadata_tracker import db

# A consistent totals block: 84.0% is exactly 100 * 84.0 / 100.0, so the
# script's cross-check against the cataloged percentage passes.
TOTALS = {
    "length_km": 100.0,
    "length_km_covered": 84.0,
    "length_km_covered_any": 91.5,
    "median_covered_age_years": 3.25,
    "coverage_pct_by_length": 84.0,
}

_COLUMNS = ("length_km", "length_km_covered", "length_km_covered_any", "median_covered_age_years")


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
    conn,
    data_dir,
    city_id,
    run_date,
    *,
    totals=TOTALS,
    write_artifact=True,
    corrupt=False,
    cataloged_pct=84.0,
):
    """Catalog a pre-v12 walk (NULL length columns) with its artifact on disk."""
    stem = f"{city_id}_width_5000_height_5000_step_20_streetwalk_sp15_{run_date.isoformat()}"
    coverage_name = stem + "_coverage.json.gz"
    if corrupt:
        with open(os.path.join(data_dir, coverage_name), "wb") as fh:
            fh.write(b"not gzip at all")
    elif write_artifact:
        metadata = {"totals": dict(totals) if totals is not None else {}}
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
        coverage_pct_by_length=cataloged_pct,
    )


def _columns(conn, walk_id):
    row = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM street_walks WHERE walk_id = ?", (walk_id,)
    ).fetchone()
    return dict(zip(_COLUMNS, row, strict=True))


def test_dry_run_reports_but_writes_nothing(conn, data_dir):
    city_id = _register_city(conn)
    walk_id = _walk(conn, data_dir, city_id, date(2026, 5, 1))

    counts = backfill(conn, data_dir, execute=False)
    assert counts["updated"] == 1
    assert _columns(conn, walk_id) == dict.fromkeys(_COLUMNS)  # dry run touched nothing


def test_execute_populates_null_rows_from_artifacts(conn, data_dir):
    city_id = _register_city(conn)
    walk_id = _walk(conn, data_dir, city_id, date(2026, 5, 1))

    counts = backfill(conn, data_dir, execute=True)
    assert counts["updated"] == 1
    assert _columns(conn, walk_id) == {
        "length_km": 100.0,
        "length_km_covered": 84.0,
        "length_km_covered_any": 91.5,
        "median_covered_age_years": 3.25,
    }

    # Idempotent: a second run finds no candidates.
    assert backfill(conn, data_dir, execute=True)["updated"] == 0


def test_missing_artifact_or_totals_is_counted_and_skipped(conn, data_dir):
    city_id = _register_city(conn)
    gone = _walk(conn, data_dir, city_id, date(2026, 3, 1), write_artifact=False)
    keyless = _walk(conn, data_dir, city_id, date(2026, 5, 1), totals=None)

    counts = backfill(conn, data_dir, execute=True)
    assert counts["updated"] == 0
    assert counts["missing_artifact"] == 1
    assert counts["missing_key"] == 1
    assert counts["unreadable"] == 0
    assert _columns(conn, gone) == dict.fromkeys(_COLUMNS)
    assert _columns(conn, keyless) == dict.fromkeys(_COLUMNS)


def test_artifact_with_a_length_but_no_covered_length_is_skipped_not_crashed(conn, data_dir):
    """
    summarize_streetwalk_coverage has emitted length_km and length_km_covered
    together since the collector shipped, so one without the other is a
    malformed artifact. It must be counted and skipped — the cross-check needs
    both, and the progress log formats both, so a half-guarded key would raise
    mid-run instead of reporting a bad artifact.
    """
    city_id = _register_city(conn)
    totals = {k: v for k, v in TOTALS.items() if k != "length_km_covered"}
    walk_id = _walk(conn, data_dir, city_id, date(2026, 5, 1), totals=totals)

    counts = backfill(conn, data_dir, execute=True)
    assert counts["missing_key"] == 1
    assert counts["updated"] == 0
    assert _columns(conn, walk_id) == dict.fromkeys(_COLUMNS)


def test_unreadable_artifact_is_counted_separately(conn, data_dir):
    """A present-but-corrupt artifact (truncated gzip, bad JSON) is its own
    count — not conflated with a readable artifact that lacks the totals."""
    city_id = _register_city(conn)
    walk_id = _walk(conn, data_dir, city_id, date(2026, 5, 1), corrupt=True)

    counts = backfill(conn, data_dir, execute=True)
    assert counts["updated"] == 0
    assert counts["unreadable"] == 1
    assert counts["missing_key"] == 0
    assert _columns(conn, walk_id) == dict.fromkeys(_COLUMNS)


def test_artifact_disagreeing_with_the_cataloged_percentage_is_refused(conn, data_dir):
    """
    The lengths and the percentage are published side by side, so an artifact
    implying a different percentage than the row already carries means the walk
    was matched to the WRONG artifact — the one failure mode a filename-keyed
    backfill can actually have. Refuse the row rather than publish two numbers
    that contradict each other.
    """
    city_id = _register_city(conn)
    # Artifact says 84% covered; the catalog row says 30%.
    walk_id = _walk(conn, data_dir, city_id, date(2026, 5, 1), cataloged_pct=30.0)

    counts = backfill(conn, data_dir, execute=True)
    assert counts["mismatched"] == 1
    assert counts["updated"] == 0
    assert _columns(conn, walk_id) == dict.fromkeys(_COLUMNS)


def test_rounding_gap_between_lengths_and_percentage_is_tolerated(conn, data_dir):
    """
    Both sides are pre-rounded in the artifact (percentage to 1 dp, lengths to
    3), so recomputing cannot reproduce the percentage exactly. A tenth-of-a-
    point gap is arithmetic, not a mismatch, and must not block the backfill.
    """
    city_id = _register_city(conn)
    totals = dict(TOTALS, length_km=1172.091, length_km_covered=872.853)
    # 100 * 872.853 / 1172.091 = 74.47…, cataloged as the rounded 74.5.
    walk_id = _walk(conn, data_dir, city_id, date(2026, 5, 1), totals=totals, cataloged_pct=74.5)

    counts = backfill(conn, data_dir, execute=True)
    assert counts["mismatched"] == 0
    assert counts["updated"] == 1
    assert _columns(conn, walk_id)["length_km"] == 1172.091


def test_any_imagery_length_absent_from_older_artifacts_stays_null(conn, data_dir):
    """
    A pre-#116 artifact measured no flat imagery. The column stays NULL ("not
    measured") rather than falling back to the 360° length — the same
    convention coverage_pct_by_length_any follows. The row must NOT become a
    perpetual candidate: `length_km` alone gates candidacy, so it resolves.
    """
    city_id = _register_city(conn)
    totals = {k: v for k, v in TOTALS.items() if k != "length_km_covered_any"}
    walk_id = _walk(conn, data_dir, city_id, date(2026, 5, 1), totals=totals)

    assert backfill(conn, data_dir, execute=True)["updated"] == 1
    cols = _columns(conn, walk_id)
    assert cols["length_km_covered_any"] is None
    assert cols["length_km"] == 100.0
    # Resolved, not a candidate forever.
    assert backfill(conn, data_dir, execute=True)["updated"] == 0


def test_zero_coverage_walk_backfills_with_a_null_age(conn, data_dir):
    """
    A walk that found nothing has a real length and a real 0.0% — but no
    covered imagery to take a median age of. All four columns must still be
    written (0.0 is a measurement; NULL age is the absence of one).
    """
    city_id = _register_city(conn)
    totals = {
        "length_km": 6.961,
        "length_km_covered": 0.0,
        "length_km_covered_any": 0.0,
        "median_covered_age_years": None,
    }
    walk_id = _walk(conn, data_dir, city_id, date(2026, 5, 1), totals=totals, cataloged_pct=0.0)

    assert backfill(conn, data_dir, execute=True)["updated"] == 1
    assert _columns(conn, walk_id) == {
        "length_km": 6.961,
        "length_km_covered": 0.0,
        "length_km_covered_any": 0.0,
        "median_covered_age_years": None,
    }


def test_populated_rows_are_untouched(conn, data_dir):
    """A row that already carries a length is not a candidate — the artifact is
    never re-read over it."""
    city_id = _register_city(conn)
    stem = f"{city_id}_width_5000_height_5000_step_20_streetwalk_sp15_2026-05-01"
    db.register_street_walk(
        conn,
        city_id=city_id,
        run_date=date(2026, 5, 1),
        csv_filename=stem + ".csv.gz",
        coverage_filename=stem + "_coverage.json.gz",
        length_km=12.5,
        length_km_covered=6.25,
    )

    counts = backfill(conn, data_dir, execute=True)
    assert counts == {
        "updated": 0,
        "missing_artifact": 0,
        "unreadable": 0,
        "missing_key": 0,
        "skipped_no_filename": 0,
        "mismatched": 0,
    }
    row = db.get_latest_street_walk(conn, city_id)
    assert row["length_km"] == 12.5
