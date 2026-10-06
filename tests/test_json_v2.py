"""JSON schema v2 tests: shape, NaN-free output, run_date-pinned ages."""

import gzip
import json
import os
import sys
from datetime import date

import pandas as pd
import pytest

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.fileutils import load_city_csv_file, load_history_csv_file
from streetscape_metadata_tracker.json_summarizer import (
    generate_aggregate_v2,
    generate_city_metadata_summary_as_json,
    generate_history_summary_as_json,
    sanitize_for_json,
)
from streetscape_metadata_tracker.naming import generate_history_filename
from tests.conftest import (
    COLUMNS,
    make_city_df,
    make_history_df,
    make_mapillary_city_df,
    write_city_csv_gz,
)


def strict_load(path):
    """json.load that raises on NaN/Infinity literals."""

    def _reject(token):
        raise ValueError(f"invalid token {token}")

    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f, parse_constant=_reject)


def _write_run(data_dir, panos, run_date, name):
    csv_path = os.path.join(data_dir, name)
    write_city_csv_gz(make_city_df(panos, run_date=run_date), csv_path)
    return csv_path


def test_sanitize_for_json():
    dirty = {"a": float("nan"), "b": [float("inf"), 1.5], "c": {"d": float("-inf")}}
    clean = sanitize_for_json(dirty)
    assert clean == {"a": None, "b": [None, 1.5], "c": {"d": None}}
    json.dumps(clean, allow_nan=False)  # must not raise


def test_single_pano_city_emits_valid_json(data_dir):
    # Regression: 1 unique pano -> stdev NaN -> literal NaN in the JSON
    csv_path = _write_run(
        data_dir,
        [("p1", "2020-05-01")],
        date(2026, 1, 15),
        "solo--city_width_100_height_100_step_20_2026-01-15.csv.gz",
    )
    df = load_city_csv_file(csv_path)
    json_path = generate_city_metadata_summary_as_json(
        csv_path,
        df,
        "Solo",
        None,
        "Testland",
        100,
        100,
        20,
        force_recreate_file=True,
        run_date=date(2026, 1, 15),
    )
    data = strict_load(json_path)  # raises if NaN leaked
    assert data["all_panos"]["age_stats"]["stdev_pano_age_years"] is None


def test_no_date_pano_counted_in_json(data_dir):
    # End-to-end: a dateless (NO_DATE) pano must appear in the published pano
    # total and coverage, but not perturb the age stats (schema v3).
    ts = "2026-01-15T12:00:00+00:00"
    df_raw = pd.DataFrame(
        [
            (44.000, -121.0, ts, 44.0001, -121.0001, "ok1", "2020-01-15", "© Google", "OK"),
            # Within the 50 m query radius (issue #367): a pano 88 m east
            # would now read as OUT_OF_RADIUS, which is not what this pins.
            (44.001, -121.0, ts, 44.0011, -121.0001, "nd1", None, "© Google", "NO_DATE"),
            (44.002, -121.0, ts, None, None, None, None, None, "ZERO_RESULTS"),
        ],
        columns=COLUMNS,
    )
    csv_path = os.path.join(data_dir, "nd--city_width_100_height_100_step_20_2026-01-15.csv.gz")
    write_city_csv_gz(df_raw, csv_path)
    df = load_city_csv_file(csv_path)
    json_path = generate_city_metadata_summary_as_json(
        csv_path,
        df,
        "ND",
        None,
        "Testland",
        100,
        100,
        20,
        force_recreate_file=True,
        run_date=date(2026, 1, 15),
    )
    data = strict_load(json_path)

    # Both panos counted; Google subset counts the dateless © Google pano too
    assert data["all_panos"]["duplicate_stats"]["total_unique_panos"] == 2
    assert data["google_panos"]["duplicate_stats"]["total_unique_panos"] == 2
    # Coverage: 2 of 3 grid points hold imagery
    assert data["coverage"]["coverage_rate"] == pytest.approx(100 * 2 / 3)
    # Age stats derive from the single dated pano (captured exactly 6y before)
    assert data["all_panos"]["age_stats"]["avg_pano_age_years"] == pytest.approx(6.0, abs=0.01)


def test_v2_fields_and_age_pinned_to_run_date(data_dir):
    run_date = date(2026, 1, 15)
    csv_path = _write_run(
        data_dir,
        [("p1", "2020-01-15"), ("p2", "2022-01-15")],
        run_date,
        "duo--city_width_100_height_100_step_20_2026-01-15.csv.gz",
    )
    df = load_city_csv_file(csv_path)
    change = {
        "from_run_date": "2025-10-01",
        "panos_added": 1,
        "panos_removed": 0,
        "capture_date_changed": 0,
        "coverage_delta_pct": 0.0,
        "grid_aligned": True,
        "diff_file": None,
    }
    json_path = generate_city_metadata_summary_as_json(
        csv_path,
        df,
        "Duo",
        None,
        "Testland",
        100,
        100,
        20,
        force_recreate_file=True,
        run_date=run_date,
        change_from_previous_run=change,
    )
    data = strict_load(json_path)

    assert data["schema_version"] == 2
    assert data["provider"] == "gsv"
    assert data["run"] == {"run_date": "2026-01-15", "is_baseline": False}
    assert data["change_from_previous_run"]["panos_added"] == 1
    assert "google_panos" in data
    assert data["copyright_info_available"] is True

    # Ages relative to run_date: panos captured exactly 6 and 4 years earlier
    ages = data["all_panos"]["age_stats"]
    assert ages["avg_pano_age_years"] == pytest.approx(5.0, abs=0.01)
    assert ages["median_pano_age_years"] == pytest.approx(5.0, abs=0.01)


def test_copyright_unknown_run_json(data_dir):
    # Archival imports (issue #93) never captured copyright_info: the
    # Google subset is unknown, so google_panos is omitted and flagged
    run_date = date(2023, 11, 5)
    csv_path = os.path.join(data_dir, "old--city_width_1000_height_1000_step_30_2023-11-05.csv.gz")
    write_city_csv_gz(
        make_city_df(
            [("p1", "2020-05-01"), ("p2", "2021-06-01")], run_date=run_date, copyright_info=None
        ),
        csv_path,
    )
    df = load_city_csv_file(csv_path)
    json_path = generate_city_metadata_summary_as_json(
        csv_path,
        df,
        "Old",
        None,
        "Testland",
        1000,
        1000,
        30,
        force_recreate_file=True,
        run_date=run_date,
        is_baseline=True,
    )
    data = strict_load(json_path)

    assert data["copyright_info_available"] is False
    assert "google_panos" not in data
    assert data["run"] == {"run_date": "2023-11-05", "is_baseline": True}
    assert data["all_panos"]["duplicate_stats"]["total_unique_panos"] == 2


def test_run_stats_google_panos_none_when_copyright_unknown():
    from streetscape_metadata_tracker.analysis import calculate_run_stats

    run_date = date(2023, 11, 5)
    df_unknown = make_city_df([("p1", "2020-05-01")], run_date=run_date, copyright_info=None)
    stats = calculate_run_stats(df_unknown, run_date)
    assert stats["unique_google_panos"] is None
    assert stats["unique_panos"] == 1

    df_known = make_city_df([("p1", "2020-05-01")], run_date=run_date)
    stats = calculate_run_stats(df_known, run_date)
    assert stats["unique_google_panos"] == 1

    # A run with zero OK rows has a trivially known (zero) Google subset
    df_empty = make_city_df([], run_date=run_date, n_empty=2)
    stats = calculate_run_stats(df_empty, run_date)
    assert stats["unique_google_panos"] == 0


def test_aggregate_propagates_copyright_flag(conn, data_dir):
    city_id = db.register_city(
        conn,
        city_name="Old",
        state_name=None,
        state_code=None,
        country_name="Testland",
        country_code=None,
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=1000,
        grid_height_m=1000,
        step_m=20,
    )
    run_date = date(2023, 11, 5)
    csv_name = f"{city_id}_width_1000_height_1000_step_30_2023-11-05.csv.gz"
    csv_path = os.path.join(data_dir, csv_name)
    write_city_csv_gz(
        make_city_df([("p1", "2020-05-01")], run_date=run_date, copyright_info=None), csv_path
    )
    df = load_city_csv_file(csv_path)
    json_path = generate_city_metadata_summary_as_json(
        csv_path,
        df,
        "Old",
        None,
        "Testland",
        1000,
        1000,
        30,
        force_recreate_file=True,
        run_date=run_date,
        is_baseline=True,
    )
    db.register_run(
        conn,
        city_id=city_id,
        run_date=run_date,
        csv_filename=csv_name,
        json_filename=os.path.basename(json_path),
        is_baseline=True,
        unique_panos=1,
        unique_google_panos=None,
    )

    summary = generate_aggregate_v2(conn, data_dir)
    gsv = summary["cities"][0]["providers"]["gsv"]

    assert gsv["latest"]["copyright_info_available"] is False
    assert "unique_google_panos" not in gsv["latest"]["panorama_counts"]
    assert "google_panos_age_stats" not in gsv["latest"]
    assert gsv["latest"]["is_baseline"] is True
    assert gsv["runs"][0]["unique_google_panos"] is None
    # No google contribution to the global gsv histograms
    assert summary["histogram_of_capture_dates"]["gsv"]["google_panos_yearly"] == {}

    strict_load(os.path.join(data_dir, "cities.json.gz"))


def test_mapillary_run_json(data_dir):
    run_date = date(2026, 1, 15)
    csv_path = os.path.join(
        data_dir, "duo--city_width_100_height_100_step_20_mapillary_2026-01-15.csv.gz"
    )
    # 4 panos on 2 grid points (2 each) + 1 empty point: exercises the
    # rows-vs-grid-points distinction that only exists for Mapillary
    write_city_csv_gz(
        make_mapillary_city_df(
            [
                ("m1", "2021-03-01"),
                ("m2", "2022-03-01"),
                ("m3", "2023-03-01"),
                ("m4", "2024-03-01"),
            ],
            run_date=run_date,
            panos_per_point=2,
        ),
        csv_path,
    )
    df = load_city_csv_file(csv_path)
    json_path = generate_city_metadata_summary_as_json(
        csv_path,
        df,
        "Duo",
        None,
        "Testland",
        100,
        100,
        20,
        force_recreate_file=True,
        run_date=run_date,
        provider="mapillary",
    )
    data = strict_load(json_path)

    assert data["provider"] == "mapillary"
    assert "google_panos" not in data  # all rows are already provider panos
    assert data["all_panos"]["duplicate_stats"]["total_unique_panos"] == 4
    # search points count grid points, not pano rows
    assert data["search_grid"]["total_search_points"] == 3
    assert data["data_file"]["rows"] == 5
    # contributor breakdown replaces the single '© Google' photographer
    assert all(
        k.startswith("© Mapillary contributor") for k in data["all_panos"]["top_10_photographers"]
    )
    # The GSV query radius (issue #367) is not a census concept: the summary
    # says so with null rather than claiming a tolerance nothing enforced.
    assert data["coverage"]["query_radius_m"] is None
    assert data["coverage"]["num_points_out_of_radius"] == 0


def test_mapillary_flat_only_stratifies_coverage_in_json_and_aggregate(conn, data_dir):
    # Issue #116: a Mapillary run with a flat-only point reports any-imagery
    # coverage above the 360° rate, both in the per-run JSON coverage block and
    # in the catalog-driven aggregate.
    from streetscape_metadata_tracker.analysis import calculate_run_stats

    city_id = db.register_city(
        conn,
        city_name="Flatville",
        state_name=None,
        state_code=None,
        country_name="Testland",
        country_code=None,
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=100,
        grid_height_m=100,
        step_m=20,
    )
    run_date = date(2026, 1, 15)
    name = f"{city_id}_width_100_height_100_step_20_mapillary_{run_date.isoformat()}.csv.gz"
    csv_path = os.path.join(data_dir, name)
    # 2 pano points + 1 flat-only point + 1 empty point = 4 points
    df = make_mapillary_city_df(
        [("m1", "2021-03-01"), ("m2", "2022-03-01")],
        run_date=run_date,
        n_flat_only=1,
        n_empty=1,
    )
    write_city_csv_gz(df, csv_path)
    df = load_city_csv_file(csv_path)

    json_path = generate_city_metadata_summary_as_json(
        csv_path,
        df,
        "Flatville",
        None,
        "Testland",
        100,
        100,
        20,
        force_recreate_file=True,
        run_date=run_date,
        provider="mapillary",
    )
    data = strict_load(json_path)
    cov = data["coverage"]
    assert cov["coverage_rate"] == pytest.approx(50.0)  # 2/4 pano points
    assert cov["any_imagery_coverage_rate"] == pytest.approx(75.0)  # 3/4 any imagery
    assert cov["num_points_with_any_imagery"] == 3

    stats = calculate_run_stats(df, run_date, provider="mapillary")
    db.register_run(
        conn,
        city_id=city_id,
        run_date=run_date,
        csv_filename=name,
        provider="mapillary",
        json_filename=os.path.basename(json_path),
        num_flat_images=9,
        **stats,
    )

    summary = generate_aggregate_v2(conn, data_dir)
    latest = summary["cities"][0]["providers"]["mapillary"]["latest"]
    assert latest["coverage_rate_percent"] == pytest.approx(50.0)
    assert latest["any_imagery_coverage_rate_percent"] == pytest.approx(75.0)
    assert latest["num_flat_images"] == 9
    strict_load(os.path.join(data_dir, "cities.json.gz"))


def test_aggregate_v2_groups_runs_and_reports_change(conn, data_dir):
    city_id = db.register_city(
        conn,
        city_name="Duo",
        state_name=None,
        state_code=None,
        country_name="Testland",
        country_code=None,
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=100,
        grid_height_m=100,
        step_m=20,
    )

    for run_date, panos, csv_name in [
        (
            date(2026, 1, 15),
            [("p1", "2020-01-15")],
            f"{city_id}_width_100_height_100_step_20_2026-01-15.csv.gz",
        ),
        (
            date(2026, 4, 15),
            [("p1", "2020-01-15"), ("p2", "2024-01-15")],
            f"{city_id}_width_100_height_100_step_20_2026-04-15.csv.gz",
        ),
    ]:
        csv_path = _write_run(data_dir, panos, run_date, csv_name)
        df = load_city_csv_file(csv_path)
        json_path = generate_city_metadata_summary_as_json(
            csv_path,
            df,
            "Duo",
            None,
            "Testland",
            100,
            100,
            20,
            force_recreate_file=True,
            run_date=run_date,
        )
        db.register_run(
            conn,
            city_id=city_id,
            run_date=run_date,
            csv_filename=csv_name,
            json_filename=os.path.basename(json_path),
            unique_google_panos=len(panos),
        )
    prev, latest = db.get_runs_for_city(conn, city_id)
    db.record_diff(
        conn,
        city_id=city_id,
        from_run_id=prev.run_id,
        to_run_id=latest.run_id,
        grid_aligned=True,
        panos_added=1,
        panos_removed=0,
        panos_persisted=1,
        capture_date_changed=0,
        points_gained_coverage=1,
        points_lost_coverage=0,
        coverage_delta_pct=33.3,
        detail_filename=None,
    )

    summary = generate_aggregate_v2(conn, data_dir)

    assert summary["schema_version"] == 4
    assert summary["cities_count"] == 1
    rec = summary["cities"][0]
    assert rec["city_id"] == city_id
    # schema v4 (#301): absent unless somebody excluded this city, so an
    # unexcluded catalog publishes byte-identically to v3 and the key's
    # PRESENCE is the signal a page reads.
    assert "excluded_channels" not in rec

    db.set_channel_membership(conn, city_id, "gsv", False, cycle_days=90)
    db.set_channel_membership(conn, city_id, "mapillary", True, cycle_days=90)
    rec2 = generate_aggregate_v2(conn, data_dir)["cities"][0]
    assert rec2["excluded_channels"] == ["gsv"], (
        "explicit zeroes only: an explicit 1 and a NULL are both 'not excluded'"
    )
    assert rec["city"]["name"] == "Duo"
    gsv = rec["providers"]["gsv"]
    assert len(gsv["runs"]) == 2
    assert gsv["latest"]["run_date"] == "2026-04-15"
    assert "unique_google_panos" in gsv["latest"]["panorama_counts"]
    assert "google_panos_age_stats" in gsv["latest"]
    assert gsv["change"]["panos_added"] == 1
    assert list(summary["histogram_of_capture_dates"]) == ["gsv"]

    # The grid's size in sample points, promoted from the per-run JSON's
    # search_grid block. coverage_rate_percent is a share OF these points, so
    # publishing the rate without its denominator leaves a reader unable to
    # tell a village's 40% from a metro's.
    # Distinct query points in the run CSV (json_summarizer:433), which is
    # exactly the denominator coverage_rate_percent divides by.
    assert gsv["latest"]["total_search_points"] == 3
    assert gsv["latest"]["grid"] == {
        "width_meters": 100,
        "height_meters": 100,
        "step_length_meters": 20,
    }

    # The written aggregate must be strict-parseable
    strict_load(os.path.join(data_dir, "cities.json.gz"))
    # Issue #404: no run the fill did not take carries the key, so the records
    # above are byte-identical to their pre-#404 form.
    assert all("early_refresh" not in r for r in gsv["runs"])
    # Issue #109: nothing harvested, so no provider block carries the history
    # pointer and the record is byte-identical to its pre-#109 form.
    assert "capture_history" not in gsv


def _register_history_city(conn, data_dir, name="Hist"):
    """A city with one gsv run and one Mapillary run, both summarized."""
    city_id = db.register_city(
        conn,
        city_name=name,
        state_name=None,
        state_code=None,
        country_name="Testland",
        country_code=None,
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=100,
        grid_height_m=100,
        step_m=20,
    )
    run_date = date(2026, 4, 15)
    gsv_csv = f"{city_id}_width_100_height_100_step_20_2026-04-15.csv.gz"
    gsv_path = _write_run(data_dir, [("g1", "2020-01-15")], run_date, gsv_csv)
    gsv_json = generate_city_metadata_summary_as_json(
        gsv_path,
        load_city_csv_file(gsv_path),
        name,
        None,
        "Testland",
        100,
        100,
        20,
        force_recreate_file=True,
        run_date=run_date,
    )
    db.register_run(
        conn,
        city_id=city_id,
        run_date=run_date,
        csv_filename=gsv_csv,
        json_filename=os.path.basename(gsv_json),
    )
    m_csv = f"{city_id}_width_100_height_100_step_20_mapillary_2026-04-15.csv.gz"
    m_path = os.path.join(data_dir, m_csv)
    write_city_csv_gz(make_mapillary_city_df([("m1", "2021-05-01")], run_date=run_date), m_path)
    m_json = generate_city_metadata_summary_as_json(
        m_path,
        load_city_csv_file(m_path),
        name,
        None,
        "Testland",
        100,
        100,
        20,
        force_recreate_file=True,
        run_date=run_date,
        provider="mapillary",
    )
    db.register_run(
        conn,
        city_id=city_id,
        run_date=run_date,
        csv_filename=m_csv,
        provider="mapillary",
        json_filename=os.path.basename(m_json),
    )
    return city_id


def _harvest(conn, data_dir, city_id, harvest_date, rows, *, summarize, catalog_unique=None):
    name = generate_history_filename(city_id, 100, 100, 20, harvest_date) + ".csv.gz"
    path = os.path.join(data_dir, name)
    write_city_csv_gz(make_history_df(rows), path)
    db.register_history_harvest(
        conn,
        city_id=city_id,
        harvest_date=harvest_date,
        csv_filename=name,
        unique_panos=len(rows) if catalog_unique is None else catalog_unique,
    )
    if summarize:
        generate_history_summary_as_json(
            path,
            load_history_csv_file(path),
            city_id=city_id,
            harvest_date=harvest_date,
            force_recreate_file=True,
        )
    return name


def test_aggregate_omits_capture_history_until_a_harvest_is_summarized(conn, data_dir, caplog):
    city_id = _register_history_city(conn, data_dir)
    rows = [
        ("h2009", "2009-06-01", 44.0, -121.0),
        ("h2018", "2018-06-01", 44.0, -121.0),
        ("hbad", "2611-01-01", 44.0, -121.0),
    ]
    # Cataloged with a unique_panos the summary will NOT report, so a block
    # built from the catalog row instead of the JSON is caught.
    name = _harvest(
        conn, data_dir, city_id, date(2026, 4, 10), rows, summarize=False, catalog_unique=99
    )

    # A harvest row whose summary is missing: the key stays absent, and the
    # warning names the command that fixes it.
    with caplog.at_level("WARNING"):
        rec = generate_aggregate_v2(conn, data_dir)["cities"][0]
    assert "capture_history" not in rec["providers"]["gsv"]
    assert "backfill_history_json.py" in caplog.text
    assert name in caplog.text

    generate_history_summary_as_json(
        os.path.join(data_dir, name),
        load_history_csv_file(os.path.join(data_dir, name)),
        city_id=city_id,
        harvest_date=date(2026, 4, 10),
    )
    rec = generate_aggregate_v2(conn, data_dir)["cities"][0]
    block = rec["providers"]["gsv"]["capture_history"]
    assert block == {
        "harvest_date": "2026-04-10",
        "data_file": name,
        "json_file": name.replace(".csv.gz", ".json.gz"),
        "unique_panos": 3,  # the summary's, not the catalog row's 99
        "plausibly_dated_panos": 2,
        "oldest_capture_date": "2009-06-01",
        "newest_capture_date": "2018-06-01",  # the 2611 row did not win
        "years_with_imagery": 2,
    }
    # Per (city, provider): the Mapillary block of the same city carries nothing.
    assert "capture_history" not in rec["providers"]["mapillary"]
    strict_load(os.path.join(data_dir, "cities.json.gz"))


def test_aggregate_capture_history_follows_the_latest_harvest(conn, data_dir):
    city_id = _register_history_city(conn, data_dir)
    # The newer harvest is registered FIRST, so "first row" is the wrong one.
    _harvest(
        conn,
        data_dir,
        city_id,
        date(2026, 6, 1),
        [("a", "2009-06-01", 44.0, -121.0), ("b", "2025-06-01", 44.0, -121.0)],
        summarize=True,
    )
    _harvest(
        conn,
        data_dir,
        city_id,
        date(2026, 4, 10),
        [("a", "2009-06-01", 44.0, -121.0)],
        summarize=True,
    )
    block = generate_aggregate_v2(conn, data_dir)["cities"][0]["providers"]["gsv"][
        "capture_history"
    ]
    assert block["harvest_date"] == "2026-06-01"
    assert block["unique_panos"] == 2
    assert block["newest_capture_date"] == "2025-06-01"


def test_aggregate_marks_only_the_early_refreshed_run(conn, data_dir):
    """A fill-phase run (issue #404) is marked in ``runs[]``; its sibling is not.

    Keyed on (city, provider, run_date): a row for another provider or another
    date must not mark this run, which is what a city-keyed lookup would do.
    """
    city_id = db.register_city(
        conn,
        city_name="Early",
        state_name=None,
        state_code=None,
        country_name="Testland",
        country_code=None,
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=100,
        grid_height_m=100,
        step_m=20,
    )
    for run_date in (date(2026, 1, 15), date(2026, 3, 1)):
        csv_name = f"{city_id}_width_100_height_100_step_20_{run_date}.csv.gz"
        csv_path = _write_run(data_dir, [("p1", "2020-01-15")], run_date, csv_name)
        json_path = generate_city_metadata_summary_as_json(
            csv_path,
            load_city_csv_file(csv_path),
            "Early",
            None,
            "Testland",
            100,
            100,
            20,
            force_recreate_file=True,
            run_date=run_date,
        )
        db.register_run(
            conn,
            city_id=city_id,
            run_date=run_date,
            csv_filename=csv_name,
            json_filename=os.path.basename(json_path),
        )
    db.record_early_refresh(
        conn,
        city_id,
        "gsv",
        date(2026, 3, 1),
        prior_success_at="2026-01-15T09:00:00+00:00",
        floor_days=30,
    )
    # Two decoys, neither of which may mark a gsv run: the UNMARKED run's date
    # on another channel (a channel-blind lookup would mark that run), and gsv's
    # own channel on a date with no run (a date-blind lookup would mark both).
    db.record_early_refresh(
        conn, city_id, "mapillary", date(2026, 1, 15), prior_success_at="x", floor_days=30
    )
    db.record_early_refresh(
        conn, city_id, "gsv", date(2026, 2, 1), prior_success_at="x", floor_days=30
    )
    runs = generate_aggregate_v2(conn, data_dir)["cities"][0]["providers"]["gsv"]["runs"]
    assert [r.get("early_refresh") for r in runs] == [None, True]
    assert "early_refresh" not in runs[0]


def test_aggregate_v3_two_providers(conn, data_dir):
    city_id = db.register_city(
        conn,
        city_name="Both",
        state_name=None,
        state_code=None,
        country_name="Testland",
        country_code=None,
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=100,
        grid_height_m=100,
        step_m=20,
    )

    # One gsv run and two mapillary runs (mapillary latest has a diff)
    gsv_csv = f"{city_id}_width_100_height_100_step_20_2026-01-15.csv.gz"
    csv_path = _write_run(data_dir, [("g1", "2020-01-15")], date(2026, 1, 15), gsv_csv)
    df = load_city_csv_file(csv_path)
    json_path = generate_city_metadata_summary_as_json(
        csv_path,
        df,
        "Both",
        None,
        "Testland",
        100,
        100,
        20,
        force_recreate_file=True,
        run_date=date(2026, 1, 15),
    )
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 1, 15),
        csv_filename=gsv_csv,
        json_filename=os.path.basename(json_path),
        unique_panos=1,
        unique_google_panos=1,
    )

    m_runs = []
    for run_date, panos in [
        (date(2026, 1, 15), [("m1", "2021-05-01")]),
        (date(2026, 4, 15), [("m1", "2021-05-01"), ("m2", "2024-05-01")]),
    ]:
        name = f"{city_id}_width_100_height_100_step_20_mapillary_{run_date.isoformat()}.csv.gz"
        csv_path = os.path.join(data_dir, name)
        write_city_csv_gz(make_mapillary_city_df(panos, run_date=run_date), csv_path)
        df = load_city_csv_file(csv_path)
        json_path = generate_city_metadata_summary_as_json(
            csv_path,
            df,
            "Both",
            None,
            "Testland",
            100,
            100,
            20,
            force_recreate_file=True,
            run_date=run_date,
            provider="mapillary",
        )
        m_runs.append(
            db.register_run(
                conn,
                city_id=city_id,
                run_date=run_date,
                csv_filename=name,
                provider="mapillary",
                json_filename=os.path.basename(json_path),
                unique_panos=len(panos),
            )
        )
    db.record_diff(
        conn,
        city_id=city_id,
        from_run_id=m_runs[0],
        to_run_id=m_runs[1],
        grid_aligned=True,
        panos_added=1,
        panos_removed=0,
        panos_persisted=1,
        capture_date_changed=0,
        points_gained_coverage=1,
        points_lost_coverage=0,
        coverage_delta_pct=33.3,
        detail_filename=None,
    )

    summary = generate_aggregate_v2(conn, data_dir)
    rec = summary["cities"][0]

    assert set(rec["providers"]) == {"gsv", "mapillary"}
    assert rec["city"]["name"] == "Both"  # taken from the gsv run

    mly = rec["providers"]["mapillary"]
    assert len(mly["runs"]) == 2
    assert mly["latest"]["run_date"] == "2026-04-15"
    assert mly["latest"]["panorama_counts"] == {"unique_panos": 2}
    assert "google_panos_age_stats" not in mly["latest"]
    assert mly["change"]["panos_added"] == 1
    assert mly["runs"][0]["unique_google_panos"] is None
    # The gsv series is untouched by the mapillary runs
    assert rec["providers"]["gsv"]["change"] is None
    assert len(rec["providers"]["gsv"]["runs"]) == 1

    # Per-provider global histograms; mapillary's google section stays empty
    hists = summary["histogram_of_capture_dates"]
    assert set(hists) == {"gsv", "mapillary"}
    # In-memory yearly histograms use int year keys (see
    # merge_capture_date_histograms); the strict_load below covers the
    # str-keyed JSON round-trip.
    assert hists["mapillary"]["all_panos_yearly"] == {2021: 1, 2024: 1}
    assert hists["mapillary"]["google_panos_yearly"] == {}

    strict_load(os.path.join(data_dir, "cities.json.gz"))


def test_aggregate_falls_back_to_derived_json_when_not_cataloged(conn, data_dir):
    """
    A crash between register_run and update_run_json_filename leaves
    runs.json_filename NULL while the sibling json.gz exists (or is later
    regenerated). The aggregate must fall back to the derived sibling name
    instead of silently dropping the provider from cities.json.gz forever
    (audit 2026-07-11).
    """
    city_id = db.register_city(
        conn,
        city_name="Crashy",
        state_name=None,
        state_code=None,
        country_name="Testland",
        country_code=None,
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=100,
        grid_height_m=100,
        step_m=20,
    )
    name = f"{city_id}_width_100_height_100_step_20_2026-01-15.csv.gz"
    csv_path = _write_run(data_dir, [("g1", "2020-01-15")], date(2026, 1, 15), name)
    df = load_city_csv_file(csv_path)
    # The per-run JSON exists on disk at the derived name…
    generate_city_metadata_summary_as_json(
        csv_path,
        df,
        "Crashy",
        None,
        "Testland",
        100,
        100,
        20,
        force_recreate_file=True,
        run_date=date(2026, 1, 15),
    )
    # …but the crash meant it was never linked in the catalog.
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 1, 15),
        csv_filename=name,
        json_filename=None,
        unique_panos=1,
        unique_google_panos=1,
    )

    summary = generate_aggregate_v2(conn, data_dir)
    rec = next(c for c in summary["cities"] if c["city_id"] == city_id)
    assert "gsv" in rec["providers"], "provider must not be dropped from the aggregate"
    assert rec["providers"]["gsv"]["latest"]["panorama_counts"]["unique_panos"] == 1


def test_aggregate_still_skips_provider_when_json_truly_missing(conn, data_dir):
    """No cataloged json_filename AND no sibling file → provider skipped (not a crash)."""
    city_id = db.register_city(
        conn,
        city_name="Gone",
        state_name=None,
        state_code=None,
        country_name="Testland",
        country_code=None,
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=100,
        grid_height_m=100,
        step_m=20,
    )
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 1, 15),
        csv_filename=f"{city_id}_width_100_height_100_step_20_2026-01-15.csv.gz",
        json_filename=None,
        unique_panos=1,
        unique_google_panos=1,
    )
    summary = generate_aggregate_v2(conn, data_dir)
    assert all(c["city_id"] != city_id for c in summary["cities"])


def test_aggregate_survives_a_dead_output_stream(conn, data_dir, monkeypatch):
    """
    A broken stdout/stderr pipe must not take down the aggregate.

    On 2026-08-17 a manual catch-up (`run-due ... | tail -40`) whose pipe reader
    had gone away collected 10/10 cities and published NONE of them: this
    function's "Aggregating cities" tqdm bar is the first statement of the
    scheduler's tail, and tqdm's status_printer flushes the RAW sys.stderr —
    outside the DisableOnWriteError wrapper that guards its own writes — so
    EPIPE surfaced as a hard exception and skipped the manifest, the plan
    summary, the tail catalog backup and the publish.

    The fix is tqdm's own disable=None ("off unless the stream is a TTY"), which
    returns from __init__ before status_printer is ever built. This test
    reproduces the real thing rather than the symptom: an os.pipe() whose read
    end is closed, so the first write genuinely raises BrokenPipeError.
    """
    city_id = db.register_city(
        conn,
        city_name="Piped",
        state_name=None,
        state_code=None,
        country_name="Testland",
        country_code=None,
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=100,
        grid_height_m=100,
        step_m=20,
    )
    csv_name = f"{city_id}_width_100_height_100_step_20_2026-01-15.csv.gz"
    csv_path = _write_run(data_dir, [("p1", "2020-01-15")], date(2026, 1, 15), csv_name)
    df = load_city_csv_file(csv_path)
    json_path = generate_city_metadata_summary_as_json(
        csv_path, df, "Piped", None, "Testland", 100, 100, 20, run_date=date(2026, 1, 15)
    )
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 1, 15),
        csv_filename=csv_name,
        json_filename=os.path.basename(json_path),
        unique_panos=1,
        unique_google_panos=1,
    )

    read_fd, write_fd = os.pipe()
    os.close(read_fd)  # nobody is listening: the next write raises EPIPE
    dead = os.fdopen(write_fd, "w")
    monkeypatch.setattr(sys, "stderr", dead)
    monkeypatch.setattr(sys, "stdout", dead)
    try:
        with pytest.raises(BrokenPipeError):
            dead.write("x")
            dead.flush()
        summary = generate_aggregate_v2(conn, data_dir)
    finally:
        # Closing flushes, which raises again on a broken pipe; the fd is
        # reclaimed either way and the test's assertion is what matters.
        try:
            dead.close()
        except BrokenPipeError:
            pass

    assert summary["schema_version"] == 4
    assert os.path.exists(os.path.join(data_dir, "cities.json.gz"))
    assert any(c["city_id"] == city_id for c in summary["cities"])


@pytest.mark.parametrize("provider", ["gsv", "kartaview", "mapillary", "panoramax"])
def test_published_total_search_points_is_the_deduplicated_grid_size(conn, data_dir, provider):
    """Issue #289: the per-run JSON's ``search_grid.total_search_points`` and the
    aggregate's ``latest.total_search_points`` are the DISTINCT grid-point count,
    the same number as ``runs.total_grid_points`` -- never ``runs.total_points``,
    which for a census is a row count (images + empty points). Pinned so a
    refactor cannot quietly repoint the published grid size at the row count.
    The census fixture is 9 rows on 5 points; gsv is 5 rows on 5 points."""
    from streetscape_metadata_tracker.analysis import calculate_run_stats
    from streetscape_metadata_tracker.naming import generate_run_filename
    from tests.conftest import make_kartaview_city_df, make_panoramax_city_df

    builders = {
        "kartaview": make_kartaview_city_df,
        "mapillary": make_mapillary_city_df,
        "panoramax": make_panoramax_city_df,
    }
    run_date = date(2026, 1, 15)
    if provider == "gsv":
        df = make_city_df(
            [("a", "2020-01-01"), ("b", "2021-01-01"), ("c", "2022-01-01")], n_empty=2
        )
        rows = 5
    else:
        df = builders[provider](
            [(f"x{i}", "2022-01-01") for i in range(6)],
            run_date=run_date,
            panos_per_point=3,
            n_empty=2,
            n_flat_only=1,
        )
        rows = 9
    city_id = db.register_city(
        conn,
        city_name="Gridtown",
        state_name=None,
        state_code=None,
        country_name="Testland",
        country_code=None,
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=100,
        grid_height_m=100,
        step_m=20,
    )
    name = generate_run_filename(city_id, 100, 100, 20, run_date, provider=provider) + ".csv.gz"
    csv_path = os.path.join(data_dir, name)
    write_city_csv_gz(df, csv_path)
    df = load_city_csv_file(csv_path)
    json_path = generate_city_metadata_summary_as_json(
        csv_path,
        df,
        "Gridtown",
        None,
        "Testland",
        100,
        100,
        20,
        force_recreate_file=True,
        run_date=run_date,
        provider=provider,
    )
    stats = calculate_run_stats(df, run_date, provider=provider)
    assert stats["total_points"] == rows
    assert stats["total_grid_points"] == 5
    assert strict_load(json_path)["search_grid"]["total_search_points"] == 5

    db.register_run(
        conn,
        city_id=city_id,
        run_date=run_date,
        csv_filename=name,
        provider=provider,
        json_filename=os.path.basename(json_path),
        **stats,
    )
    summary = generate_aggregate_v2(conn, data_dir)
    assert summary["cities"][0]["providers"][provider]["latest"]["total_search_points"] == 5
