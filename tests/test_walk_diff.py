"""Walk-to-walk diff engine (issue #101): compute_walk_diff and the
compute_and_record_walk_diff orchestrator shared by collect.py and the
scheduler's walk-salvage path."""

import gzip
import json
import os
from datetime import date

import pandas as pd
import pytest

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.walk_diff import (
    DETAIL_COLUMNS,
    compute_and_record_walk_diff,
    compute_walk_diff,
    write_walk_diff_detail,
)


def _edge(
    edge_id,
    fraction=0.5,
    any_fraction=None,
    pano_date="2020-06-01",
    highway="residential",
    length_m=100.0,
):
    """One coverage-GeoJSON feature. any_fraction defaults to fraction (the
    GSV by-construction equality); pass explicitly to model Mapillary flats."""
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[0, 0], [0, 0.001]]},
        "properties": {
            "edge_id": edge_id,
            "highway": highway,
            "length_m": length_m,
            "total_samples": 10,
            "covered_samples": int(fraction * 10),
            "coverage_fraction": fraction,
            "covered": fraction >= 1.0,
            "coverage_fraction_any": fraction if any_fraction is None else any_fraction,
            "nearest_pano_date": pano_date,
        },
    }


def _fc(features, totals=None, **metadata):
    """A coverage FeatureCollection with the collector's metadata shape."""
    meta = {
        "schema_version": 1,
        "kind": "streetwalk_coverage",
        "totals": totals if totals is not None else {},
    }
    meta.update(metadata)
    return {
        "type": "FeatureCollection",
        "properties": {"metadata": meta},
        "features": features,
    }


def test_no_changes_between_identical_walks():
    fc = _fc([_edge("1-2"), _edge("2-3", fraction=1.0)])
    diff = compute_walk_diff(fc, fc)
    assert diff.edges_aligned == 2
    assert diff.edges_added == 0
    assert diff.edges_removed == 0
    assert diff.edges_gained_coverage == 0
    assert diff.edges_lost_coverage == 0
    assert diff.coverage_fraction_changed == 0
    assert diff.nearest_pano_date_changed == 0
    assert diff.edges_fully_covered_delta == 0
    assert not diff.has_changes
    assert len(diff.detail) == 0


def test_edge_gains_and_loses_any_coverage():
    old = _fc([_edge("1-2", fraction=0.0, pano_date=None), _edge("2-3", fraction=0.6)])
    new = _fc([_edge("1-2", fraction=0.4), _edge("2-3", fraction=0.0, pano_date=None)])
    diff = compute_walk_diff(old, new)
    assert diff.edges_gained_coverage == 1
    assert diff.edges_lost_coverage == 1
    # Gained/lost are transitions of the fraction, so both also count as changed.
    assert diff.coverage_fraction_changed == 2
    assert diff.has_changes
    by_type = dict(zip(diff.detail["edge_id"], diff.detail["change_type"], strict=True))
    assert by_type == {"1-2": "gained_coverage", "2-3": "lost_coverage"}


def test_fraction_change_without_transition_counts_as_coverage_changed():
    old = _fc([_edge("1-2", fraction=0.4)])
    new = _fc([_edge("1-2", fraction=0.8)])
    diff = compute_walk_diff(old, new)
    assert diff.edges_gained_coverage == 0
    assert diff.edges_lost_coverage == 0
    assert diff.coverage_fraction_changed == 1
    assert diff.detail["change_type"].tolist() == ["coverage_changed"]
    row = diff.detail.iloc[0]
    assert row["old_coverage_fraction"] == 0.4
    assert row["new_coverage_fraction"] == 0.8


def test_nearest_pano_date_change_detected():
    old = _fc([_edge("1-2", pano_date="2019-05-01"), _edge("2-3", pano_date=None)])
    new = _fc([_edge("1-2", pano_date="2026-03-01"), _edge("2-3", pano_date="2026-03-01")])
    diff = compute_walk_diff(old, new)
    assert diff.nearest_pano_date_changed == 2
    assert diff.coverage_fraction_changed == 0
    assert set(diff.detail["change_type"]) == {"pano_date_changed"}
    row = diff.detail.set_index("edge_id").loc["2-3"]
    assert pd.isna(row["old_nearest_pano_date"]) or row["old_nearest_pano_date"] is None
    assert row["new_nearest_pano_date"] == "2026-03-01"


def test_overlapping_changes_counted_independently_but_one_detail_row():
    """An edge that gains coverage AND changes its pano date increments both
    headline counters but emits ONE detail row, labeled by precedence."""
    old = _fc([_edge("1-2", fraction=0.0, pano_date=None)])
    new = _fc([_edge("1-2", fraction=0.7, pano_date="2026-01-01")])
    diff = compute_walk_diff(old, new)
    assert diff.edges_gained_coverage == 1
    assert diff.coverage_fraction_changed == 1
    assert diff.nearest_pano_date_changed == 1
    assert len(diff.detail) == 1
    assert diff.detail["change_type"].tolist() == ["gained_coverage"]


def test_refreshed_network_diffs_the_intersection_only():
    """One-sided edges are network churn: reported as added/removed but never
    as coverage gained/lost — a brand-new covered edge is not a gain."""
    old = _fc([_edge("1-2"), _edge("2-3", fraction=0.0, pano_date=None)])
    new = _fc([_edge("1-2"), _edge("3-4", fraction=0.9)])
    diff = compute_walk_diff(old, new)
    assert diff.edges_aligned == 1
    assert diff.edges_added == 1
    assert diff.edges_removed == 1
    assert diff.edges_gained_coverage == 0
    assert diff.edges_lost_coverage == 0
    assert diff.has_changes
    by_type = dict(zip(diff.detail["edge_id"], diff.detail["change_type"], strict=True))
    assert by_type == {"3-4": "edge_added", "2-3": "edge_removed"}
    added = diff.detail.set_index("edge_id").loc["3-4"]
    assert pd.isna(added["old_coverage_fraction"])
    assert added["new_coverage_fraction"] == 0.9


def test_any_fraction_falls_back_to_fraction_on_pre_116_artifacts():
    """Features without coverage_fraction_any (pre-#116 artifacts) diff on the
    360° fraction rather than crashing or treating every edge as changed."""
    old_feature = _edge("1-2", fraction=0.0, pano_date=None)
    del old_feature["properties"]["coverage_fraction_any"]
    new = _fc([_edge("1-2", fraction=0.5)])
    diff = compute_walk_diff(_fc([old_feature]), new)
    assert diff.edges_gained_coverage == 1


def test_totals_deltas_come_from_artifact_metadata():
    old = _fc(
        [_edge("1-2")],
        totals={"coverage_pct_by_length": 62.1, "coverage_pct_by_length_any": 64.0},
    )
    new = _fc(
        [_edge("1-2")],
        totals={"coverage_pct_by_length": 63.4, "coverage_pct_by_length_any": 66.2},
    )
    diff = compute_walk_diff(old, new)
    assert diff.coverage_pct_by_length_delta == pytest.approx(1.3)
    assert diff.coverage_pct_by_length_any_delta == pytest.approx(2.2)


def test_any_delta_is_none_when_either_side_lacks_any_totals():
    """A pre-v8 walk never measured any-imagery coverage; the delta is 'not
    measured', never a copy of the 360° delta or zero."""
    old = _fc([_edge("1-2")], totals={"coverage_pct_by_length": 62.1})
    new = _fc(
        [_edge("1-2")],
        totals={"coverage_pct_by_length": 63.4, "coverage_pct_by_length_any": 66.2},
    )
    diff = compute_walk_diff(old, new)
    assert diff.coverage_pct_by_length_delta == pytest.approx(1.3)
    assert diff.coverage_pct_by_length_any_delta is None


def test_fully_covered_delta_counts_full_artifacts():
    old = _fc([_edge("1-2", fraction=1.0), _edge("2-3", fraction=0.5)])
    new = _fc([_edge("1-2", fraction=1.0), _edge("2-3", fraction=1.0)])
    diff = compute_walk_diff(old, new)
    assert diff.edges_fully_covered_delta == 1


def test_write_walk_diff_detail_roundtrip(tmp_path):
    old = _fc([_edge("1-2", fraction=0.2)])
    new = _fc([_edge("1-2", fraction=0.9)])
    diff = compute_walk_diff(old, new)
    out = str(tmp_path / "diff.csv.gz")
    write_walk_diff_detail(diff, out)
    with gzip.open(out, "rt", encoding="utf-8") as f:
        back = pd.read_csv(f)
    assert list(back.columns) == DETAIL_COLUMNS
    assert back["edge_id"].tolist() == ["1-2"]
    assert back["change_type"].tolist() == ["coverage_changed"]


# ── Orchestrator ───────────────────────────────────────────────────────────


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


def _write_coverage(data_dir, filename, fc):
    path = os.path.join(data_dir, filename)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(fc, fh)
    return path


def _register_walk(
    conn,
    data_dir,
    city_id,
    run_date,
    fc,
    *,
    provider="gsv",
    network_type="drive",
    spacing_m=15.0,
    match_dist_m=25.0,
):
    """Catalog a walk and write its coverage artifact, name via the generator
    (never by hand — the token rules are the whole point)."""
    from streetscape_metadata_tracker.naming import (
        generate_streetwalk_filename,
        streetwalk_coverage_filename,
    )

    stem = generate_streetwalk_filename(
        city_id, 5000, 5000, 20, spacing_m, run_date, provider=provider, network_type=network_type
    )
    csv_name = stem + ".csv.gz"
    coverage_name = streetwalk_coverage_filename(csv_name)
    _write_coverage(data_dir, coverage_name, fc)
    walk_id = db.register_street_walk(
        conn,
        city_id=city_id,
        run_date=run_date,
        csv_filename=csv_name,
        provider=provider,
        coverage_filename=coverage_name,
        network_type=network_type,
        spacing_m=spacing_m,
        match_dist_m=match_dist_m,
    )
    return walk_id, coverage_name


def _walk_diff_rows(conn):
    return conn.execute("SELECT * FROM street_walk_diffs").fetchall()


def test_first_walk_returns_none_and_records_nothing(conn, data_dir):
    city_id = _register_city(conn)
    fc = _fc([_edge("1-2")])
    walk_id, _ = _register_walk(conn, data_dir, city_id, date(2026, 7, 1), fc)
    change = compute_and_record_walk_diff(
        conn,
        data_dir=data_dir,
        city_id=city_id,
        walk_id=walk_id,
        run_date=date(2026, 7, 1),
        provider="gsv",
        network_type="drive",
        spacing_m=15.0,
        match_dist_m=25.0,
        fc_new=fc,
    )
    assert change is None
    assert _walk_diff_rows(conn) == []


def test_spacing_mismatch_skips_the_diff(conn, data_dir):
    city_id = _register_city(conn)
    fc = _fc([_edge("1-2")])
    _register_walk(conn, data_dir, city_id, date(2026, 4, 1), fc, spacing_m=15.0)
    walk_id, _ = _register_walk(conn, data_dir, city_id, date(2026, 7, 1), fc, spacing_m=30.0)
    change = compute_and_record_walk_diff(
        conn,
        data_dir=data_dir,
        city_id=city_id,
        walk_id=walk_id,
        run_date=date(2026, 7, 1),
        provider="gsv",
        network_type="drive",
        spacing_m=30.0,
        match_dist_m=25.0,
        fc_new=fc,
    )
    assert change is None
    assert _walk_diff_rows(conn) == []


def test_match_dist_mismatch_skips_the_diff(conn, data_dir):
    city_id = _register_city(conn)
    fc = _fc([_edge("1-2")])
    _register_walk(conn, data_dir, city_id, date(2026, 4, 1), fc, match_dist_m=25.0)
    walk_id, _ = _register_walk(conn, data_dir, city_id, date(2026, 7, 1), fc, match_dist_m=50.0)
    change = compute_and_record_walk_diff(
        conn,
        data_dir=data_dir,
        city_id=city_id,
        walk_id=walk_id,
        run_date=date(2026, 7, 1),
        provider="gsv",
        network_type="drive",
        spacing_m=15.0,
        match_dist_m=50.0,
        fc_new=fc,
    )
    assert change is None
    assert _walk_diff_rows(conn) == []


def test_missing_previous_artifact_skips_the_diff(conn, data_dir):
    city_id = _register_city(conn)
    fc = _fc([_edge("1-2")])
    _, prev_coverage = _register_walk(conn, data_dir, city_id, date(2026, 4, 1), fc)
    os.remove(os.path.join(data_dir, prev_coverage))
    walk_id, _ = _register_walk(conn, data_dir, city_id, date(2026, 7, 1), fc)
    change = compute_and_record_walk_diff(
        conn,
        data_dir=data_dir,
        city_id=city_id,
        walk_id=walk_id,
        run_date=date(2026, 7, 1),
        provider="gsv",
        network_type="drive",
        spacing_m=15.0,
        match_dist_m=25.0,
        fc_new=fc,
    )
    assert change is None
    assert _walk_diff_rows(conn) == []


def test_series_isolation_never_diffs_across_provider_or_network(conn, data_dir):
    """A gsv/drive walk must not diff against a mapillary or all_public walk
    of the same city — different series entirely."""
    city_id = _register_city(conn)
    fc = _fc([_edge("1-2")])
    _register_walk(conn, data_dir, city_id, date(2026, 4, 1), fc, provider="mapillary")
    _register_walk(conn, data_dir, city_id, date(2026, 4, 2), fc, network_type="all_public")
    walk_id, _ = _register_walk(conn, data_dir, city_id, date(2026, 7, 1), fc)
    change = compute_and_record_walk_diff(
        conn,
        data_dir=data_dir,
        city_id=city_id,
        walk_id=walk_id,
        run_date=date(2026, 7, 1),
        provider="gsv",
        network_type="drive",
        spacing_m=15.0,
        match_dist_m=25.0,
        fc_new=fc,
    )
    assert change is None
    assert _walk_diff_rows(conn) == []


def test_happy_path_records_row_detail_and_change_block(conn, data_dir):
    city_id = _register_city(conn)
    old_fc = _fc(
        [_edge("1-2", fraction=0.0, pano_date=None), _edge("2-3", fraction=0.5)],
        totals={"coverage_pct_by_length": 50.0, "coverage_pct_by_length_any": 50.0},
    )
    new_fc = _fc(
        [_edge("1-2", fraction=0.8), _edge("2-3", fraction=0.5)],
        totals={"coverage_pct_by_length": 75.0, "coverage_pct_by_length_any": 75.0},
    )
    from_walk_id, _ = _register_walk(conn, data_dir, city_id, date(2026, 4, 1), old_fc)
    walk_id, _ = _register_walk(conn, data_dir, city_id, date(2026, 7, 1), new_fc)
    change = compute_and_record_walk_diff(
        conn,
        data_dir=data_dir,
        city_id=city_id,
        walk_id=walk_id,
        run_date=date(2026, 7, 1),
        provider="gsv",
        network_type="drive",
        spacing_m=15.0,
        match_dist_m=25.0,
        fc_new=new_fc,
    )
    assert change is not None
    assert change["from"] == "2026-04-01"
    assert change["to"] == "2026-07-01"
    assert change["edges_gained_coverage"] == 1
    assert change["coverage_pct_by_length_delta"] == pytest.approx(25.0)
    assert change["diff_file"] is not None
    assert os.path.exists(os.path.join(data_dir, change["diff_file"]))

    rows = _walk_diff_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["from_walk_id"] == from_walk_id
    assert row["to_walk_id"] == walk_id
    assert row["edges_gained_coverage"] == 1
    assert row["detail_filename"] == change["diff_file"]

    joined = db.get_walk_diff_for_walk(conn, walk_id)
    assert joined["from_run_date"] == "2026-04-01"


def test_identical_walks_record_row_without_detail_file(conn, data_dir):
    """'Diffed, nothing changed' is a recorded fact, but no detail file is
    published (mirrors the grid diff)."""
    city_id = _register_city(conn)
    fc = _fc([_edge("1-2")], totals={"coverage_pct_by_length": 50.0})
    _register_walk(conn, data_dir, city_id, date(2026, 4, 1), fc)
    walk_id, _ = _register_walk(conn, data_dir, city_id, date(2026, 7, 1), fc)
    change = compute_and_record_walk_diff(
        conn,
        data_dir=data_dir,
        city_id=city_id,
        walk_id=walk_id,
        run_date=date(2026, 7, 1),
        provider="gsv",
        network_type="drive",
        spacing_m=15.0,
        match_dist_m=25.0,
        fc_new=fc,
    )
    assert change is not None
    assert change["diff_file"] is None
    rows = _walk_diff_rows(conn)
    assert len(rows) == 1
    assert rows[0]["detail_filename"] is None
    assert not [f for f in os.listdir(data_dir) if "streetwalkdiff" in f]


def test_fc_new_loaded_from_catalog_when_not_passed(conn, data_dir):
    """The salvage path may not hold the new FC in memory; the orchestrator
    loads it from the walk's cataloged coverage_filename."""
    city_id = _register_city(conn)
    old_fc = _fc([_edge("1-2", fraction=0.2)], totals={"coverage_pct_by_length": 20.0})
    new_fc = _fc([_edge("1-2", fraction=0.9)], totals={"coverage_pct_by_length": 90.0})
    _register_walk(conn, data_dir, city_id, date(2026, 4, 1), old_fc)
    walk_id, _ = _register_walk(conn, data_dir, city_id, date(2026, 7, 1), new_fc)
    change = compute_and_record_walk_diff(
        conn,
        data_dir=data_dir,
        city_id=city_id,
        walk_id=walk_id,
        run_date=date(2026, 7, 1),
        provider="gsv",
        network_type="drive",
        spacing_m=15.0,
        match_dist_m=25.0,
    )
    assert change is not None
    assert change["coverage_pct_by_length_delta"] == pytest.approx(70.0)
