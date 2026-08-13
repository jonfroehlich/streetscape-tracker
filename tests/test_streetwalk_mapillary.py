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
from streetscape_metadata_tracker import download_gsv as dg
from streetscape_metadata_tracker.download_mapillary import (
    DEFAULT_TILE_REQUESTS_PER_MINUTE,
    records_to_census,
)
from streetscape_metadata_tracker.naming import (
    generate_streetwalk_filename,
    streetwalk_coverage_filename,
)
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
        return {
            "census": records_to_census(images),
            "api_requests": 7,
            "tiles": 7,
            "raw_feature_count": len(images),
        }

    monkeypatch.setattr(cm, "fetch_city_images_async", fake_fetch_images)
    monkeypatch.setenv("MAPILLARY_STREETS_ACCESS_TOKEN", "MLY|TESTTOKEN")
    return data_dir, calls


def _args(data_dir, provider="mapillary", **overrides):
    argv = [
        CITY_QUERY,
        "--provider",
        provider,
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


def _csv_name(spacing=15, provider="mapillary", run_date=RUN_DATE):
    """Snapshot filename, via the real generator so the tests can't drift from
    the naming contract (the provider token is what keeps the two channels'
    artifacts from colliding)."""
    return (
        generate_streetwalk_filename(
            CITY_ID, 200, 200, 20, spacing, date.fromisoformat(run_date), provider=provider
        )
        + ".csv.gz"
    )


def _coverage(data_dir, spacing=15, provider="mapillary"):
    name = streetwalk_coverage_filename(_csv_name(spacing, provider))
    with gzip.open(os.path.join(data_dir, name), "rt") as fh:
        return json.load(fh)


# --- The join ---------------------------------------------------------------


def test_panos_along_the_edge_cover_it_and_meter_only_the_streets_channel(tmp_path, monkeypatch):
    # A pano every ~0.0002° of latitude (~22 m) down the long edge: dense
    # enough that every 15 m sample finds one within the 25 m match distance.
    images = [_image(f"p{i}", 44.05 + i * 0.0001, -121.30) for i in range(26)]
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
    images = [_image(f"f{i}", 44.05 + i * 0.0001, -121.30, is_pano=False) for i in range(26)]
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

    df = load_city_csv_file(os.path.join(data_dir, _csv_name()))
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

    df = load_city_csv_file(os.path.join(data_dir, _csv_name()))
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


def test_the_road_walk_forwards_the_tile_pace_to_the_census(tmp_path, monkeypatch):
    """The grid run and the road walk share fetch_city_images_async and share
    the per-IP tile limit (issue #198), so the walk must not be the unpaced way
    in. Driven through the real CLI: a signature check would not notice
    collect.py dropping the argument between argparse and the census."""
    data_dir, _ = _setup(tmp_path, monkeypatch, [_image("i-1", 44.0500, -121.3000)])
    census = cm.fetch_city_images_async  # the local stand-in installed by _setup
    seen = {}

    async def capture(city_name, bbox, access_token, **kwargs):
        seen.update(kwargs)
        return await census(city_name, bbox, access_token, **kwargs)

    monkeypatch.setattr(cm, "fetch_city_images_async", capture)

    args = _args(data_dir, **{"mapillary-max-requests-per-minute": 42})
    assert collect.run_collect(args) == 0
    assert seen["max_requests_per_minute"] == 42


def test_an_unset_road_walk_pace_still_paces(tmp_path, monkeypatch):
    """Unset must mean the collector's own conservative default, never unpaced
    and never the gsv_streets figure."""
    data_dir, _ = _setup(tmp_path, monkeypatch, [_image("i-1", 44.0500, -121.3000)])
    census = cm.fetch_city_images_async
    seen = {}

    async def capture(city_name, bbox, access_token, **kwargs):
        seen.update(kwargs)
        return await census(city_name, bbox, access_token, **kwargs)

    monkeypatch.setattr(cm, "fetch_city_images_async", capture)

    assert collect.run_collect(_args(data_dir)) == 0
    assert seen["max_requests_per_minute"] == DEFAULT_TILE_REQUESTS_PER_MINUTE


def test_estimate_reports_tiles_and_needs_no_token(tmp_path, monkeypatch, capsys):
    data_dir, calls = _setup(tmp_path, monkeypatch, [])
    monkeypatch.delenv("MAPILLARY_STREETS_ACCESS_TOKEN", raising=False)

    assert collect.run_collect(_args(data_dir, estimate=True)) == 0
    out = capsys.readouterr().out
    assert "Mapillary tile requests" in out
    assert calls["n"] == 0  # nothing fetched
    assert not os.path.exists(os.path.join(data_dir, _csv_name()))


# --- Both channels on one night ---------------------------------------------


def test_both_providers_can_walk_the_same_city_on_the_same_night(tmp_path, monkeypatch):
    """
    The scheduler runs a city's gsv_streets and mapillary_streets channels
    back-to-back with ONE run_date, so the two collections must not collide.

    They used to: the snapshot filename carried no provider token, so the
    second collection found the first's artifact already on disk, hit the
    immutable-per-date guard, and returned 0 — a *success* as far as the
    scheduler is concerned, which advanced last_success_at and meant the
    Mapillary arm never ran at all. Assert both artifacts and both catalog rows
    exist, and that the second run actually collected rather than skipping.
    """
    images = [_image(f"p{i}", 44.05 + i * 0.0001, -121.30) for i in range(26)]
    data_dir, census_calls = _setup(tmp_path, monkeypatch, images)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    gsv_calls = {"n": 0}

    async def fake_gsv(lat, lon, api_key, session, timeout, limiter=None):
        assert api_key == "TESTKEY"  # the gsv_streets key, not the grid one
        gsv_calls["n"] += 1
        return {
            "status": "OK",
            "location": {"lat": lat, "lng": lon},
            "pano_id": f"pano_{lat:.6f}_{lon:.6f}",
            "copyright": "© Google",
            "date": "2022-06",
        }

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", fake_gsv)

    # Same city, same run date, in the order enabled_providers() dispatches.
    assert collect.run_collect(_args(data_dir, provider="gsv")) == 0
    assert collect.run_collect(_args(data_dir, provider="mapillary")) == 0

    # Neither channel short-circuited: each actually reached its imagery source.
    assert gsv_calls["n"] > 0
    assert census_calls["n"] == 1

    for provider in ("gsv", "mapillary"):
        csv_path = os.path.join(data_dir, _csv_name(provider=provider))
        assert os.path.exists(csv_path), f"{provider} snapshot missing"
        assert os.path.exists(
            os.path.join(data_dir, streetwalk_coverage_filename(_csv_name(provider=provider)))
        ), f"{provider} coverage artifact missing"
    assert _csv_name(provider="gsv") != _csv_name(provider="mapillary")

    conn = db.connect(db.get_default_db_path(data_dir))
    rows = conn.execute(
        "SELECT provider, csv_filename, coverage_filename FROM street_walks ORDER BY provider"
    ).fetchall()
    assert [r["provider"] for r in rows] == ["gsv", "mapillary"]
    # Each catalog row points at its own artifact, not a shared one.
    assert rows[0]["csv_filename"] != rows[1]["csv_filename"]
    assert rows[0]["coverage_filename"] != rows[1]["coverage_filename"]

    # Isolated ledgers: each channel metered only its own spend.
    d = date.fromisoformat(RUN_DATE)
    assert db.get_api_usage(conn, d, provider="gsv_streets") == gsv_calls["n"]
    assert db.get_api_usage(conn, d, provider="mapillary_streets") == 7
    assert db.get_api_usage(conn, d, provider="gsv") == 0
    assert db.get_api_usage(conn, d, provider="mapillary") == 0
    conn.close()


# --- The pure join helper ---------------------------------------------------


def _ids(census, positions):
    """Matched image ids per sample, with None where nothing was in range."""
    ids = census["id"].to_numpy(dtype=object)
    return [None if p < 0 else ids[p] for p in positions]


def test_nearest_images_to_samples_picks_the_closest_of_each_kind():
    samples = [(44.0500, -121.3000, 0, 0)]
    near_pano = _image("p_near", 44.05001, -121.3000)
    far_pano = _image("p_far", 44.05015, -121.3000)  # ~17 m away
    near_flat = _image("f_near", 44.050005, -121.3000, is_pano=False)
    census = records_to_census([far_pano, near_pano, near_flat])
    panos, flats = cm.nearest_images_to_samples(samples, census, 25.0)
    assert _ids(census, panos) == ["p_near"]
    assert _ids(census, flats) == ["f_near"]


def test_chunked_join_matches_a_single_shot_join(monkeypatch):
    """
    The join runs the samples through sjoin_nearest in blocks so a dense city's
    census can't materialize one enormous match set. Chunking is a memory knob
    only: the same samples must map to the same images at any block size,
    including a block boundary that falls mid-run.
    """
    samples = [(44.05 + i * 0.0001, -121.30, i, 0) for i in range(25)]
    # Imagery near only some samples, so the result has genuine gaps to preserve.
    images = [_image(f"p{i}", 44.05 + i * 0.0001, -121.30) for i in range(0, 25, 3)]
    images += [
        _image(f"f{i}", 44.05 + i * 0.0001, -121.3000, is_pano=False) for i in range(1, 25, 4)
    ]

    # A tight match distance against ~11 m sample spacing, so most samples have
    # nothing in range and the result has real gaps to preserve.
    match_dist = 5.0
    census = records_to_census(images)

    monkeypatch.setattr(cm, "_JOIN_CHUNK_SIZE", 10_000)  # one block
    whole_panos, whole_flats = cm.nearest_images_to_samples(samples, census, match_dist)

    monkeypatch.setattr(cm, "_JOIN_CHUNK_SIZE", 7)  # boundaries at 7, 14, 21
    chunked_panos, chunked_flats = cm.nearest_images_to_samples(samples, census, match_dist)

    assert _ids(census, chunked_panos) == _ids(census, whole_panos)
    assert _ids(census, chunked_flats) == _ids(census, whole_flats)
    matched = (whole_panos >= 0).sum()
    assert matched, "fixture should match at least some samples"
    # Samples out of range stay unmatched rather than being filled in.
    assert matched < len(samples)


def test_nearest_images_to_samples_handles_empty_inputs():
    one_image = records_to_census([_image("p", 44.05, -121.3)])
    no_samples = cm.nearest_images_to_samples([], one_image, 25.0)
    assert all(len(m) == 0 for m in no_samples)
    # An empty census leaves every sample unmatched rather than raising.
    no_images = cm.nearest_images_to_samples([(44.05, -121.3, 0, 0)], records_to_census([]), 25.0)
    assert all(list(m) == [-1] for m in no_images)
