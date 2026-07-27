"""End-to-end tests for the MAPILLARY arm of the road-walk collector (#99).

Mapillary has no per-point metadata endpoint: a road walk reads the z14 tile
census once and joins it onto the same on-street sample points the GSV walk
uses. These tests drive the real `collect.run_collect` flow with the OSM fetch
and the tile census both served from memory, and assert the things that make
that join trustworthy:

  * one row per sample location, with the #116 status vocabulary
    (OK / FLAT_ONLY / ZERO_RESULTS) and the match-distance guard applied;
  * flat imagery raising the ANY-imagery number without touching the 360° one;
  * requests metered under `mapillary_streets`, never `mapillary` or
    `gsv_streets`;
  * cost independent of sample spacing (the whole point of the tile census).
"""

import gzip
import json
import os
from datetime import date

import geopandas as gpd
from shapely.geometry import LineString

from streetscape_metadata_tracker import db
from streetscape_street_analyzer import collect
from streetscape_street_analyzer import collect_mapillary as cm

# ~222 m north-south edge, plus a short spur — same geometry as the GSV test.
LONG_EDGE = LineString([(-121.30, 44.05), (-121.30, 44.052)])
SHORT_EDGE = LineString([(-121.30, 44.052), (-121.30, 44.0525)])
CITY_QUERY = "Bend, Oregon, United States"
CITY_ID = "bend--oregon--united-states"
RUN_DATE = "2026-07-08"


def _edges():
    return gpd.GeoDataFrame(
        {"edge_id": ["1_2", "2_3"], "highway": ["residential", "service"], "length": [222.0, 55.0]},
        geometry=[LONG_EDGE, SHORT_EDGE],
        crs="EPSG:4326",
    )


def _image(image_id, lat, lon, *, is_pano=True, captured_at_ms=1655000000000, creator_id=42):
    """One decoded Mapillary tile feature (decode_image_features' shape)."""
    return {
        "id": image_id,
        "lat": lat,
        "lon": lon,
        "is_pano": is_pano,
        "captured_at_ms": captured_at_ms,
        "creator_id": creator_id,
        "organization_id": None,
        "sequence_id": "seq-1",
        "on_foot": False,
        "quality_score": 0.9,
        "compass_angle": 12.5,
    }


def _setup(tmp_path, monkeypatch, images):
    """Data dir + catalog with one city; edges and the tile census served locally."""
    data_dir = str(tmp_path)
    conn = db.connect(db.get_default_db_path(data_dir))
    db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.30,
        grid_width_m=200,
        grid_height_m=200,
        step_m=20,
    )
    conn.close()
    monkeypatch.setattr(collect, "fetch_street_edges", lambda *a, **k: _edges())

    calls = {"n": 0}

    async def fake_fetch_images(city_name, bbox, access_token, **kwargs):
        assert access_token == "MLY|TESTTOKEN"  # the streets token, not the grid one
        calls["n"] += 1
        return {"images": images, "api_requests": 7, "tiles": 7, "raw_feature_count": len(images)}

    monkeypatch.setattr(cm, "fetch_city_images_async", fake_fetch_images)
    monkeypatch.setenv("MAPILLARY_STREETS_ACCESS_TOKEN", "MLY|TESTTOKEN")
    return data_dir, calls


def _args(data_dir, **overrides):
    argv = [
        CITY_QUERY,
        "--provider",
        "mapillary",
        "--data-dir",
        data_dir,
        "--run-date",
        RUN_DATE,
        "--spacing",
        "15",
    ]
    for k, v in overrides.items():
        argv += [f"--{k}", str(v)] if v is not True else [f"--{k}"]
    return collect.build_parser().parse_args(argv)


def _coverage(data_dir, spacing=15):
    name = (
        f"{CITY_ID}_width_200_height_200_step_20_streetwalk_sp{spacing}_"
        f"{RUN_DATE}_coverage.json.gz"
    )
    with gzip.open(os.path.join(data_dir, name), "rt") as fh:
        return json.load(fh)


# --- The join ---------------------------------------------------------------


def test_panos_along_the_edge_cover_it_and_meter_only_the_streets_channel(tmp_path, monkeypatch):
    # A pano every ~0.0002° of latitude (~22 m) down the long edge: dense
    # enough that every 15 m sample finds one within the 25 m match distance.
    images = [
        _image(f"p{i}", 44.05 + i * 0.0001, -121.30) for i in range(26)
    ]
    data_dir, calls = _setup(tmp_path, monkeypatch, images)

    assert collect.run_collect(_args(data_dir)) == 0
    assert calls["n"] == 1  # the census is fetched exactly once

    totals = _coverage(data_dir)["properties"]["metadata"]["totals"]
    assert totals["coverage_pct_by_length"] > 90
    # No flat imagery in this census, so any-imagery adds nothing.
    assert totals["coverage_pct_by_length_any"] == totals["coverage_pct_by_length"]

    conn = db.connect(db.get_default_db_path(data_dir))
    walk = db.get_latest_street_walk(conn, CITY_ID, provider="mapillary")
    assert walk is not None
    assert walk["provider"] == "mapillary"
    assert walk["api_requests"] == 7  # tiles, not sample points
    d = date.fromisoformat(RUN_DATE)
    # Budget isolation: the streets channel only. The grid channels and the
    # GSV streets channel must be untouched.
    assert db.get_api_usage(conn, d, provider="mapillary_streets") == 7
    assert db.get_api_usage(conn, d, provider="mapillary") == 0
    assert db.get_api_usage(conn, d, provider="gsv_streets") == 0
    conn.close()


def test_imagery_beyond_the_match_distance_does_not_cover(tmp_path, monkeypatch):
    """The distance guard is what stops a pano on a parallel street from
    claiming this one. ~0.005° of longitude is ~400 m east of the edge."""
    images = [_image(f"p{i}", 44.05 + i * 0.0001, -121.295) for i in range(26)]
    data_dir, _ = _setup(tmp_path, monkeypatch, images)

    assert collect.run_collect(_args(data_dir)) == 0
    totals = _coverage(data_dir)["properties"]["metadata"]["totals"]
    assert totals["coverage_pct_by_length"] == 0.0
    assert totals["coverage_pct_by_length_any"] == 0.0


def test_flat_imagery_raises_any_coverage_but_not_360_coverage(tmp_path, monkeypatch):
    """Issue #116's distinction, applied to streets: flat/perspective imagery
    is imagery of the street, but it is not a 360° pano and carries no usable
    date — so it must move the any-imagery number and ONLY that one."""
    images = [
        _image(f"f{i}", 44.05 + i * 0.0001, -121.30, is_pano=False) for i in range(26)
    ]
    data_dir, _ = _setup(tmp_path, monkeypatch, images)

    assert collect.run_collect(_args(data_dir)) == 0
    totals = _coverage(data_dir)["properties"]["metadata"]["totals"]
    assert totals["coverage_pct_by_length"] == 0.0
    assert totals["coverage_pct_by_length_any"] > 90

    # And the catalog carries both numbers.
    conn = db.connect(db.get_default_db_path(data_dir))
    walk = db.get_latest_street_walk(conn, CITY_ID, provider="mapillary")
    assert walk["coverage_pct_by_length"] == 0.0
    assert walk["coverage_pct_by_length_any"] > 90
    conn.close()


def test_a_pano_wins_over_a_flat_image_at_the_same_place(tmp_path, monkeypatch):
    """When both are in range the sample is 360°-covered: the FLAT_ONLY marker
    exists only for samples with NO pano nearby."""
    images = []
    for i in range(26):
        lat = 44.05 + i * 0.0001
        images.append(_image(f"p{i}", lat, -121.30))
        images.append(_image(f"f{i}", lat, -121.30, is_pano=False))
    data_dir, _ = _setup(tmp_path, monkeypatch, images)

    assert collect.run_collect(_args(data_dir)) == 0
    totals = _coverage(data_dir)["properties"]["metadata"]["totals"]
    assert totals["coverage_pct_by_length"] == totals["coverage_pct_by_length_any"] > 90


def test_flat_only_rows_carry_no_capture_date(tmp_path, monkeypatch):
    """A FLAT_ONLY row is a presence marker; a date on it would leak flat
    timestamps into age statistics."""
    images = [_image("f1", 44.0500, -121.30, is_pano=False)]
    data_dir, _ = _setup(tmp_path, monkeypatch, images)

    assert collect.run_collect(_args(data_dir)) == 0
    from streetscape_metadata_tracker.fileutils import load_city_csv_file

    csv_name = f"{CITY_ID}_width_200_height_200_step_20_streetwalk_sp15_{RUN_DATE}.csv.gz"
    df = load_city_csv_file(os.path.join(data_dir, csv_name))
    flat_rows = df[df["status"] == "FLAT_ONLY"]
    assert len(flat_rows) >= 1
    assert flat_rows["capture_date"].isna().all()
    # Every sample location produced exactly one row.
    assert df["status"].isin(("OK", "NO_DATE", "FLAT_ONLY", "ZERO_RESULTS")).all()


def test_unusable_timestamp_becomes_no_date_not_a_bogus_year(tmp_path, monkeypatch):
    images = [_image(f"p{i}", 44.05 + i * 0.0001, -121.30, captured_at_ms=0) for i in range(26)]
    data_dir, _ = _setup(tmp_path, monkeypatch, images)

    assert collect.run_collect(_args(data_dir)) == 0
    from streetscape_metadata_tracker.fileutils import load_city_csv_file

    csv_name = f"{CITY_ID}_width_200_height_200_step_20_streetwalk_sp15_{RUN_DATE}.csv.gz"
    df = load_city_csv_file(os.path.join(data_dir, csv_name))
    assert (df["status"] == "NO_DATE").any()
    assert not (df["capture_date"].fillna("") == "1970-01-01").any()


def test_empty_census_yields_zero_coverage_not_a_crash(tmp_path, monkeypatch):
    data_dir, _ = _setup(tmp_path, monkeypatch, [])

    assert collect.run_collect(_args(data_dir)) == 0
    totals = _coverage(data_dir)["properties"]["metadata"]["totals"]
    assert totals["coverage_pct_by_length"] == 0.0
    assert totals["edges"] == 2


# --- Cost model -------------------------------------------------------------


def test_tile_cost_is_independent_of_sample_spacing(tmp_path, monkeypatch):
    """The reason Mapillary street coverage can be scheduled for every city:
    halving the spacing doubles the sample points but costs the same census.

    The two runs use different run dates because street_walks is UNIQUE on
    (city_id, provider, run_date) — a same-day re-collection replaces the row.
    """
    images = [_image(f"p{i}", 44.05 + i * 0.0001, -121.30) for i in range(26)]
    data_dir, _ = _setup(tmp_path, monkeypatch, images)

    coarse_date, fine_date = "2026-07-08", "2026-07-09"
    assert collect.run_collect(_args(data_dir, spacing=30)) == 0
    fine_args = _args(data_dir, spacing=15)
    fine_args.run_date = fine_date
    assert collect.run_collect(fine_args) == 0

    conn = db.connect(db.get_default_db_path(data_dir))
    rows = conn.execute(
        "SELECT run_date, spacing_m, sample_points, api_requests FROM street_walks"
    ).fetchall()
    by_spacing = {int(r["spacing_m"]): r for r in rows}
    assert by_spacing[15]["run_date"] == fine_date
    assert by_spacing[30]["run_date"] == coarse_date
    # Twice the samples...
    assert by_spacing[15]["sample_points"] > by_spacing[30]["sample_points"]
    # ...for the same number of requests.
    assert by_spacing[15]["api_requests"] == by_spacing[30]["api_requests"] == 7
    conn.close()


def test_estimate_reports_tiles_and_needs_no_token(tmp_path, monkeypatch, capsys):
    data_dir, calls = _setup(tmp_path, monkeypatch, [])
    monkeypatch.delenv("MAPILLARY_STREETS_ACCESS_TOKEN", raising=False)

    assert collect.run_collect(_args(data_dir, estimate=True)) == 0
    out = capsys.readouterr().out
    assert "Mapillary tile requests" in out
    assert calls["n"] == 0  # nothing fetched
    assert not os.path.exists(
        os.path.join(
            data_dir,
            f"{CITY_ID}_width_200_height_200_step_20_streetwalk_sp15_{RUN_DATE}.csv.gz",
        )
    )


# --- The pure join helper ---------------------------------------------------


def test_nearest_images_to_samples_picks_the_closest_of_each_kind():
    samples = [(44.0500, -121.3000, 0, 0)]
    near_pano = _image("p_near", 44.05001, -121.3000)
    far_pano = _image("p_far", 44.05015, -121.3000)  # ~17 m away
    near_flat = _image("f_near", 44.050005, -121.3000, is_pano=False)
    panos, flats = cm.nearest_images_to_samples(
        samples, [far_pano, near_pano, near_flat], 25.0
    )
    assert panos[0]["id"] == "p_near"
    assert flats[0]["id"] == "f_near"


def test_nearest_images_to_samples_handles_empty_inputs():
    assert cm.nearest_images_to_samples([], [_image("p", 44.05, -121.3)], 25.0) == ({}, {})
    assert cm.nearest_images_to_samples([(44.05, -121.3, 0, 0)], [], 25.0) == ({}, {})
