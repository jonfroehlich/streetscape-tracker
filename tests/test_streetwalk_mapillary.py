"""End-to-end tests for the MAPILLARY arm of the road-walk collector (#99).

Mapillary has no per-point metadata endpoint: a road walk reads the z14 tile
census once and joins it onto the same on-street sample points the GSV walk
uses. These tests drive the real `collect.run_collect` flow with the OSM fetch
and the tile census both served from memory, and assert the things that make
that join trustworthy:

  * one row per sample location, with the #116 status vocabulary
    (OK / FLAT_ONLY / ZERO_RESULTS) and the match-distance guard applied;
  * flat imagery raising the ANY-imagery number without touching the 360° one;
  * undated imagery (NO_DATE) covering the street while ageing nothing (#257),
    which is the arm the GSV-only unit tests structurally cannot reach;
  * a tile the fetch never got back publishing REQUEST_FAILED rather than
    ZERO_RESULTS, so an unmeasured hole is not republished as measured
    emptiness (#259) -- and only for the samples nothing matched;
  * requests metered under `mapillary_streets`, never `mapillary` or
    `gsv_streets`;
  * cost independent of sample spacing (the whole point of the tile census).
"""

import gzip
import json
import logging
import os
from datetime import date

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker import download_gsv as dg
from streetscape_metadata_tracker.checkpointing import (
    census_cache_path_for,
    checkpoint_path_for,
)
from streetscape_metadata_tracker.download_common import grid_bbox
from streetscape_metadata_tracker.download_mapillary import (
    DEFAULT_TILE_REQUESTS_PER_MINUTE,
    TILE_ZOOM,
    _points_in_tiles,
    lonlat_to_tile_frac,
    records_to_census,
    tile_frac_to_lonlat,
)
from streetscape_metadata_tracker.naming import (
    generate_streetwalk_filename,
    streetwalk_coverage_filename,
)
from streetscape_street_analyzer import census_walk, collect
from streetscape_street_analyzer import collect_mapillary as cm
from tests.conftest import stamp_census_cache

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


def _setup(
    tmp_path,
    monkeypatch,
    images,
    *,
    api_requests=7,
    api_requests_total=None,
    census_fetched_by=None,
    census_fetched_at=None,
    census_reused=None,
    failed_tiles=None,
    edges=None,
):
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
    walked = _edges() if edges is None else edges
    monkeypatch.setattr(collect, "fetch_street_edges", lambda *a, **k: walked)

    calls = {"n": 0}

    async def fake_fetch_images(city_name, bbox, access_token, **kwargs):
        assert access_token == "MLY|TESTTOKEN"  # the streets token, not the grid one
        calls["n"] += 1
        calls["checkpoint_path"] = kwargs.get("checkpoint_path")
        calls["checkpoint_channel"] = kwargs.get("checkpoint_channel")
        calls["checkpoint_variant"] = kwargs.get("checkpoint_variant")
        policy = kwargs.get("census_cache")
        calls["cache_path"] = policy.path if policy else None
        calls["reuse_census"] = policy.reuse if policy else None
        calls["run_date"] = policy.run_date if policy else None
        return {
            "census": records_to_census(images),
            # Per-process spend and the crawl's cumulative spend are different
            # numbers by design (#256): the first feeds the additive daily
            # ledger, the second the street_walks row. Equal here because this
            # fake never resumes; test_streetwalk_mapillary_resume drives them
            # apart.
            "api_requests": api_requests,
            "api_requests_total": (
                api_requests if api_requests_total is None else api_requests_total
            ),
            "checkpoint_path": kwargs.get("checkpoint_path"),
            # Tiles the fetch never got back. The real fetch refuses to
            # finalize past MAX_FAILED_TILE_FRACTION, so a live list is always
            # a small remainder; these tests hand it one on purpose (#259).
            "failed_tiles": list(failed_tiles or []),
            "tiles": 7,
            "raw_feature_count": len(images),
            # Census provenance (#290). Defaults mimic an ordinary fresh fetch:
            # this channel paid, and nothing was reused.
            "census_fetched_by": census_fetched_by or kwargs.get("checkpoint_channel"),
            "census_fetched_at": census_fetched_at,
            "census_reused": (
                bool(census_fetched_by and census_fetched_by != kwargs.get("checkpoint_channel"))
                if census_reused is None
                else census_reused
            ),
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
    """An unusable contributor timestamp becomes NO_DATE rather than 1970 —
    and, since #257, that pano still COVERS the street it is standing on.

    This is the end-to-end half of the PRESENT-vocabulary contract that
    `tests/test_streetwalk_coverage.py` pins on `compute_streetwalk_coverage`
    directly. Those unit tests drive the scorer with `provider="gsv"`, the one
    provider whose NO_DATE population is empty in practice, so they cannot see
    a walk that produces NO_DATE rows for real. This one can: every image here
    is undated, so the whole artifact is the undated case, and it reads 0.0%
    against the pre-#257 `status == "OK"` filter and 100.0% after.
    """
    images = [_image(f"p{i}", 44.05 + i * 0.0001, -121.30, captured_at_ms=0) for i in range(26)]
    data_dir, _ = _setup(tmp_path, monkeypatch, images)

    assert collect.run_collect(_args(data_dir)) == 0
    from streetscape_metadata_tracker.fileutils import load_city_csv_file

    df = load_city_csv_file(os.path.join(data_dir, _csv_name()))
    assert (df["status"] == "NO_DATE").any()
    assert not (df["capture_date"].fillna("") == "1970-01-01").any()

    # Undated panos on the street cover it, in the 360° number as well as _any.
    totals = _coverage(data_dir)["properties"]["metadata"]["totals"]
    assert totals["coverage_pct_by_length"] == 100.0
    assert totals["coverage_pct_by_length_any"] == 100.0
    # ...and age nothing, with the denominator recording exactly why: every
    # covered sample here is undated, so there is no age to take a median of.
    assert totals["median_covered_age_years"] is None
    assert totals["covered_samples"] > 0
    assert totals["covered_samples_dated"] == 0
    assert totals["dated_pct_of_covered"] == 0.0

    # The catalog carries the corrected number too, not just the artifact —
    # `streets.html` and the walk diffs both read the row, not the GeoJSON.
    conn = db.connect(db.get_default_db_path(data_dir))
    walk = db.get_latest_street_walk(conn, CITY_ID, provider="mapillary")
    assert walk["coverage_pct_by_length"] == 100.0
    assert walk["median_covered_age_years"] is None
    conn.close()


def test_empty_census_yields_zero_coverage_not_a_crash(tmp_path, monkeypatch):
    data_dir, _ = _setup(tmp_path, monkeypatch, [])

    assert collect.run_collect(_args(data_dir)) == 0
    totals = _coverage(data_dir)["properties"]["metadata"]["totals"]
    assert totals["coverage_pct_by_length"] == 0.0
    assert totals["edges"] == 2


# --- An unswept sample is not an empty one (issue #259) ---------------------

# The city's own edges sit wholly inside ONE z14 tile, so failing that tile
# would mask every sample or none and could not tell the two apart. These tests
# walk an edge laid ACROSS a real z14 seam instead -- the northern boundary of
# the tile the city sits in -- and fail only the tile on one side, so a single
# run carries both kinds at once. The seam is derived from the shipped tiling
# rather than hardcoded, so a zoom change moves the geometry with it instead of
# quietly making these tests vacuous.
CITY_TILE = tuple(int(v) for v in lonlat_to_tile_frac(-121.30, 44.05, TILE_ZOOM))
NORTH_TILE = (CITY_TILE[0], CITY_TILE[1] - 1)
# y grows southward, so a tile's own y index names its NORTHERN edge.
SEAM_LAT = tile_frac_to_lonlat(CITY_TILE[0] + 0.5, CITY_TILE[1], TILE_ZOOM)[1]


def _seam_edges():
    """One ~440 m north-south edge straddling the seam: half in each tile."""
    return gpd.GeoDataFrame(
        {"edge_id": ["s1"], "highway": ["residential"], "length": [440.0]},
        geometry=[LineString([(-121.30, SEAM_LAT - 0.002), (-121.30, SEAM_LAT + 0.002)])],
        crs="EPSG:4326",
    )


def _statuses(data_dir, spacing=15):
    """(status, query_lat) for every published row of the walk snapshot."""
    with gzip.open(os.path.join(data_dir, _csv_name(spacing)), "rt") as fh:
        header = fh.readline().rstrip("\n").split(",")
        si, li = header.index("status"), header.index("query_lat")
        return [(r.split(",")[si], float(r.split(",")[li])) for r in fh.read().splitlines() if r]


def test_a_failed_tile_publishes_request_failed_not_zero_results(tmp_path, monkeypatch):
    """
    A tile the fetch never got back leaves its samples UNKNOWN.

    Recording an unswept sample as ZERO_RESULTS publishes an absence we never
    observed into an immutable dated snapshot, and no later reader can tell it
    from a measured one. What that buys is a legible ROW and nothing more --
    REQUEST_FAILED sits in the same coverage denominator ZERO_RESULTS did, so
    no published percentage and no walk-diff counter moves either way, which is
    the same choice the grid path makes. The Mapillary GRID run has masked its
    holes this way since #168 and the KartaView walk since #258; this walk was
    the one that did not, which is #259.
    """
    data_dir, _ = _setup(tmp_path, monkeypatch, [], failed_tiles=[NORTH_TILE], edges=_seam_edges())

    assert collect.run_collect(_args(data_dir)) == 0
    rows = _statuses(data_dir)
    kinds = {s for s, _ in rows}
    assert kinds == {"REQUEST_FAILED", "ZERO_RESULTS"}, (
        f"the failed tile's half must read REQUEST_FAILED and the swept half "
        f"ZERO_RESULTS, got {kinds}"
    )
    # ...and the split follows the tile boundary rather than some other
    # accident: the unknown rows are exactly the ones north of the seam.
    assert all((s == "REQUEST_FAILED") == (lat > SEAM_LAT) for s, lat in rows)


def test_a_sample_matched_inside_a_failed_tile_stays_matched(tmp_path, monkeypatch):
    """
    The mask speaks only for samples that matched NOTHING.

    A sample that found imagery within the match distance was measured by
    construction, so overwriting it with REQUEST_FAILED would erase real
    coverage. The imagery here sits inside the failed tile's own bounds, which
    is exactly the case a mask applied before the join would get wrong.
    """
    # A pano every ~11 m up the northern (failed) half, dense enough that every
    # 15 m sample there is within the 25 m match distance of one.
    images = [_image(f"p{i}", SEAM_LAT + 0.0001 * i, -121.30) for i in range(1, 21)]
    data_dir, _ = _setup(
        tmp_path, monkeypatch, images, failed_tiles=[NORTH_TILE], edges=_seam_edges()
    )

    assert collect.run_collect(_args(data_dir)) == 0
    rows = _statuses(data_dir)
    kinds = {s for s, _ in rows}
    north = np.array([lat for _, lat in rows if lat > SEAM_LAT])
    # Stated rather than assumed, because it is the whole premise: these
    # samples really do lie inside the tile that failed, so a mask applied
    # before the join -- or to every sample rather than the unmatched ones --
    # would turn each of them into a hole.
    assert _points_in_tiles(north, np.full(north.shape, -121.30), [NORTH_TILE]).all()
    assert {s for s, lat in rows if lat > SEAM_LAT} == {"OK"}, (
        "a matched sample must stay matched even inside a failed tile's bounds"
    )
    # So this run publishes no unknown row at all, despite a failed tile being
    # in play: every sample inside it matched, and the mask speaks only for the
    # ones that did not. The southern half was swept and is mostly empty, so it
    # still reads as measured emptiness rather than as a hole.
    assert "REQUEST_FAILED" not in kinds
    assert "ZERO_RESULTS" in kinds


def test_a_clean_fetch_still_publishes_zero_results(tmp_path, monkeypatch):
    """
    The other half of the pair: with no failed tiles, an empty census is a
    MEASURED absence and must read ZERO_RESULTS.

    Without this, masking everything would satisfy the test above while
    destroying the ordinary case -- the coverage denominator depends on telling
    observed emptiness from unobserved ground.
    """
    data_dir, _ = _setup(tmp_path, monkeypatch, [], failed_tiles=[], edges=_seam_edges())

    assert collect.run_collect(_args(data_dir)) == 0
    assert {s for s, _ in _statuses(data_dir)} == {"ZERO_RESULTS"}


def test_a_degraded_walk_says_so_in_its_own_log(tmp_path, monkeypatch, caplog):
    """
    A walk that publishes holes must SAY so in the per-attempt log.

    That log is the tier the `[alerts]` mail tails, and it is the only place an
    operator learns an artifact is partly unmeasured -- nothing in the catalog
    row or the coverage GeoJSON records it. The reuse path (#290) makes this
    sharper than it looks: a walk that reads its census from the cache inherits
    the crawl's failed tiles for zero requests, so no fetch-side "N/M tiles
    failed" warning fires at all and this is the ONLY evidence there is.
    """
    data_dir, _ = _setup(tmp_path, monkeypatch, [], failed_tiles=[NORTH_TILE], edges=_seam_edges())

    with caplog.at_level(logging.WARNING):
        assert collect.run_collect(_args(data_dir)) == 0
    degraded = sum(1 for s, _ in _statuses(data_dir) if s == "REQUEST_FAILED")
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        f"{degraded:,} walk samples" in m and "1 undownloaded tile(s)" in m for m in warnings
    ), f"the degraded sample count and what failed must both be in the log, got {warnings}"


def test_a_failed_tile_covering_no_sample_claims_no_degradation(tmp_path, monkeypatch, caplog):
    """
    The other side of that warning: it fires on DEGRADATION, not on a failure.

    A failed tile can legitimately cover no sample at all -- one over water, or
    a margin tile whose on-street points a neighbour already answered -- and a
    WARNING asserting damage that did not happen goes straight into the tail
    the alert mail ships, which is where a real one has to stand out.
    """
    far_away = (NORTH_TILE[0] + 40, NORTH_TILE[1] + 40)
    data_dir, _ = _setup(tmp_path, monkeypatch, [], failed_tiles=[far_away], edges=_seam_edges())

    with caplog.at_level(logging.WARNING):
        assert collect.run_collect(_args(data_dir)) == 0
    assert {s for s, _ in _statuses(data_dir)} == {"ZERO_RESULTS"}
    assert not [r for r in caplog.records if "walk samples fall in" in r.message]


def test_a_mask_without_a_description_is_refused(tmp_path, monkeypatch):
    """
    The desc is what makes the warning above actionable, so the seam refuses a
    mask without one rather than logging an unattributed count -- the same
    contract `census.write_census_grid_run` enforces for the grid tail.
    """
    with pytest.raises(ValueError, match="unmeasured_desc"):
        cm.build_streetwalk_rows(
            [(44.05, -121.30, 0, 0)],
            records_to_census([]),
            25.0,
            "2026-07-08T00:00:00+00:00",
            unmeasured_mask=lambda lats, lons: np.zeros(len(lats), dtype=bool),
        )


def test_a_wholly_unmeasured_walk_is_rejected_rather_than_cataloged(tmp_path, monkeypatch):
    """
    #259's interaction with the systemic-failure guard, pinned deliberately.

    REQUEST_FAILED is in SYSTEMIC_FAILURE_STATUSES and ZERO_RESULTS is not, so
    this change can only ever push a run's denied fraction UP -- it can newly
    trip the >=95% guard, never newly clear it. Tripping is the right outcome:
    a run that measured almost nothing carries no information about the city
    and must not become the diff baseline for its series. The alternative is
    what #259 describes -- publishing that same night as a confident "no
    imagery anywhere", which is silently worse than failing loudly.
    """
    data_dir, _ = _setup(
        tmp_path,
        monkeypatch,
        [],
        failed_tiles=[CITY_TILE, NORTH_TILE],
        edges=_seam_edges(),
    )

    assert collect.run_collect(_args(data_dir)) == 1
    assert not os.path.exists(os.path.join(data_dir, _csv_name()))
    assert os.path.exists(os.path.join(data_dir, _csv_name() + ".rejected"))
    conn = db.connect(db.get_default_db_path(data_dir))
    assert db.get_latest_street_walk(conn, CITY_ID, provider="mapillary") is None
    conn.close()


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


def test_the_road_walk_jitter_reaches_the_census(tmp_path, monkeypatch):
    """Same seam as the rate above (issue #292): the walk pays for the census on
    an un-paired night, so its tile requests must carry the same jitter the grid
    run's do — otherwise the metronome survives on one of the two channels."""
    from streetscape_metadata_tracker.download_mapillary import DEFAULT_TILE_JITTER

    data_dir, _ = _setup(tmp_path, monkeypatch, [_image("i-1", 44.0500, -121.3000)])
    census = cm.fetch_city_images_async
    seen = []

    async def capture(city_name, bbox, access_token, **kwargs):
        seen.append(kwargs)
        return await census(city_name, bbox, access_token, **kwargs)

    monkeypatch.setattr(cm, "fetch_city_images_async", capture)

    assert collect.run_collect(_args(data_dir, **{"mapillary-jitter": 0.3})) == 0
    assert seen[-1]["jitter"] == pytest.approx(0.3)
    assert collect.run_collect(_args(data_dir, force=True)) == 0
    assert seen[-1]["jitter"] == DEFAULT_TILE_JITTER


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
#
# The join moved to census_walk with #258 (it touches only lon/lat/is_pano, so
# it was already provider-agnostic) and is addressed there now. The tests stay
# here, driven by a real MAPILLARY census: the behaviours below -- closest of
# each kind, chunk-invariance, empty inputs -- are properties of the join, and
# exercising them against an actual provider census is what makes them evidence
# rather than a restatement of the implementation.


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
    panos, flats = census_walk.nearest_images_to_samples(samples, census, 25.0)
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

    monkeypatch.setattr(census_walk, "_JOIN_CHUNK_SIZE", 10_000)  # one block
    whole_panos, whole_flats = census_walk.nearest_images_to_samples(samples, census, match_dist)

    monkeypatch.setattr(census_walk, "_JOIN_CHUNK_SIZE", 7)  # boundaries at 7, 14, 21
    chunked_panos, chunked_flats = census_walk.nearest_images_to_samples(
        samples, census, match_dist
    )

    assert _ids(census, chunked_panos) == _ids(census, whole_panos)
    assert _ids(census, chunked_flats) == _ids(census, whole_flats)
    matched = (whole_panos >= 0).sum()
    assert matched, "fixture should match at least some samples"
    # Samples out of range stay unmatched rather than being filled in.
    assert matched < len(samples)


def test_nearest_images_to_samples_handles_empty_inputs():
    one_image = records_to_census([_image("p", 44.05, -121.3)])
    no_samples = census_walk.nearest_images_to_samples([], one_image, 25.0)
    assert all(len(m) == 0 for m in no_samples)
    # An empty census leaves every sample unmatched rather than raising.
    no_images = census_walk.nearest_images_to_samples(
        [(44.05, -121.3, 0, 0)], records_to_census([]), 25.0
    )
    assert all(list(m) == [-1] for m in no_images)


# --- The census checkpoint (issue #256) -------------------------------------


def test_the_walk_checkpoints_under_its_own_channel_not_the_grid_runs(tmp_path, monkeypatch):
    """
    A walk and a grid run of one city sweep the IDENTICAL frozen bbox, so
    geometry alone cannot tell their checkpoints apart. The budget channel is
    what does — and it has to be the channel, not the provider, or the walk
    would resume the grid run's census and meter it into the wrong ledger under
    the wrong credential.

    The NETWORK TYPE is the other half, and the channel does not supply it: two
    walks of one city agree on the ledger, the credential and every geometric
    parameter, so without it the second would re-finalize the first's crawl for
    zero requests and inherit its `api_requests_total`.
    """
    images = [_image("p1", 44.05, -121.30)]
    data_dir, calls = _setup(tmp_path, monkeypatch, images)

    assert collect.run_collect(_args(data_dir)) == 0

    city = db.resolve_city(db.connect(db.get_default_db_path(data_dir)), CITY_QUERY)
    bbox = grid_bbox(
        city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
    )
    assert calls["checkpoint_channel"] == "mapillary_streets"
    assert calls["checkpoint_variant"] == "drive"
    assert calls["checkpoint_path"] == checkpoint_path_for(
        CITY_ID, bbox, "mapillary_streets", variant="drive"
    )
    assert calls["checkpoint_path"] != checkpoint_path_for(CITY_ID, bbox, "mapillary")
    assert calls["checkpoint_path"] != checkpoint_path_for(
        CITY_ID, bbox, "mapillary_streets", variant="all_public"
    )


def test_the_walk_row_takes_the_crawl_and_the_ledger_this_process(tmp_path, monkeypatch):
    """
    The #239/#256 split, on the walk path this time: street_walks.api_requests
    describes the walk, api_usage is additive and keyed by (date, provider).
    """
    images = [_image("p1", 44.05, -121.30)]
    data_dir, _ = _setup(tmp_path, monkeypatch, images, api_requests=4, api_requests_total=19)

    assert collect.run_collect(_args(data_dir)) == 0

    conn = db.connect(db.get_default_db_path(data_dir))
    walk = db.get_latest_street_walk(conn, CITY_ID, provider="mapillary")
    assert walk["api_requests"] == 19, "the row carries the whole crawl"
    assert db.get_api_usage(conn, date(2026, 7, 8), provider="mapillary_streets") == 4, (
        "the ledger carries only this process's spend"
    )


def test_a_walk_that_fails_to_catalog_keeps_its_checkpoint(tmp_path, monkeypatch):
    """
    The discard belongs AFTER register_street_walk. Before it, a catalog failure
    would cost the whole census again — the placement cli.py already reasons
    through, and the walk path had no discard at all until #256.
    """
    images = [_image("p1", 44.05, -121.30)]
    data_dir, calls = _setup(tmp_path, monkeypatch, images)
    discarded = []
    monkeypatch.setattr(collect, "discard_checkpoint", discarded.append)

    def explode(*a, **k):
        raise RuntimeError("catalog write failed")

    monkeypatch.setattr(db, "register_street_walk", explode)
    with pytest.raises(RuntimeError):
        collect.run_collect(_args(data_dir))
    assert discarded == [], "a failed register must not spend the checkpoint"


def test_a_cataloged_walk_discards_its_checkpoint(tmp_path, monkeypatch):
    images = [_image("p1", 44.05, -121.30)]
    data_dir, calls = _setup(tmp_path, monkeypatch, images)
    discarded = []
    monkeypatch.setattr(collect, "discard_checkpoint", discarded.append)

    assert collect.run_collect(_args(data_dir)) == 0
    assert discarded == [calls["checkpoint_path"]]


def test_a_walk_whose_tail_dies_still_records_what_the_census_cost(tmp_path, monkeypatch):
    """
    The checkpoint is what makes this a permanent loss rather than a wasted
    night. Without one, a tail failure lost the spend with the process and a
    re-run bought the tiles again, so nothing went unrecorded. With one, the
    checkpoint survives COMPLETE and the next invocation re-finalizes it for
    zero requests — so a spend missed here would land in no `api_usage` row,
    ever. Same gap PR #251 closed for KartaView in the grid pipeline.
    """
    images = [_image("p1", 44.05, -121.30)]
    data_dir, _ = _setup(tmp_path, monkeypatch, images, api_requests=11)

    def explode(*a, **k):
        raise OSError("no space left on device")

    # After the census, before the CSV lands — not a DownloadError.
    monkeypatch.setattr(cm, "build_streetwalk_rows", explode)

    assert collect.run_collect(_args(data_dir)) == 1
    conn = db.connect(db.get_default_db_path(data_dir))
    assert db.get_api_usage(conn, date(2026, 7, 8), provider="mapillary_streets") == 11, (
        "the tiles were bought; the ledger has to know even though the walk failed"
    )


# --- The shared census cache (issue #290) -----------------------------------
#
# A Mapillary walk and its city's grid run read the IDENTICAL z14 census over
# the IDENTICAL frozen bbox — that identity is stated a few tests up, as the
# reason the two need separate CHECKPOINTS. Here it is the point rather than the
# hazard: on a paired night the grid run pays and the walk reads its census for
# zero requests, and `--network-type all_public` becomes free rather than a
# third copy.


def test_the_walk_reaches_the_provider_keyed_cache_the_grid_run_writes(tmp_path, monkeypatch):
    """
    The two path builders, side by side, because both mistakes are silent. The
    CHECKPOINT path keys the channel and the network type (a shared one would
    let two crawls resume each other's spend into the wrong ledger); the CACHE
    path keys NEITHER (a channel-keyed one would never reuse anything, and every
    census would still be bought twice with nothing failing).
    """
    images = [_image("p1", 44.05, -121.30)]
    data_dir, calls = _setup(tmp_path, monkeypatch, images)

    assert collect.run_collect(_args(data_dir)) == 0

    bbox = grid_bbox(44.05, -121.30, 200, 200, 20)
    assert calls["cache_path"] == census_cache_path_for("mapillary", CITY_ID, bbox)
    assert calls["cache_path"] == census_cache_path_for("mapillary", CITY_ID, bbox), (
        "and the grid run resolves to the very same entry"
    )
    assert calls["cache_path"] != calls["checkpoint_path"]
    assert calls["reuse_census"] is True
    # The snapshot date travels with the policy, so an entry observed after a
    # backdated --run-date is refused at the loader rather than published.
    assert calls["run_date"] == date.fromisoformat(RUN_DATE)


def test_a_reused_census_costs_the_street_ledger_nothing_and_says_who_paid(tmp_path, monkeypatch):
    """
    The zero has to be legible. `street_walks.api_requests = 0` on a fully
    walked city reads as a bug unless the row also records that the `mapillary`
    channel bought the census — which is what the v14 provenance columns are.
    """
    images = [_image("p1", 44.05, -121.30)]
    data_dir, _ = _setup(
        tmp_path,
        monkeypatch,
        images,
        api_requests=0,
        api_requests_total=0,
        census_fetched_by="mapillary",
        census_fetched_at="2026-07-08T01:02:03+00:00",
    )

    assert collect.run_collect(_args(data_dir)) == 0

    conn = db.connect(db.get_default_db_path(data_dir))
    walk = db.get_latest_street_walk(conn, CITY_ID, provider="mapillary")
    assert walk["api_requests"] == 0
    assert walk["census_fetched_by"] == "mapillary"
    assert walk["census_fetched_at"] == "2026-07-08T01:02:03+00:00"
    assert db.get_api_usage(conn, date(2026, 7, 8), provider="mapillary_streets") == 0
    assert db.get_api_usage(conn, date(2026, 7, 8), provider="mapillary") == 0, (
        "and the walk must never charge the channel that actually paid"
    )


def test_a_reused_census_stamps_its_rows_with_when_mapillary_was_observed(tmp_path, monkeypatch):
    """
    Every row of a reused census was fetched by another collection, possibly on
    an earlier night, so stamping `query_timestamp` with this process's clock
    would record an observation that never happened — and json_summarizer reports
    the run's start/end from exactly that column.
    """
    images = [_image("p1", 44.05, -121.30)]
    observed = "2026-07-07T22:15:00+00:00"
    data_dir, _ = _setup(
        tmp_path, monkeypatch, images, census_fetched_by="mapillary", census_fetched_at=observed
    )

    assert collect.run_collect(_args(data_dir)) == 0
    with gzip.open(os.path.join(data_dir, _csv_name()), "rt") as fh:
        rows = fh.read()
    assert observed in rows


def test_a_freshly_fetched_census_keeps_this_processs_clock(tmp_path, monkeypatch):
    """
    The other side, and the reason the restamp is gated on REUSE rather than on
    the provenance being present at all: a fresh crawl's rows were observed now,
    and #256's byte-identity contract between an interrupted census and an
    uninterrupted one is written against `started_at`.
    """
    images = [_image("p1", 44.05, -121.30)]
    crawl_start = "2026-07-01T00:00:00+00:00"
    data_dir, _ = _setup(
        tmp_path,
        monkeypatch,
        images,
        census_fetched_by="mapillary_streets",
        census_fetched_at=crawl_start,
        census_reused=False,
    )

    assert collect.run_collect(_args(data_dir)) == 0
    with gzip.open(os.path.join(data_dir, _csv_name()), "rt") as fh:
        rows = fh.read()
    assert crawl_start not in rows


def test_refetch_census_tells_the_collector_not_to_reuse(tmp_path, monkeypatch):
    """
    Deliberately separate from --force, which is about this run date's
    artifacts: a walk whose tail died after writing its CSV is re-run with
    --force and must re-finalize for zero requests rather than re-pay a census
    it already bought.
    """
    images = [_image("p1", 44.05, -121.30)]
    data_dir, calls = _setup(tmp_path, monkeypatch, images)

    assert collect.run_collect(_args(data_dir, **{"refetch-census": True})) == 0
    assert calls["reuse_census"] is False


def test_force_leaves_the_cache_alone(tmp_path, monkeypatch):
    images = [_image("p1", 44.05, -121.30)]
    data_dir, calls = _setup(tmp_path, monkeypatch, images)

    assert collect.run_collect(_args(data_dir)) == 0
    assert collect.run_collect(_args(data_dir, **{"force": True})) == 0
    assert calls["reuse_census"] is True, "--force is about artifacts, not observations"


def test_gsv_never_probes_the_cache(tmp_path, monkeypatch):
    """
    GSV queries per point; there is no census to share, so the probe must answer
    None rather than pricing a gsv_streets walk at zero off a Mapillary entry
    that happens to exist for the same city.
    """
    images = [_image("p1", 44.05, -121.30)]
    data_dir, _ = _setup(tmp_path, monkeypatch, images)
    conn = db.connect(db.get_default_db_path(data_dir))
    city = db.resolve_city(conn, CITY_QUERY)
    bbox = grid_bbox(
        city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
    )
    _stamp_cache_entry(census_cache_path_for("mapillary", CITY_ID, bbox))

    assert collect._cached_census_marker(city, "gsv", _args(data_dir)) is None
    assert collect._cached_census_marker(city, "mapillary", _args(data_dir)) is not None
    # --refetch-census prices the fetch it is about to force, not the entry it
    # is about to ignore.
    forced = _args(data_dir, **{"refetch-census": True})
    assert collect._cached_census_marker(city, "mapillary", forced) is None


def test_estimate_prices_a_cached_census_at_zero(tmp_path, monkeypatch, capsys):
    """
    `--estimate` is how an operator decides whether to spend, so it has to know
    the walk is free. It reads the marker only — a planning pass must not become
    a disk sweep — which makes a hit a strong hint rather than a promise.
    """
    images = [_image("p1", 44.05, -121.30)]
    data_dir, _ = _setup(tmp_path, monkeypatch, images)
    conn = db.connect(db.get_default_db_path(data_dir))
    city = db.resolve_city(conn, CITY_QUERY)
    bbox = grid_bbox(
        city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
    )
    _stamp_cache_entry(census_cache_path_for("mapillary", CITY_ID, bbox))

    assert collect.run_collect(_args(data_dir, estimate=True)) == 0
    out = capsys.readouterr().out
    assert "0 Mapillary tile requests" in out
    assert "cached census fetched by mapillary" in out


def test_the_budget_preflight_does_not_abort_a_free_walk(tmp_path, monkeypatch):
    """
    Without this the cheapest possible walk — one whose census the grid run
    already paid for — is exactly the one a nearly-spent street budget refuses,
    so the pairing the cache exists to exploit never happens on the nights it
    helps most.
    """
    images = [_image("p1", 44.05, -121.30)]
    data_dir, _ = _setup(tmp_path, monkeypatch, images, api_requests=0, api_requests_total=0)
    conn = db.connect(db.get_default_db_path(data_dir))
    city = db.resolve_city(conn, CITY_QUERY)
    bbox = grid_bbox(
        city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
    )
    # A zero budget refuses any census at all, so an uncached walk aborts.
    uncached = collect.run_collect(_args(data_dir, **{"daily-budget": 0}))
    assert uncached == 1, "the guard still works when there is nothing to reuse"

    _stamp_cache_entry(census_cache_path_for("mapillary", CITY_ID, bbox))
    assert collect.run_collect(_args(data_dir, **{"daily-budget": 0})) == 0


def _stamp_cache_entry(cache_path, *, fetched_by="mapillary"):
    """A marker-only cache entry — what `census_cache_probe` reads."""
    return stamp_census_cache(cache_path, "mapillary", fetched_by=fetched_by)
