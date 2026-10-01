"""Diff engine tests: pano set algebra, date changes, coverage transitions,
and the grid orchestrator's detail-file lifecycle (issue #265)."""

import gzip
import os
from datetime import date

import pandas as pd
import pytest

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.cli import _compute_and_record_diff
from streetscape_metadata_tracker.diff import (
    DIFF_DETAIL_FILENAME_RE,
    compute_run_diff,
    diff_detail_match,
    generate_diff_filename,
    is_diff_detail_filename,
)
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from streetscape_metadata_tracker.naming import (
    KNOWN_PROVIDERS,
    generate_run_filename,
    generate_streetwalk_diff_filename,
)
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


# ── The detail file is a function of the diff result (issue #265) ──────────
#
# Driven through the real cli._compute_and_record_diff, with every argument
# the collector passes, on runs whose names come from the generators.

FROM_DATE, TO_DATE = date(2026, 4, 1), date(2026, 7, 1)


def _two_run_series(conn, data_dir, provider, old_panos, new_panos):
    """Catalog two runs of one (city, provider) series with their CSVs on disk,
    and return what the collector hands _compute_and_record_diff."""
    city_id = db.register_city(
        conn,
        city_name="Bend",
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
    factory = make_mapillary_city_df if provider == "mapillary" else make_city_df
    paths, run_ids = [], []
    for run_date, panos in ((FROM_DATE, old_panos), (TO_DATE, new_panos)):
        name = generate_run_filename(city_id, 1000, 1000, 20, run_date, provider=provider)
        name += ".csv.gz"
        paths.append(write_city_csv_gz(factory(panos, run_date=run_date), f"{data_dir}/{name}"))
        run_ids.append(
            db.register_run(
                conn, city_id=city_id, run_date=run_date, csv_filename=name, provider=provider
            )
        )
    return {
        "city_row": db.get_all_cities(conn)[0],
        "prev_run": db.get_previous_run(conn, city_id, TO_DATE, provider=provider),
        "run_id": run_ids[1],
        "df_new": load_city_csv_file(paths[1]),
        "old_path": paths[0],
        "detail": generate_diff_filename(
            city_id, FROM_DATE.isoformat(), TO_DATE.isoformat(), provider=provider
        ),
    }


def _grid_diff(conn, data_dir, series, provider):
    return _compute_and_record_diff(
        conn,
        series["city_row"],
        series["prev_run"],
        series["run_id"],
        TO_DATE,
        series["df_new"],
        data_dir,
        provider=provider,
    )


def _run_diff_row(conn, run_id):
    return conn.execute("SELECT * FROM run_diffs WHERE to_run_id = ?", (run_id,)).fetchone()


@pytest.mark.parametrize("provider", ["gsv", "mapillary"])
def test_grid_rediff_with_no_changes_removes_the_detail_file(conn, data_dir, provider):
    """A diff that wrote a detail file, recomputed to no changes, removes it:
    record_diff's INSERT OR REPLACE nulls the row's pointer, so a surviving
    file is published with nothing referencing it. Parametrized over provider
    because the name is DERIVED — a dropped provider argument would remove
    the gsv name and leave mapillary's file behind."""
    series = _two_run_series(
        conn, data_dir, provider, [("p1", "2020-05-01")], [("p2", "2024-05-01")]
    )
    detail_path = os.path.join(data_dir, series["detail"])
    assert _grid_diff(conn, data_dir, series, provider)["diff_file"] == series["detail"]
    assert os.path.exists(detail_path)

    # The same pair recomputed against a new side that no longer differs.
    series["df_new"] = load_city_csv_file(series["old_path"])
    change = _grid_diff(conn, data_dir, series, provider)
    assert change["diff_file"] is None
    assert _run_diff_row(conn, series["run_id"])["detail_filename"] is None
    assert not os.path.exists(detail_path)


def test_grid_rediff_with_changes_overwrites_a_stale_detail_file(conn, data_dir):
    """The has-changes path REWRITES the file from this diff — asserted on
    content, because a stale file and a fresh one both 'exist'."""
    series = _two_run_series(conn, data_dir, "gsv", [("p1", "2020-05-01")], [("p2", "2024-05-01")])
    detail_path = os.path.join(data_dir, series["detail"])
    with gzip.open(detail_path, "wt") as fh:
        fh.write("stale,content\nfrom,before\n")
    _grid_diff(conn, data_dir, series, "gsv")
    with gzip.open(detail_path, "rt") as fh:
        back = pd.read_csv(fh)
    assert "stale" not in back.columns
    assert set(back["pano_id"]) == {"p1", "p2"}
    assert _run_diff_row(conn, series["run_id"])["detail_filename"] == series["detail"]


def test_grid_missing_previous_file_records_and_removes_nothing(conn, data_dir):
    """The 'previous run file missing' return fires before any row or file is
    written, for a to_run_id that has no run_diffs row yet — so it cannot
    strand a file, and it deletes nothing on behalf of a comparison it never
    made (a file already at that name is the orphan sweep's to judge)."""
    series = _two_run_series(conn, data_dir, "gsv", [("p1", "2020-05-01")], [("p1", "2020-05-01")])
    planted = os.path.join(data_dir, series["detail"])
    with open(planted, "w") as fh:
        fh.write("x")
    os.remove(series["old_path"])
    assert _grid_diff(conn, data_dir, series, "gsv") is None
    assert _run_diff_row(conn, series["run_id"]) is None
    assert os.path.exists(planted)


# ── The diff-detail filename shape the orphan sweep keys on (issue #265) ────


def test_diff_detail_regex_matches_exactly_what_the_generator_emits():
    for provider in KNOWN_PROVIDERS:
        name = generate_diff_filename("st.-louis--mo", "2026-04-01", "2026-07-01", provider)
        m = DIFF_DETAIL_FILENAME_RE.match(name)
        assert m, name
        assert m["slug"] == "st.-louis--mo"
        assert (m["provider"] or "gsv") == provider
        assert (m["from_date"], m["to_date"]) == ("2026-04-01", "2026-07-01")
        assert is_diff_detail_filename(name)


def test_diff_detail_regex_rejects_near_misses():
    """Every name here is one the sweep must never delete."""
    run = generate_run_filename("my_diff_city", 1000, 1000, 20, TO_DATE)
    for name in [
        run + ".csv.gz",  # a run CSV whose slug contains '_diff_'
        run + ".json.gz",
        "bend--or_diff_gsv_2026-04-01_to_2026-07-01.csv.gz",  # the generator never writes gsv_
        "bend--or_diff_bing_2026-04-01_to_2026-07-01.csv.gz",
        "bend--or_diff_2026-04-01_to_2026-07-01.csv",
        "bend--or_diff_2026-04-01_to_2026-07-01.csv.gz.downloading",
        "bend--or_diff_2026-04-01_2026-07-01.csv.gz",
        "cities.json.gz",
        # '$' would accept these (it matches before a trailing newline).
        generate_diff_filename("bend--or", "2026-04-01", "2026-07-01") + "\n",
        generate_streetwalk_diff_filename("bend--or", "2026-04-01", "2026-07-01") + "\n",
    ]:
        assert not is_diff_detail_filename(name), repr(name)
        assert diff_detail_match(name) is None, repr(name)
    # The pattern itself ends in \Z, so even a caller using .match() directly
    # (rather than the fullmatch predicates) cannot accept a trailing newline.
    newline = generate_diff_filename("bend--or", "2026-04-01", "2026-07-01") + "\n"
    assert DIFF_DETAIL_FILENAME_RE.match(newline) is None


def test_diff_detail_match_exposes_the_dates_for_both_families():
    for name in (
        generate_diff_filename("bend--or", "2026-04-01", "2026-07-01", provider="kartaview"),
        generate_streetwalk_diff_filename(
            "bend--or", "2026-04-01", "2026-07-01", "panoramax", "all_public"
        ),
    ):
        m = diff_detail_match(name)
        assert (m["from_date"], m["to_date"]) == ("2026-04-01", "2026-07-01")


@pytest.mark.parametrize(("provider", "sibling"), [("mapillary", "gsv"), ("gsv", "mapillary")])
def test_grid_no_change_rediff_removes_only_its_own_providers_file(
    conn, data_dir, provider, sibling
):
    """Both directions (#402 review): a no-change re-diff of one provider must
    not remove the other provider's live file for the SAME date pair — the
    names differ only by the provider token."""
    panos = [("p1", "2020-05-01")]
    series = _two_run_series(conn, data_dir, provider, panos, panos)
    city_id = series["city_row"].city_id
    sibling_path = os.path.join(
        data_dir,
        generate_diff_filename(
            city_id, FROM_DATE.isoformat(), TO_DATE.isoformat(), provider=sibling
        ),
    )
    own_path = os.path.join(data_dir, series["detail"])
    for path in (sibling_path, own_path):
        with open(path, "w") as fh:
            fh.write("x")
    assert _grid_diff(conn, data_dir, series, provider)["diff_file"] is None
    assert not os.path.exists(own_path)  # the branch was reached
    assert os.path.exists(sibling_path)
