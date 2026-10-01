"""Walk-to-walk diff engine (issue #101): compute_walk_diff and the
compute_and_record_walk_diff orchestrator shared by collect.py and the
scheduler's walk-salvage path."""

import gzip
import json
import os
from datetime import date

import pandas as pd
import pytest

from streetscape_metadata_tracker import db, fileutils
from streetscape_metadata_tracker.fileutils import remove_stale_diff_detail
from streetscape_metadata_tracker.naming import generate_streetwalk_diff_filename
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


def test_recollection_with_changed_frame_clears_stale_diff(conn, data_dir):
    """A same-day re-collection replaces the walk row in place (the register
    upsert keeps walk_id). If the re-collection changed the sample frame, the
    diff recorded by the earlier collection describes a replaced artifact —
    the skip path must clear it, or the manifest keeps advertising a stale
    change block."""
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
        fc_new=new_fc,
    )
    assert change is not None  # the first collection diffed normally

    # Re-collect the same date at a different spacing: same walk_id, new frame.
    recollected_id, _ = _register_walk(
        conn, data_dir, city_id, date(2026, 7, 1), new_fc, spacing_m=30.0
    )
    assert recollected_id == walk_id
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
        fc_new=new_fc,
    )
    assert change is None
    assert _walk_diff_rows(conn) == []  # the stale diff row is gone


# ── The detail file is a function of the diff result (issue #265) ──────────
#
# Every case starts from a state the bug produced: a walk diff that wrote a
# detail file, followed by a re-collection or re-diff. Two removals are pinned
# separately because they reach different files: the cleared row's OWN
# pointer (every early return, and a predecessor that changed) and the
# deterministic name in the no-changes branch (a file no row points at).

D0, D1, D2 = date(2026, 4, 1), date(2026, 5, 1), date(2026, 7, 1)
OLD_FC = _fc(
    [_edge("1-2", fraction=0.0, pano_date=None), _edge("2-3", fraction=0.5)],
    totals={"coverage_pct_by_length": 25.0, "coverage_pct_by_length_any": 25.0},
)
NEW_FC = _fc(
    [_edge("1-2", fraction=0.8), _edge("2-3", fraction=0.5)],
    totals={"coverage_pct_by_length": 65.0, "coverage_pct_by_length_any": 65.0},
)


def _rediff(conn, data_dir, city_id, walk_id, run_date, fc, *, spacing_m=15.0):
    return compute_and_record_walk_diff(
        conn,
        data_dir=data_dir,
        city_id=city_id,
        walk_id=walk_id,
        run_date=run_date,
        provider="gsv",
        network_type="drive",
        spacing_m=spacing_m,
        match_dist_m=25.0,
        fc_new=fc,
    )


def _diffed_pair(conn, data_dir):
    """Walks at D0 and D2 whose diff has changes and wrote a detail file."""
    city_id = _register_city(conn)
    from_walk_id, _ = _register_walk(conn, data_dir, city_id, D0, OLD_FC)
    walk_id, _ = _register_walk(conn, data_dir, city_id, D2, NEW_FC)
    change = _rediff(conn, data_dir, city_id, walk_id, D2, NEW_FC)
    detail_path = os.path.join(data_dir, change["diff_file"])
    assert os.path.exists(detail_path)
    return city_id, from_walk_id, walk_id, detail_path


def _read_detail(path):
    with gzip.open(path, "rt") as fh:
        return pd.read_csv(fh)


def _deterministic_detail(data_dir, city_id, from_date, to_date):
    name = generate_streetwalk_diff_filename(city_id, from_date.isoformat(), to_date.isoformat())
    return os.path.join(data_dir, name)


def test_recollection_at_new_spacing_removes_the_stale_detail_file(conn, data_dir):
    """The issue's sharpest reproduction, needing no data change: re-collect
    the same date at a different --spacing. The frame gate skips the diff,
    and the file the cleared row pointed at must not survive it."""
    city_id, _, walk_id, detail_path = _diffed_pair(conn, data_dir)
    _register_walk(conn, data_dir, city_id, D2, NEW_FC, spacing_m=30.0)
    assert _rediff(conn, data_dir, city_id, walk_id, D2, NEW_FC, spacing_m=30.0) is None
    assert _walk_diff_rows(conn) == []
    assert not os.path.exists(detail_path)


def test_rediff_to_no_changes_removes_the_file_and_records_null(conn, data_dir):
    city_id, _, walk_id, detail_path = _diffed_pair(conn, data_dir)
    _register_walk(conn, data_dir, city_id, D2, OLD_FC)  # re-collected: now identical
    change = _rediff(conn, data_dir, city_id, walk_id, D2, OLD_FC)
    assert change is not None
    assert change["diff_file"] is None
    rows = _walk_diff_rows(conn)
    assert len(rows) == 1
    assert rows[0]["detail_filename"] is None
    assert not os.path.exists(detail_path)


def test_rediff_that_still_has_changes_rewrites_the_file(conn, data_dir):
    """The happy path keeps a file, and it is THIS diff's file: asserted on
    content, since the old file and a fresh one both 'exist'."""
    city_id, _, walk_id, detail_path = _diffed_pair(conn, data_dir)
    assert _read_detail(detail_path)["new_coverage_fraction"].tolist() == [0.8]
    newer = _fc(
        [_edge("1-2", fraction=0.3), _edge("2-3", fraction=0.5)],
        totals={"coverage_pct_by_length": 40.0, "coverage_pct_by_length_any": 40.0},
    )
    _register_walk(conn, data_dir, city_id, D2, newer)
    change = _rediff(conn, data_dir, city_id, walk_id, D2, newer)
    assert os.path.join(data_dir, change["diff_file"]) == detail_path
    assert _read_detail(detail_path)["new_coverage_fraction"].tolist() == [0.3]
    assert _walk_diff_rows(conn)[0]["detail_filename"] == change["diff_file"]


def test_has_changes_overwrites_an_unreferenced_file_at_the_same_name(conn, data_dir):
    """No row points at the stale file here, so the up-front removal cannot
    reach it: only the write itself replaces it, and it must truncate."""
    city_id = _register_city(conn)
    _register_walk(conn, data_dir, city_id, D0, OLD_FC)
    walk_id, _ = _register_walk(conn, data_dir, city_id, D2, NEW_FC)
    stale = _deterministic_detail(data_dir, city_id, D0, D2)
    with gzip.open(stale, "wt") as fh:
        fh.write("stale,content\nfrom,before\n")
    _rediff(conn, data_dir, city_id, walk_id, D2, NEW_FC)
    back = _read_detail(stale)
    assert list(back.columns) == DETAIL_COLUMNS
    assert back["edge_id"].tolist() == ["1-2"]


def test_no_changes_heals_an_orphan_at_the_deterministic_name(conn, data_dir):
    """A file stranded before #265 has no row pointing at it, so only the
    no-changes branch's removal by name can reach it."""
    city_id = _register_city(conn)
    _register_walk(conn, data_dir, city_id, D0, OLD_FC)
    walk_id, _ = _register_walk(conn, data_dir, city_id, D2, OLD_FC)
    orphan = _deterministic_detail(data_dir, city_id, D0, D2)
    with open(orphan, "w") as fh:
        fh.write("x")
    assert _rediff(conn, data_dir, city_id, walk_id, D2, OLD_FC)["diff_file"] is None
    assert not os.path.exists(orphan)


def test_no_previous_walk_removes_the_file_the_cleared_row_pointed_at(conn, data_dir):
    """'No previous walk' returns before any from-date exists, so no name can
    be derived there; only the cleared row's own pointer reaches the file.
    The predecessor is moved out of the series in place, which leaves the
    stale diff row (and its foreign key) exactly as the first diff wrote it."""
    city_id, from_walk_id, walk_id, detail_path = _diffed_pair(conn, data_dir)
    conn.execute(
        "UPDATE street_walks SET network_type = 'all_public' WHERE walk_id = ?", (from_walk_id,)
    )
    conn.commit()
    assert _rediff(conn, data_dir, city_id, walk_id, D2, NEW_FC) is None
    assert _walk_diff_rows(conn) == []
    assert not os.path.exists(detail_path)


def test_changed_predecessor_removes_the_old_rows_file_not_todays_name(conn, data_dir):
    """A walk backfilled between the pair makes it the new predecessor, so a
    name re-derived from TODAY's predecessor (D1->D2) is not the file the old
    row named (D0->D2). Removing by the row's pointer is what reaches it."""
    city_id, _, walk_id, detail_path = _diffed_pair(conn, data_dir)
    _register_walk(conn, data_dir, city_id, D1, NEW_FC)
    change = _rediff(conn, data_dir, city_id, walk_id, D2, NEW_FC)
    assert change["from"] == D1.isoformat()
    assert change["diff_file"] is None  # D1 and D2 are identical
    assert not os.path.exists(detail_path)
    assert not os.path.exists(_deterministic_detail(data_dir, city_id, D1, D2))


def test_a_missing_detail_file_at_delete_time_is_tolerated(conn, data_dir, caplog):
    """The row points at a file that is already gone: nothing raises, nothing
    is reported as a failure, and the skip still clears the row."""
    city_id, _, walk_id, detail_path = _diffed_pair(conn, data_dir)
    os.remove(detail_path)
    _register_walk(conn, data_dir, city_id, D2, NEW_FC, spacing_m=30.0)
    with caplog.at_level("WARNING"):
        assert _rediff(conn, data_dir, city_id, walk_id, D2, NEW_FC, spacing_m=30.0) is None
    assert _walk_diff_rows(conn) == []
    assert "Could not remove" not in caplog.text


def test_an_unlink_oserror_is_logged_and_the_diff_still_recorded(
    conn, data_dir, caplog, monkeypatch
):
    """A failed unlink must never sink a paid-for crawl's diff: the warning is
    logged, the file stays (and is the sweep's to find), the row is written."""
    city_id, _, walk_id, detail_path = _diffed_pair(conn, data_dir)
    _register_walk(conn, data_dir, city_id, D2, OLD_FC)

    def refuse(path):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(fileutils.os, "remove", refuse)
    with caplog.at_level("WARNING"):
        change = _rediff(conn, data_dir, city_id, walk_id, D2, OLD_FC)
    assert change is not None
    assert change["diff_file"] is None
    rows = _walk_diff_rows(conn)
    assert len(rows) == 1
    assert rows[0]["detail_filename"] is None
    assert os.path.exists(detail_path)
    assert "Could not remove stale diff detail" in caplog.text


# ── remove_stale_diff_detail, the one remover both diff families share ─────


def test_remover_reports_a_missing_file_quietly(data_dir, caplog):
    with caplog.at_level("WARNING"):
        assert (
            remove_stale_diff_detail(data_dir, "absent_diff_2026-04-01_to_2026-07-01.csv.gz")
            is False
        )
        assert remove_stale_diff_detail(data_dir, None) is False
    assert caplog.text == ""


def test_remover_refuses_a_name_with_a_path_component(tmp_path, data_dir):
    victim = tmp_path / "victim.csv.gz"
    victim.write_text("x")
    assert remove_stale_diff_detail(data_dir, "../victim.csv.gz") is False
    assert victim.exists()
