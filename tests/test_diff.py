"""Diff engine tests: pano set algebra, date changes, coverage transitions."""

import os
from datetime import date

import pandas as pd

from streetscape_metadata_tracker.diff import compute_run_diff, generate_diff_filename
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from streetscape_metadata_tracker.naming import generate_run_filename
from tests.conftest import COLUMNS, make_city_df, make_mapillary_city_df, write_city_csv_gz


def _two_point_df(point_b_status, point_b_pano, point_b_date):
    """Grid of two points: A always holds an OK pano; B varies by status."""
    ts = "2026-01-15T12:00:00+00:00"
    rows = [(44.000, -121.0, ts, 44.0001, -121.0001, "a", "2020-01-01", "© Google", "OK")]
    if point_b_status == "ZERO_RESULTS":
        rows.append((44.001, -121.0, ts, None, None, None, None, None, "ZERO_RESULTS"))
    else:
        rows.append(
            (
                44.001,
                -121.0,
                ts,
                44.0011,
                -121.0011,
                point_b_pano,
                point_b_date,
                "© Google",
                point_b_status,
            )
        )
    return pd.DataFrame(rows, columns=COLUMNS)


def test_no_changes_between_identical_runs():
    df = make_city_df([("p1", "2020-05-01"), ("p2", "2021-06-01")])
    d = compute_run_diff(df, df.copy())
    assert not d.has_changes
    assert d.panos_persisted == 2
    assert d.grid_aligned and d.coverage_delta_pct == 0.0
    assert len(d.detail) == 0


def test_added_removed_and_date_changed():
    old = make_city_df([("p1", "2020-05-01"), ("p2", "2021-06-01")])
    new = make_city_df(
        [
            ("p1", "2024-08-01"),  # re-dated
            ("p3", "2025-01-01"),
        ]
    )  # p2 removed, p3 added
    d = compute_run_diff(old, new)
    assert (d.panos_added, d.panos_removed, d.panos_persisted) == (1, 1, 1)
    assert d.capture_date_changed == 1
    assert set(d.detail["change_type"]) == {"pano_added", "pano_removed", "capture_date_changed"}
    row = d.detail[d.detail["change_type"] == "capture_date_changed"].iloc[0]
    assert row["old_capture_date"] == "2020-05-01"
    assert row["new_capture_date"] == "2024-08-01"


def test_coverage_transitions_on_aligned_grid():
    # 2 panos + 1 empty point vs 3 panos + 0 empty on the same 3-point grid
    old = make_city_df([("p1", "2020-05-01"), ("p2", "2021-06-01")], n_empty=1)
    new = make_city_df(
        [("p1", "2020-05-01"), ("p2", "2021-06-01"), ("p3", "2025-01-01")], n_empty=0
    )
    d = compute_run_diff(old, new)
    assert d.grid_aligned
    assert d.points_gained_coverage == 1 and d.points_lost_coverage == 0
    assert abs(d.coverage_delta_pct - 100 / 3) < 1e-9


def test_no_date_point_gains_coverage():
    # Point B goes ZERO_RESULTS -> NO_DATE: dateless imagery appeared, so the
    # point is now covered (schema v3).
    old = _two_point_df("ZERO_RESULTS", None, None)
    new = _two_point_df("NO_DATE", "b", None)
    d = compute_run_diff(old, new)
    assert d.grid_aligned
    assert d.points_gained_coverage == 1 and d.points_lost_coverage == 0
    assert abs(d.coverage_delta_pct - 50.0) < 1e-9


def test_no_date_point_lost_coverage():
    # Reverse: NO_DATE -> ZERO_RESULTS is a loss of coverage.
    old = _two_point_df("NO_DATE", "b", None)
    new = _two_point_df("ZERO_RESULTS", None, None)
    d = compute_run_diff(old, new)
    assert d.points_gained_coverage == 0 and d.points_lost_coverage == 1
    assert abs(d.coverage_delta_pct + 50.0) < 1e-9


def test_mapillary_grid_aligned_despite_differing_row_counts():
    # Mapillary is a census: one row per pano, so two runs of the identical
    # frozen grid rarely have equal row counts. Alignment must compare unique
    # grid points, not rows (regression: row-count check nulled all Mapillary
    # coverage transitions).
    # Old: 2 panos on point 0, point 1 empty (3 rows, 2 grid points).
    old = make_mapillary_city_df(
        [("m1", "2023-01-01"), ("m2", "2023-02-01")], panos_per_point=2, n_empty=1
    )
    # New: 2 panos on each of points 0 and 1 (4 rows, same 2 grid points).
    new = make_mapillary_city_df(
        [("m1", "2023-01-01"), ("m2", "2023-02-01"), ("m3", "2024-01-01"), ("m4", "2024-02-01")],
        panos_per_point=2,
        n_empty=0,
    )
    d = compute_run_diff(old, new)
    assert d.grid_aligned
    assert d.points_gained_coverage == 1 and d.points_lost_coverage == 0
    assert abs(d.coverage_delta_pct - 50.0) < 1e-9
    assert (d.panos_added, d.panos_removed, d.panos_persisted) == (2, 0, 2)


def test_mapillary_census_growth_on_same_point_keeps_alignment():
    # A point gaining extra pano rows changes row counts but not the grid or
    # its coverage: aligned, zero coverage delta.
    old = make_mapillary_city_df([("m1", "2023-01-01")], panos_per_point=1, n_empty=1)
    new = make_mapillary_city_df(
        [("m1", "2023-01-01"), ("m2", "2024-01-01")], panos_per_point=2, n_empty=1
    )
    d = compute_run_diff(old, new)
    assert d.grid_aligned
    assert d.points_gained_coverage == 0 and d.points_lost_coverage == 0
    assert d.coverage_delta_pct == 0.0
    assert d.panos_added == 1


def test_misaligned_grid_skips_point_stats():
    old = make_city_df([("p1", "2020-05-01")])
    new = make_city_df([("p1", "2020-05-01")], grid_origin=(45.0, -120.0))
    d = compute_run_diff(old, new)
    assert not d.grid_aligned
    assert d.points_gained_coverage is None
    assert d.coverage_delta_pct is None
    # Pano-level diff still works
    assert d.panos_persisted == 1


def test_ok_to_no_date_flip_is_a_date_change_not_churn():
    # A pano whose date disappears (OK -> NO_DATE, common for Mapillary's
    # bogus contributor timestamps) is still present in both runs: it must
    # count as persisted + capture_date_changed, never as removed + added.
    old = _two_point_df("OK", "b", "2021-03-01")
    new = _two_point_df("NO_DATE", "b", None)
    d = compute_run_diff(old, new)
    assert (d.panos_added, d.panos_removed, d.panos_persisted) == (0, 0, 2)
    assert d.capture_date_changed == 1
    row = d.detail[d.detail["change_type"] == "capture_date_changed"].iloc[0]
    assert row["old_capture_date"] == "2021-03-01"
    assert row["new_capture_date"] is None


def test_no_date_to_ok_flip_is_a_date_change_not_churn():
    old = _two_point_df("NO_DATE", "b", None)
    new = _two_point_df("OK", "b", "2026-05-01")
    d = compute_run_diff(old, new)
    assert (d.panos_added, d.panos_removed) == (0, 0)
    assert d.capture_date_changed == 1


def test_duplicate_pano_ids_deduped_keeping_newest_date():
    old = make_city_df([("p1", "2020-05-01"), ("p1", "2022-03-01")])
    new = make_city_df([("p1", "2022-03-01")])
    d = compute_run_diff(old, new)
    assert d.panos_persisted == 1
    assert d.capture_date_changed == 0  # newest old date matches new date


def test_diff_filename():
    assert (
        generate_diff_filename("bend--or", "2026-04-01", "2026-07-01")
        == "bend--or_diff_2026-04-01_to_2026-07-01.csv.gz"
    )


def test_diff_filename_provider():
    # gsv keeps the tokenless legacy form; other providers get a token
    assert (
        generate_diff_filename("bend--or", "2026-04-01", "2026-07-01", provider="gsv")
        == "bend--or_diff_2026-04-01_to_2026-07-01.csv.gz"
    )
    assert (
        generate_diff_filename("bend--or", "2026-04-01", "2026-07-01", provider="mapillary")
        == "bend--or_diff_mapillary_2026-04-01_to_2026-07-01.csv.gz"
    )


# --- issue #367: both sides of a diff come through the query-radius seam -----


def test_a_far_pano_in_both_runs_is_neither_churn_nor_coverage(data_dir):
    """Point A holds the SAME far pano in both runs: absent on both sides, so
    no add/remove. Point B held only a far pano before and a near one now:
    that is coverage gained, which it would not be if the far pano had
    counted."""

    def run(run_date, b_pano, b_offset_deg):
        df = make_city_df([("far_a", "2024-01-01"), (b_pano, "2024-01-01")], run_date=run_date)
        df.loc[0, "pano_lat"] = df.loc[0, "query_lat"] + 0.01  # ~1.1 km
        df.loc[1, "pano_lat"] = df.loc[1, "query_lat"] + b_offset_deg
        name = generate_run_filename("bend--or", 100, 100, 20, run_date)
        path = os.path.join(data_dir, name + ".csv.gz")
        write_city_csv_gz(df, path)
        return load_city_csv_file(path)

    old = run(date(2026, 1, 15), "far_b", 0.01)
    new = run(date(2026, 4, 15), "near_b", 0.0001)
    d = compute_run_diff(old, new)

    assert d.panos_removed == 0  # far_a and far_b were never counted
    assert d.panos_added == 1  # near_b
    assert d.points_gained_coverage == 1  # B: far -> near
    assert d.points_lost_coverage == 0
